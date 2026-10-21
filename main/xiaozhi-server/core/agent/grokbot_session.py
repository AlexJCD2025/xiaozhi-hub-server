"""Grok Bot 会话：对着一个云端 Bot 的主对话发一句话，把它的回复一条条交给设备播报。

和 ClaudeSession 不同，这里没有本机进程：对话记录在 Bot 的云端电脑上，天然跨连接接续，
"会话 ID"就是 Bot 的 id。一轮的经过：

1. 先记下对话尾部已有的条目（之后只播新的）；
2. sendPrompt 发送，结果不明确时用 promptAcceptanceStatus 确认，不盲目重发；
3. 从事件流收：Bot 开始处理（agent-upserted）→ 我们的消息回显（transcript）→ 回复条目 → Bot 空闲；
   空闲 1.5 秒无新动静算本轮结束（服务端托管的 Bot 会先变空闲、再追加最后一条消息）；
4. 兜底：发送后 10 秒没看到回显就让中枢重连并补拉尾部；等待期间每 12 秒轮询一次（listAgents 看状态，
   newestEntryId 变了才拉尾部）。推送和轮询按条目 id 去重，谁先到用谁。

用户拍板：多条回复每条到了就播；打断只停止播报，云端任务继续做，不找停止接口。
"""

import asyncio
import json
import re
import time
import uuid
from dataclasses import dataclass
from typing import AsyncIterator, Dict, List, Optional

from core.agent.agent_session import Activity, AgentEvent, AgentSession, AgentSessionError, Done, Error, ErrorKind, TextDelta
from core.agent.grokbot_client import GrokBotError, GrokBotHub, Subscription

SENTENCE_END = set("。！？!?；;…~～.\n")
WAITING_NOTICE = "Grok Bot 在等你去 App 里处理。"
NO_REPLY_NOTICE = "Grok Bot 没有回话。"

# 出错时播报给用户的话（代替 agent_session 里 Claude 的默认文案）
VOICE_MESSAGES = {
    ErrorKind.AUTH: "Grok Bot 还没登录，或者登录过期了，请到管理后台重新登录。",
    ErrorKind.SERVER: "Grok Bot 那边暂时连不上，稍后再试吧。",
    ErrorKind.TIMEOUT: "Grok Bot 这件事做得太久，我先不等了。它做完后你在 App 里能看到结果。",
    ErrorKind.STALLED: "Grok Bot 一直没有回应，我先停下了。",
    ErrorKind.PROCESS_EXITED: "和 Grok Bot 的连接断开了，再说一次试试。",
    ErrorKind.UNKNOWN: "出了点小问题，再说一次试试。",
}


def voice_for(kind: ErrorKind) -> str:
    return VOICE_MESSAGES.get(kind) or VOICE_MESSAGES[ErrorKind.UNKNOWN]


@dataclass
class GrokBotSessionConfig:
    bot_id: str
    bot_name: str = ""
    echo_timeout: float = 10  # 发送后这么久没在事件流里看到回显，就重连并补拉
    poll_interval: float = 12  # 等回复期间轮询对话尾部的间隔
    done_delay: float = 1.5  # Bot 空闲后再等这么久没新动静才算本轮结束
    first_reply_grace: float = 5  # Bot 忙过又空闲了、却一条回复都还没来：多等一会儿（服务端托管的 Bot 先空闲再追加最后一条）
    no_reply_timeout: float = 90  # 消息送到了、Bot 一直空闲又没回话，这么久后结束本轮
    connect_wait: float = 3  # 发送前最多等事件流连上这么久
    tail_limit: int = 20


def plain_text(text: str) -> str:
    """去掉 Markdown 记号（Bot 的回复是给屏幕看的），只留要念的文字。"""
    t = re.sub(r"```.*?```", " ", text or "", flags=re.S)
    t = re.sub(r"`([^`\n]*)`", r"\1", t)
    t = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", t)
    t = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", t)
    t = re.sub(r"^\s{0,3}#{1,6}\s*", "", t, flags=re.M)
    t = re.sub(r"^\s*(?:[-*+]|\d+[.)])\s+", "", t, flags=re.M)
    t = re.sub(r"^\s*>\s?", "", t, flags=re.M)
    t = re.sub(r"(\*\*|__|~~)(.+?)\1", r"\2", t, flags=re.S)
    t = re.sub(r"(?<![\w*])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![\w*])", r"\1", t)
    t = re.sub(r"^\s*\|?[\s:|-]+\|\s*$", "", t, flags=re.M)  # 表格分隔行
    t = t.replace("|", " ")
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\s*\n\s*", "\n", t)
    return t.strip()


def spoken_form(text: str) -> str:
    """一条回复整理成播报形式：去 Markdown，结尾补句号，TTS 才会马上念出来。"""
    t = plain_text(text)
    if t and t[-1] not in SENTENCE_END:
        t += "。"
    return t


def reply_text(entry: dict) -> Optional[str]:
    """Bot 给人看的文字：assistant 的 message，或 send-message 里的 text。其他条目（工具调用、通知）返回 None。"""
    kind = entry.get("kind")
    if kind == "message" and (entry.get("role") == "assistant" or entry.get("fromAgent")):
        return entry.get("content") or ""
    if kind == "send-message":
        m = entry.get("message") or {}
        if m.get("type") == "text":
            return m.get("content") or m.get("text") or ""
    return None


def agent_busy(agent: dict) -> bool:
    return bool(agent.get("isRunning") or agent.get("isRunningTurn") or agent.get("isComposingMessage"))


def agent_waiting(agent: dict) -> bool:
    return agent.get("awaitingUserResponse") not in (None, False, "", "null")


def is_streaming(entry: dict) -> bool:
    return bool(entry.get("isStreaming") or entry.get("streaming"))


class GrokBotSession(AgentSession):
    def __init__(self, hub: GrokBotHub, config: GrokBotSessionConfig, logger=None):
        self.hub = hub
        self.config = config
        self.logger = logger
        self._sub: Optional[Subscription] = None
        self._interrupted = False
        self._started = False

    @property
    def session_id(self) -> Optional[str]:
        return self.config.bot_id

    @property
    def alive(self) -> bool:
        return self._started

    def _log(self, level: str, message: str) -> None:
        if self.logger:
            getattr(self.logger, level)(f"[{self.config.bot_name or self.config.bot_id[:8]}] {message}")

    # ---------- 生命周期 ----------

    async def start(self) -> None:
        try:
            await self.hub.client.ensure_box()
        except GrokBotError as e:
            raise AgentSessionError(e.kind, e.detail)
        self._sub = self.hub.subscribe(self.config.bot_id)
        self._started = True

    async def close(self) -> None:
        self._started = False
        sub, self._sub = self._sub, None
        if sub is not None:
            sub.close()

    async def interrupt(self) -> None:
        self._interrupted = True
        if self._sub is not None:
            self._sub.queue.put_nowait(("interrupt", {}))

    # ---------- 一轮对话 ----------

    async def send(self, text: str) -> AsyncIterator[AgentEvent]:
        if not self._started or self._sub is None:
            yield Error(ErrorKind.PROCESS_EXITED, "session not started", voice=voice_for(ErrorKind.PROCESS_EXITED))
            return
        self._interrupted = False
        cfg = self.config
        client = self.hub.client
        bot_id = cfg.bot_id
        queue = self._sub.queue
        nonce = str(uuid.uuid4())
        seen: Dict[str, str] = {}  # 条目 id -> 已播报的文字；发送前已有的条目也记在这里（值为空）
        raw: Dict[str, str] = {}  # 已播报条目的纯文字（没补句号），条目被更新、文字变长时只念多出来的部分
        t0 = time.monotonic()
        state = {"echoed": False, "busy": False, "was_busy": False, "idle_since": None, "spoken": 0,
                 "generation": self.hub.generation, "newest": None, "waiting": False, "recovered": False}

        # 清掉上一轮残留的事件
        while not queue.empty():
            queue.get_nowait()

        try:
            for e in await client.transcript_tail(bot_id, cfg.tail_limit):
                if e.get("id"):
                    seen[str(e["id"])] = ""
        except GrokBotError as e:
            self._log("warning", f"读对话尾部失败：{e.detail}")
            if e.kind == ErrorKind.AUTH:
                yield Error(e.kind, e.detail, voice=voice_for(e.kind))
                return

        if not self.hub.connected and not await self.hub.wait_connected(cfg.connect_wait):
            self._log("warning", f"事件流 {cfg.connect_wait} 秒内没连上，先发送，靠补拉和轮询兜底")

        try:
            res = await client.send_prompt(bot_id, text, nonce)
            self._log("info", f"+{time.monotonic() - t0:.1f}s sendPrompt 返回 {json.dumps(res, ensure_ascii=False)[:120]}")
        except GrokBotError as e:
            if e.kind == ErrorKind.AUTH:
                yield Error(e.kind, e.detail, voice=voice_for(e.kind))
                return
            # 结果不明确：查送达记录。只有查不到记录才是真的没送到（也只有这时才能用同一个 nonce 重发，这里不重发）
            try:
                status = await client.acceptance_status(bot_id, nonce)
            except GrokBotError as e2:
                yield Error(ErrorKind.SERVER, f"sendPrompt 失败：{e.detail}；查送达状态也失败：{e2.detail}",
                            voice=voice_for(ErrorKind.SERVER))
                return
            self._log("warning", f"sendPrompt 失败：{e.detail}；送达状态 {json.dumps(status, ensure_ascii=False)[:200]}")
            if "not-found" in json.dumps(status).lower() or "not_found" in json.dumps(status).lower():
                yield Error(ErrorKind.SERVER, f"消息没有送到：{e.detail}", voice="Grok Bot 没收到这句话，再说一次试试。")
                return

        yield Activity("sent", tools_running=1)
        next_poll = t0 + cfg.poll_interval

        def process_entry(entry: dict) -> List[AgentEvent]:
            """新条目：回显就标记，Bot 的文字就播报。返回要产出的事件。"""
            entry_id = str(entry.get("id") or "")
            if not entry_id or is_streaming(entry):
                return []
            if entry.get("clientNonce") == nonce or (
                    entry.get("kind") == "message" and entry.get("role") == "user" and entry.get("content") == text):
                if not state["echoed"]:
                    state["echoed"] = True
                    self._log("info", f"+{time.monotonic() - t0:.1f}s 消息已出现在对话里")
                seen.setdefault(entry_id, "")
                return []
            content = reply_text(entry)
            if entry_id in seen:
                # 已经念过的条目被更新：文字在原来基础上变长（流式追加）就只念多出来的部分，别的改动不管
                old = raw.get(entry_id)
                new = plain_text(content) if content is not None else ""
                if old is None or len(new) <= len(old) or not new.startswith(old):
                    return []
                raw[entry_id] = new
                extra = spoken_form(new[len(old):].lstrip("。.！!？?；;，,、 \n"))  # 上一段已经补过句号
                if not extra:
                    return []
                state["idle_since"] = None if state["busy"] else time.monotonic()
                self._log("info", f"+{time.monotonic() - t0:.1f}s 回复追加：{extra[:80]}")
                return [TextDelta(extra)]
            if content is None:
                seen[entry_id] = ""
                return []
            spoken = spoken_form(content)
            seen[entry_id] = spoken
            if not spoken:
                return []
            raw[entry_id] = plain_text(content)
            state["spoken"] += 1
            state["idle_since"] = None if state["busy"] else time.monotonic()
            self._log("info", f"+{time.monotonic() - t0:.1f}s 回复：{spoken[:80]}")
            return [TextDelta(spoken)]

        def update_agent(agent: dict) -> List[AgentEvent]:
            busy = agent_busy(agent)
            events: List[AgentEvent] = []
            if busy and not state["was_busy"]:
                self._log("info", f"+{time.monotonic() - t0:.1f}s Bot 开始处理")
            if busy:
                state["was_busy"] = True
                state["idle_since"] = None
            elif state["busy"] or state["idle_since"] is None:
                state["idle_since"] = time.monotonic()
            state["busy"] = busy
            if agent_waiting(agent) and not state["waiting"]:
                state["waiting"] = True
                self._log("info", "Bot 在等用户处理（审批/提问），需要去 App 里看")
                events.append(TextDelta(WAITING_NOTICE))
            return events

        async def poll() -> List[AgentEvent]:
            events: List[AgentEvent] = []
            agents = await client.list_agents()
            agent = next((a for a in agents if a.get("id") == bot_id), None)
            if agent is None:
                raise AgentSessionError(ErrorKind.UNKNOWN, f"云端没有这个 Bot：{bot_id}")
            events.extend(update_agent(agent))
            # 每次都拉尾部：newestEntryId 的含义没有文档，实测过一次它没变、尾部却有新条目的情况，不能只靠它省请求
            entries = await client.transcript_tail(bot_id, cfg.tail_limit)
            self._log("debug", f"+{time.monotonic() - t0:.1f}s 轮询：busy={agent_busy(agent)} "
                               f"newest={agent.get('newestEntryId')} 尾部 {len(entries)} 条，已见 {len(seen)} 条")
            for e in entries:
                events.extend(process_entry(e))
            return events

        try:
            while True:
                if self._interrupted:
                    self._log("info", "本轮被打断，停止播报（云端继续做）")
                    yield Done(session_id=bot_id, interrupted=True)
                    return
                now = time.monotonic()
                idle_pending = state["idle_since"] is not None and (state["was_busy"] or state["spoken"])
                # 忙过却一条回复都没来：多等几秒，服务端托管的 Bot 会先变空闲、再追加最后一条消息
                idle_wait = cfg.done_delay if state["spoken"] else max(cfg.done_delay, cfg.first_reply_grace)
                if idle_pending and now - state["idle_since"] >= idle_wait:
                    self._log("info", f"+{now - t0:.1f}s 本轮结束，共 {state['spoken']} 条回复")
                    yield Done(session_id=bot_id)
                    return
                if state["waiting"]:
                    # Bot 在等用户去 App 里处理：这一轮到此为止，不管它的忙闲标志
                    yield Done(session_id=bot_id)
                    return
                if state["echoed"] and not state["busy"] and not state["was_busy"] and not state["spoken"] \
                        and now - t0 >= cfg.no_reply_timeout:
                    self._log("warning", f"消息送到了但 Bot {int(cfg.no_reply_timeout)} 秒内没有回话")
                    yield TextDelta(NO_REPLY_NOTICE)
                    yield Done(session_id=bot_id)
                    return

                waits = [next_poll - now]
                if idle_pending:
                    waits.append(state["idle_since"] + idle_wait - now)
                if not state["echoed"] and not state["recovered"]:
                    waits.append(t0 + cfg.echo_timeout - now)
                try:
                    channel, payload = await asyncio.wait_for(queue.get(), timeout=max(0.05, min(waits)))
                except asyncio.TimeoutError:
                    channel, payload = None, {}

                if channel == "transcript":
                    if payload.get("type") in ("appended", "updated") and isinstance(payload.get("entry"), dict):
                        for ev in process_entry(payload["entry"]):
                            yield ev
                elif channel == "agent-upserted":
                    for ev in update_agent(payload.get("agent") or {}):
                        yield ev
                    yield Activity("agent", tools_running=1)
                elif channel == "hub-connected":
                    if payload.get("generation") != state["generation"]:
                        # 事件流重连过，中间的事件可能漏了：补拉一次
                        state["generation"] = payload.get("generation")
                        for ev in await poll():
                            yield ev
                        next_poll = time.monotonic() + cfg.poll_interval
                elif channel == "interrupt":
                    continue

                now = time.monotonic()
                if not state["echoed"] and not state["recovered"] and now - t0 >= cfg.echo_timeout:
                    # 事件流可能"假活"：连接还在，但什么都不推。重连并补拉
                    state["recovered"] = True
                    self._log("warning", f"发送 {int(cfg.echo_timeout)} 秒后事件流里没有回显，重连事件流并补拉对话")
                    self.hub.reconnect()
                    for ev in await poll():
                        yield ev
                    next_poll = time.monotonic() + cfg.poll_interval
                elif now >= next_poll:
                    for ev in await poll():
                        yield ev
                    next_poll = time.monotonic() + cfg.poll_interval
                    yield Activity("poll", tools_running=1)
        except AgentSessionError as e:
            yield Error(e.kind, e.detail, voice=voice_for(e.kind))
        except GrokBotError as e:
            yield Error(e.kind, e.detail, voice=voice_for(e.kind))

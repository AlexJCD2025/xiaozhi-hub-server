"""claude_cli：让小智设备通过本机 Claude Code（claude-agent-sdk）对话和干活。

线程模型：
- 服务端在每个连接的线程池里同步迭代 response() 生成器；
- 本 provider 自带一个后台线程运行 asyncio 事件循环，所有 SDK 调用都在这个循环里执行；
- 两边通过 queue.Queue 传递事件。

每个设备连接最多对应一个常驻的 Claude Code 进程：首句话到来时启动（顺便决定新开还是接续），
空闲超时、连接关闭或服务退出时结束。
"""

import asyncio
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

from config.config_loader import get_project_dir
from config.logger import setup_logging
from core.agent.agent_session import Activity, AgentSessionError, Done, Error, ErrorKind, TextDelta
from core.agent.claude_session import ClaudeSession, ClaudeSessionConfig
from core.agent.session_store import SessionStore, choose_resume
from core.agent.voice_commands import DEFAULT_RESUME_KEYWORDS, match_resume
from core.providers.llm.base import LLMProviderBase
from core.utils import textUtils
from core.utils.prompt_manager import EMOJI_List

TAG = __name__
logger = setup_logging()

VOICE_RULES = f"""
# 语音助手规则（最高优先级）
你现在通过一个语音设备和用户对话，你说的每个字都会被朗读出来。
1. 每次回复的第一个字符必须是一个表示心情的 emoji，并且只能从这个列表中选：{" ".join(EMOJI_List)}。列表以外的 emoji 一律不要用，回复中间和结尾也不要再出现 emoji。
2. 回答要简短口语化，一般不超过 3 句话。
3. 不要使用 Markdown、列表、编号、代码块、链接、表格等任何格式符号。
4. 需要调用工具时，调用前最多说一句过渡语；完成后用一两句话汇报结果，不要念出代码、命令、路径细节和文件内容。
5. 用户没问时，不要主动解释自己有哪些工具、权限或限制。
"""

COMMON_CLI_PATHS = [
    "~/.claude/local/claude",
    "~/.local/bin/claude",
    "/opt/homebrew/bin/claude",
    "/usr/local/bin/claude",
]


def single_sentence(text: str) -> str:
    """把一句话整理成 TTS 只会切出一段的形式。

    原项目为了降低首字延迟，第一句遇到逗号、顿号等就会切开，填充语会因此变成两段（两个气泡）。
    这里把句中的停顿标点换成空格，结尾统一用句号。
    """
    body = re.sub(r"[，,、~～；;：:]+", " ", text or "").strip()
    body = body.rstrip("。.！!？? ")
    return f"{body}。" if body else ""


def find_claude_cli(configured: Optional[str] = None) -> Optional[str]:
    """优先用配置的路径，其次 PATH，再其次常见安装位置（从图形界面启动时 PATH 可能不完整）。"""
    if configured:
        p = Path(os.path.expanduser(configured))
        return str(p) if p.exists() else None
    names = ["claude.exe", "claude"] if sys.platform == "win32" else ["claude"]
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    for candidate in COMMON_CLI_PATHS:
        p = Path(os.path.expanduser(candidate))
        if p.exists():
            return str(p)
    return None


def claude_cli_version(cli_path: str, timeout: float = 15) -> str:
    """运行 `claude --version`，返回第一行非空输出；失败时抛出异常。"""
    out = subprocess.run([cli_path, "--version"], capture_output=True, text=True, encoding="utf-8", timeout=timeout)
    lines = [l.strip() for l in (out.stdout or out.stderr or "").splitlines() if l.strip()]
    return lines[0] if lines else ""


def _num(config: dict, key: str, default: float) -> float:
    """读取数字配置。只有没填（None 或空字符串）才用默认值，填 0 就是 0。"""
    value = config.get(key)
    if value is None or str(value).strip() == "":
        return float(default)
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


@dataclass
class ConnState:
    key: str  # 设备 ID；没有时退回连接的 session_id
    conn: object = None
    agent: Optional[ClaudeSession] = None
    lock: Optional[asyncio.Lock] = None
    idle_handle: Optional[asyncio.TimerHandle] = None
    intent_warned: bool = False
    closed: bool = False  # 连接已关闭：不再启动或保留 Claude Code 进程
    turn_gen: int = 0  # 每开始一轮加 1，用于让过期的空闲定时器失效


class LLMProvider(LLMProviderBase):
    def __init__(self, config: dict):
        self.model_name = f"claude_cli:{config.get('model') or 'default'}"
        self.cli_path = find_claude_cli(config.get("cli_path"))
        workdir = Path(os.path.expanduser(config.get("workdir") or "~/xiaozhi-workspace"))
        workdir.mkdir(parents=True, exist_ok=True)
        self.session_config = ClaudeSessionConfig(
            cli_path=self.cli_path,
            model=config.get("model") or None,
            workdir=str(workdir),
            allowed_tools=list(config.get("allowed_tools") or ["Read", "Grep", "Glob"]),
            permission_mode=config.get("permission_mode") or "default",
            # 默认 low：语音场景以快速应答为主，实测工具类问题从约 6 秒降到约 4 秒
            effort=str(config.get("effort", "low") or "").strip() or None,
        )
        # 超时类配置填 0 表示不限制
        self.turn_timeout = _num(config, "turn_timeout_seconds", 120) or float("inf")
        # 无响应超时：Claude Code 连续这么久没有任何事件（文字、思考、工具调用……）就判定卡住
        self.stall_timeout = _num(config, "stall_timeout_seconds", 30) or float("inf")
        self.idle_timeout = _num(config, "idle_timeout_seconds", 300)  # 0：每轮结束就关闭进程
        self.resume_window = _num(config, "resume_window_minutes", 120)  # 0：不自动接续
        self.resume_keywords = list(config.get("resume_keywords") or DEFAULT_RESUME_KEYWORDS)
        self.filler = single_sentence(config.get("first_reply_filler", "好的，我看一下"))
        self.filler_delay = _num(config, "filler_delay_seconds", 2)
        self.store = SessionStore(Path(get_project_dir()) / "data" / "agent_sessions.json", "claude_cli")

        self._states: Dict[str, ConnState] = {}
        self._states_lock = threading.Lock()
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, name="claude-cli-loop", daemon=True)
        self._thread.start()
        self._self_check()

    # ---------- 启动自检 ----------

    def _self_check(self) -> None:
        if not self.cli_path:
            logger.bind(tag=TAG).error("未找到 claude 命令，请先安装 Claude Code 并登录，或在配置中指定 cli_path")
            return
        try:
            logger.bind(tag=TAG).info(
                f"Claude Code: {claude_cli_version(self.cli_path)} ({self.cli_path})，"
                f"工作目录 {self.session_config.workdir}，允许工具 {self.session_config.allowed_tools}，"
                f"思考力度 {self.session_config.effort or '默认'}"
            )
        except Exception as e:
            logger.bind(tag=TAG).error(f"无法运行 {self.cli_path} --version: {e}")

    # ---------- 连接钩子（由原项目代码通过 getattr 调用） ----------

    def _state(self, session_id: str) -> ConnState:
        with self._states_lock:
            st = self._states.get(session_id)
            if st is None:
                st = ConnState(key=session_id)
                self._states[session_id] = st
            return st

    def on_conn_open(self, conn) -> None:
        st = self._state(conn.session_id)
        st.conn = conn
        if getattr(conn, "device_id", None):
            st.key = conn.device_id

    def abort(self, conn) -> None:
        st = self._states.get(conn.session_id)
        if st and st.agent:
            asyncio.run_coroutine_threadsafe(st.agent.interrupt(), self._loop)

    def on_conn_close(self, conn) -> None:
        with self._states_lock:
            st = self._states.pop(conn.session_id, None)
        if st:
            st.closed = True
            asyncio.run_coroutine_threadsafe(self._close_agent(st, "连接关闭"), self._loop)

    # ---------- 后台循环中的逻辑 ----------

    async def _close_agent(self, st: ConnState, reason: str) -> None:
        if st.idle_handle:
            st.idle_handle.cancel()
            st.idle_handle = None
        agent, st.agent = st.agent, None
        if agent:
            await agent.close()
            logger.bind(tag=TAG).info(f"已关闭 Claude Code 进程（{reason}），设备 {st.key}")

    def _schedule_idle_close(self, st: ConnState) -> None:
        if st.idle_handle:
            st.idle_handle.cancel()
        gen = st.turn_gen
        st.idle_handle = self._loop.call_later(
            self.idle_timeout, lambda: asyncio.ensure_future(self._idle_close(st, gen))
        )

    async def _idle_close(self, st: ConnState, gen: int) -> None:
        async with st.lock:
            if st.turn_gen != gen:
                return  # 定时器触发前已经开始了新的一轮
            st.idle_handle = None
            await self._close_agent(st, f"空闲 {int(self.idle_timeout)} 秒")

    async def _abandon_if_stuck(self, st: ConnState, gen: int, grace: float = 5) -> None:
        """超时后已发送中断；如果这一轮过了 grace 秒还没结束，说明 Claude Code 卡死了，直接结束进程。
        下一轮会自动重新启动进程（在接续窗口内会接续原会话）。"""
        await asyncio.sleep(grace)
        if st.turn_gen == gen and st.lock is not None and st.lock.locked() and st.agent:
            logger.bind(tag=TAG).warning("中断后 Claude Code 仍未结束本轮，强制关闭进程")
            await self._close_agent(st, "中断无效")

    async def _ensure_agent(self, st: ConnState, text: str, out: queue.Queue, system_prompt: str) -> bool:
        """确保有可用的进程。返回 True 表示这句话已经作为"接续指令"处理完，不必再发给 Claude。

        接续指令后面还带着别的内容时（如"接着上次聊那个 bug"），接续后把整句话照常发给 Claude，
        不能只回一句"好的"就把后半句吞掉。
        """
        if st.agent and st.agent.alive:
            return False
        remainder = match_resume(text, self.resume_keywords)
        keyword = remainder is not None
        resume_id, reason = choose_resume(self.store.get(st.key), keyword, self.resume_window)
        cfg = ClaudeSessionConfig(**{**self.session_config.__dict__,
                                     "system_prompt_append": (system_prompt or "") + "\n" + VOICE_RULES})
        agent = ClaudeSession(cfg, resume_id=resume_id, logger=logger.bind(tag=TAG))
        t0 = time.time()
        try:
            await agent.start()
        except AgentSessionError as e:
            if e.kind != ErrorKind.SESSION_MISSING:
                raise
            logger.bind(tag=TAG).warning(f"接续会话 {resume_id} 失败，改为新开会话")
            agent = ClaudeSession(cfg, logger=logger.bind(tag=TAG))
            await agent.start()  # 新会话也启动失败时（如未登录、网络问题）保留旧记录，下次还能接续
            self.store.forget(st.key)
            if keyword:
                out.put(TextDelta("😶没有找到之前的对话，我们重新开始吧。"))
            resume_id, reason = None, "resume_failed"
        if st.closed:
            await agent.close()  # 启动期间连接已经关闭
            raise AgentSessionError(ErrorKind.PROCESS_EXITED, "连接已关闭")
        st.agent = agent
        logger.bind(tag=TAG).info(
            f"Claude Code 进程已启动（{time.time() - t0:.1f}s），设备 {st.key}，"
            f"{'接续会话 ' + resume_id if resume_id else '新会话 ' + agent.session_id}（{reason}）"
        )
        if keyword and not remainder:
            if resume_id:
                out.put(TextDelta("😉好的，我们接着上次聊。"))
            elif reason == "no_record":
                out.put(TextDelta("😶没有找到之前的对话，我们重新开始吧。"))
            out.put(Done(session_id=agent.session_id))
            return True
        if keyword and reason == "no_record":
            out.put(TextDelta("😶没有找到之前的对话，我们重新开始吧。"))
        return False

    async def _run_turn(self, st: ConnState, text: str, system_prompt: str, out: queue.Queue) -> None:
        if st.lock is None:
            st.lock = asyncio.Lock()
        async with st.lock:
            st.turn_gen += 1
            if st.idle_handle:
                st.idle_handle.cancel()
                st.idle_handle = None
            try:
                if await self._ensure_agent(st, text, out, system_prompt):
                    self.store.touch(st.key, st.agent.session_id)
                    return
                async for ev in st.agent.send(text):
                    out.put(ev)
                    if isinstance(ev, Error) and ev.kind == ErrorKind.PROCESS_EXITED:
                        st.agent = None
                if st.agent:
                    self.store.touch(st.key, st.agent.session_id)
            except AgentSessionError as e:
                out.put(Error(e.kind, e.detail))
            except Exception as e:
                logger.bind(tag=TAG).exception(f"Claude Code 处理出错: {e}")
                out.put(Error(ErrorKind.UNKNOWN, str(e)))
            finally:
                if st.agent and not st.closed:
                    self._schedule_idle_close(st)

    # ---------- 服务端调用的同步接口 ----------

    def response(self, session_id, dialogue, **kwargs):
        text = next((m.get("content") for m in reversed(dialogue) if m.get("role") == "user"), "") or ""
        system_prompt = dialogue[0].get("content", "") if dialogue and dialogue[0].get("role") == "system" else ""
        st = self._state(session_id)
        self._warn_intent(st)

        out: queue.Queue = queue.Queue()
        asyncio.run_coroutine_threadsafe(self._run_turn(st, text, system_prompt, out), self._loop)

        t0 = time.time()
        last_event_at = t0
        first_at = None
        filler_sent = False
        emotion_buf = ""          # 填充语之后，正式回复开头的几个字，用来找表情
        emotion_resent = False
        finished = False
        try:
            while True:
                try:
                    ev = out.get(timeout=0.2)
                except queue.Empty:
                    elapsed = time.time() - t0
                    if first_at is None and self.filler and not filler_sent and elapsed >= self.filler_delay:
                        filler_sent = True
                        yield f"😶{self.filler}"
                    stalled = time.time() - last_event_at >= self.stall_timeout
                    if elapsed >= self.turn_timeout or stalled:
                        kind = ErrorKind.STALLED if stalled else ErrorKind.TIMEOUT
                        logger.bind(tag=TAG).warning(
                            f"已中断本轮：{'连续 %d 秒没有任何响应' % int(self.stall_timeout) if stalled else '超过 %d 秒' % int(self.turn_timeout)}"
                        )
                        if st.agent:
                            asyncio.run_coroutine_threadsafe(st.agent.interrupt(), self._loop)
                            asyncio.run_coroutine_threadsafe(self._abandon_if_stuck(st, st.turn_gen), self._loop)
                        prefix = "" if (first_at is not None or filler_sent) else "😶"
                        yield prefix + Error(kind).voice_message
                        finished = True
                        return
                    continue
                last_event_at = time.time()
                if isinstance(ev, Activity):
                    continue
                if isinstance(ev, TextDelta):
                    if first_at is None:
                        first_at = time.time() - t0
                    if filler_sent and not emotion_resent:
                        # 原项目每轮只按第一段文字设一次表情，填充语会把表情锁成 😶。
                        # 正式回复开头带了表情时，再补发一次情绪消息，让设备换成真正的表情。
                        emotion_buf += ev.text
                        emoji = next((ch for ch in emotion_buf[:8] if ch in textUtils.EMOJI_MAP), None)
                        if emoji or len(emotion_buf) >= 8:
                            emotion_resent = True
                            if emoji:
                                self._send_emotion(st, emoji)
                    yield ev.text
                elif isinstance(ev, Error):
                    logger.bind(tag=TAG).error(f"Claude Code 错误 [{ev.kind.value}]: {ev.detail[:500]}")
                    prefix = "" if (first_at is not None or filler_sent) else "😶"
                    yield prefix + ev.voice_message
                    finished = True
                    return
                elif isinstance(ev, Done):
                    finished = True
                    logger.bind(tag=TAG).info(
                        f"Claude Code 本轮完成：首字 {first_at if first_at is None else round(first_at, 2)}s，"
                        f"总耗时 {time.time() - t0:.1f}s{'，已被打断' if ev.interrupted else ''}"
                    )
                    return
        finally:
            if not finished and st.agent:
                # 生成器被提前关闭（例如设备打断后服务端停止迭代），让 Claude 也停下来
                asyncio.run_coroutine_threadsafe(st.agent.interrupt(), self._loop)

    def _send_emotion(self, st: ConnState, emoji: str) -> None:
        conn = st.conn
        loop = getattr(conn, "loop", None)
        if conn is None or loop is None or not (getattr(conn, "features", None) or {}).get("emoji", True):
            return
        asyncio.run_coroutine_threadsafe(textUtils.get_emotion(conn, emoji), loop)
        logger.bind(tag=TAG).debug(f"填充语之后补发表情 {emoji}")

    def response_no_stream(self, system_prompt, user_prompt, **kwargs):
        logger.bind(tag=TAG).warning("claude_cli 不支持非流式调用（意图识别、记忆总结等），请为这些模块单独配置 API 模型")
        return ""

    def _warn_intent(self, st: ConnState) -> None:
        if st.intent_warned or st.conn is None:
            return
        intent_type = getattr(st.conn, "intent_type", None)
        if intent_type and intent_type != "nointent":
            logger.bind(tag=TAG).warning(f"使用 claude_cli 时 Intent 应设为 nointent，当前为 {intent_type}")
        st.intent_warned = True

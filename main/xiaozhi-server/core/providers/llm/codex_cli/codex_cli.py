"""codex_cli：让小智设备通过本机 OpenAI Codex CLI 对话。

`codex exec --json` 每轮一个子进程，不用 --full-auto / --yolo / dangerously-bypass。
计划阶段是只读沙箱；要删除、付款、部署、发消息或写入时，先等设备确认，再另起一个
workspace-write 进程。确认只认两件事：播放时设备发来的 abort（点屏幕 / 按键），
以及播放结束后说「确认」。超时算取消，不会自动同意。

按键或唤醒词打断不会杀掉这一轮，而是转到后台，做完用语音汇报。
整句说「停止」或「取消」才会杀掉。「继续」「接着上次聊」接上一个线程。
"""

import asyncio
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

from config.config_loader import get_project_dir
from config.logger import setup_logging
from core.agent import reports
from core.agent.agent_session import AgentSessionError, ConfirmRequest, Done, Error, ErrorKind, TextDelta
from core.agent.codex_safety import CODEX_REPORT_ID, classify_confirm_reply, match_continue_phrase, match_stop
from core.agent.codex_session import DEFAULT_MODEL, CodexSession, CodexSessionConfig, voice_for
from core.agent.session_store import SessionStore
from core.providers.llm.claude_cli.claude_cli import ConnState, LLMProvider as AgentProvider, _num

TAG = __name__
logger = setup_logging()

COMMON_CLI_PATHS = [
    "/opt/homebrew/bin/codex",
    "/usr/local/bin/codex",
    "~/.local/bin/codex",
]


def find_codex_cli(configured=None):
    """优先用配置的路径，其次 PATH，再其次 macOS 上常见的 Homebrew 位置。"""
    if configured:
        path = Path(os.path.expanduser(str(configured)))
        if path.exists():
            return str(path)
        # 配置的路径不存在（比如模板里的 Homebrew 路径）时，继续自动查找
    names = ["codex.exe", "codex"] if sys.platform == "win32" else ["codex"]
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    for candidate in COMMON_CLI_PATHS:
        path = Path(os.path.expanduser(candidate))
        if path.exists():
            return str(path)
    return None


def codex_cli_version(cli_path: str, timeout: float = 15) -> str:
    out = subprocess.run([cli_path, "--version"], capture_output=True, text=True, encoding="utf-8", timeout=timeout)
    lines = [line.strip() for line in (out.stdout or out.stderr or "").splitlines() if line.strip()]
    return lines[0] if lines else ""


def _sandbox(value, default: str) -> str:
    text = str(value or "").strip() or default
    if text not in ("read-only", "workspace-write"):
        logger.bind(tag=TAG).warning(f"沙箱 {text} 不能用，改成 {default}")
        return default
    return text


class LLMProvider(AgentProvider):
    AGENT_LABEL = "Codex"

    def __init__(self, config: dict):
        model = str(config.get("model") or DEFAULT_MODEL).strip() or DEFAULT_MODEL
        self.model_name = f"codex_cli:{model}"
        self.cli_path = find_codex_cli(config.get("cli_path"))
        workdir = Path(os.path.expanduser(config.get("workdir") or "~/xiaozhi-workspace"))
        workdir.mkdir(parents=True, exist_ok=True)
        self.session_config = CodexSessionConfig(
            cli_path=self.cli_path,
            model=model,
            workdir=str(workdir),
            sandbox_plan=_sandbox(config.get("sandbox_plan"), "read-only"),
            sandbox_execute=_sandbox(config.get("sandbox_execute"), "workspace-write"),
        )
        self._init_common(config)
        # 0 表示不自动取消，只能等用户说取消或停止。任何值都不会自动同意。
        self.confirm_timeout = _num(config, "confirm_timeout_seconds", 45) or float("inf")
        self.store = SessionStore(Path(get_project_dir()) / "data" / "agent_sessions.json", "codex_cli")
        self._start_loop()
        self._self_check()

    def _start_loop(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, args=(self._loop,), name="codex-cli-loop", daemon=True)
        self._thread.start()

    def _progress_gap(self, count: int) -> float:
        """第 1、2、3 次进度之间的间隔，默认 30、30、60 秒，所以大约在第 30、60、120 秒开口。"""
        if not self.progress_interval:
            return 0
        factors = (1, 1, 2)
        factor = factors[count] if count < len(factors) else 4
        return self.progress_interval * factor

    def _resume_match(self, text: str):
        if match_continue_phrase(text):
            return ""
        return super()._resume_match(text)

    def _open_session(self, resume_id, system_prompt: str):
        return CodexSession(
            self.session_config, resume_id=resume_id, system_prompt=system_prompt, logger=logger.bind(tag=TAG),
        )

    def _voice_error(self, kind: ErrorKind) -> str:
        return voice_for(kind)

    def _self_check(self) -> None:
        cfg = self.session_config
        if not self.cli_path:
            logger.bind(tag=TAG).error("未找到 codex 命令。请先安装 Codex CLI 并登录，或在配置里写 cli_path")
            return
        try:
            version = codex_cli_version(self.cli_path)
        except Exception as e:
            logger.bind(tag=TAG).error(f"无法运行 {self.cli_path} --version: {e}")
            return
        logger.bind(tag=TAG).info(
            f"Codex CLI: {version} ({self.cli_path})，模型 {cfg.model}，工作目录 {cfg.workdir}，"
            f"计划沙箱 {cfg.sandbox_plan}，执行沙箱 {cfg.sandbox_execute}"
        )

    def abort(self, conn) -> None:
        """按键、点屏幕或唤醒词：这一轮转到后台，不杀进程。确认进行中时不会进到这里。"""
        st = self._states.get(conn.session_id)
        if st is not None:
            st.backgrounded = True

    def handle_device_abort(self, conn) -> bool:
        """播放过程中设备发来的 abort（StackChan 点脸、开发板按键）。

        正在等确认时，这一下就是同意，返回 True，调用方不要再把它当成打断。
        唤醒词打断不会进到这里（abort 消息里带 wake 原因时由 abortHandle 排除）。
        """
        st = self._states.get(conn.session_id)
        if st is None or not self._claim_pending(st):
            return False
        logger.bind(tag=TAG).info(f"设备 {st.key} 用按键或点屏幕确认了 Codex 操作")
        asyncio.run_coroutine_threadsafe(self._execute_and_report(st), self._loop)
        return True

    def _busy(self, st: ConnState):
        for attr in ("bg_task", "exec_task"):
            task = getattr(st, attr, None)
            if task is not None and not task.done():
                return task
        return None

    def _claim_pending(self, st: ConnState) -> bool:
        pending = getattr(st, "pending_confirm", None)
        if pending is None or pending.claimed:
            return False
        if pending.deadline is not None and time.time() >= pending.deadline:
            st.pending_confirm = None
            return False
        pending.claimed = True
        st.pending_confirm = None
        timer = getattr(st, "confirm_timer", None)
        if timer is not None:
            timer.cancel()
            st.confirm_timer = None
        return True

    def _clear_pending(self, st: ConnState) -> None:
        st.pending_confirm = None
        timer = getattr(st, "confirm_timer", None)
        if timer is not None:
            timer.cancel()
            st.confirm_timer = None

    def _arm_confirm(self, st: ConnState, event: ConfirmRequest) -> None:
        token = uuid.uuid4().hex
        deadline = None if self.confirm_timeout == float("inf") else time.time() + self.confirm_timeout
        st.pending_confirm = SimpleNamespace(
            token=token, prompt=event.prompt, summary=event.summary, reasons=event.reasons,
            deadline=deadline, claimed=False,
        )
        if deadline is None:
            return

        def fire(tok=token):
            asyncio.ensure_future(self._expire_confirm(st, tok))

        st.confirm_timer = self._loop.call_later(self.confirm_timeout, fire)

    async def _expire_confirm(self, st: ConnState, token: str) -> None:
        pending = getattr(st, "pending_confirm", None)
        if pending is None or pending.token != token or pending.claimed:
            return
        st.pending_confirm = None
        st.confirm_timer = None
        logger.bind(tag=TAG).info(f"设备 {st.key} 确认超时，已取消")
        reports.add(st.key, CODEX_REPORT_ID, "😶没等到确认，已取消。")

    def _watch_progress(self, st: ConnState, still_running) -> None:
        if not self.progress_interval or not self.progress_reply:
            return
        token = uuid.uuid4().hex
        st.progress_token = token
        started = time.time()
        state = {"count": 0}

        def fire():
            if getattr(st, "progress_token", None) != token or not still_running():
                return
            count = state["count"]
            if count == 0:
                text = self.progress_reply
            else:
                text = f"{self.progress_prefix}，已经 {self._elapsed(time.time() - started)}了。"
            state["count"] = count + 1
            reports.add(st.key, CODEX_REPORT_ID, "😶" + text.lstrip("😶"))
            if state["count"] < 8:
                st.progress_timer = self._loop.call_later(self._progress_gap(state["count"]), fire)

        st.progress_timer = self._loop.call_later(self._progress_gap(0), fire)

    @staticmethod
    def _elapsed(seconds: float) -> str:
        from core.providers.llm.claude_cli.claude_cli import elapsed_text
        return elapsed_text(seconds)

    def _stop_progress(self, st: ConnState) -> None:
        st.progress_token = None
        timer = getattr(st, "progress_timer", None)
        if timer is not None:
            timer.cancel()
            st.progress_timer = None

    async def _kill_running(self, st: ConnState) -> None:
        st.suppress_report = True
        self._stop_progress(st)
        if st.agent is not None:
            await st.agent.interrupt()
        for attr in ("bg_task", "exec_task"):
            task = getattr(st, attr, None)
            if task is not None and not task.done():
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout=3)
                except asyncio.TimeoutError:
                    task.cancel()

    async def _close_agent(self, st: ConnState, reason: str) -> None:
        if self._busy(st):
            if st.agent is not None:
                st.agent.mark_background()
            logger.bind(tag=TAG).info(f"设备 {st.key} {reason}，Codex 转到后台，做完再汇报")
            if st.idle_handle:
                st.idle_handle.cancel()
                st.idle_handle = None
            self._watch_progress(st, lambda: self._busy(st) is not None)
            return
        await super()._close_agent(st, reason)

    async def _run_turn(self, st: ConnState, text: str, system_prompt: str, out) -> None:
        if st.lock is None:
            st.lock = asyncio.Lock()
        # 停止不能排在正在跑的那一轮锁后面，否则要等它自己结束才杀得掉。
        if match_stop(text):
            await self._kill_running(st)
            async with st.lock:
                st.turn_gen += 1
                st.turn_task = asyncio.current_task()
                self._clear_pending(st)
                if st.idle_handle:
                    st.idle_handle.cancel()
                    st.idle_handle = None
                out.put(TextDelta("😶好的，已停下。"))
                out.put(Done())
                if self._busy(st) is None and st.agent and not st.closed:
                    self._schedule_idle_close(st)
            return

        task = None
        detached = {"on": False}
        async with st.lock:
            st.turn_gen += 1
            st.turn_task = asyncio.current_task()
            st.backgrounded = False
            if st.idle_handle:
                st.idle_handle.cancel()
                st.idle_handle = None
            try:
                if await self._preamble(st, text, out):
                    return
                if await self._ensure_agent(st, text, out, system_prompt):
                    if st.agent and st.agent.session_id:
                        self.store.touch(st.key, st.agent.session_id)
                    return
                task = asyncio.ensure_future(self._drive(st, text, out, detached))
                st.bg_task = task
            except AgentSessionError as e:
                out.put(Error(e.kind, e.detail, voice=self._voice_error(e.kind)))
                return
            except asyncio.CancelledError:
                out.put(Done(interrupted=True))
                raise
            except Exception as e:
                logger.bind(tag=TAG).exception(f"{self.AGENT_LABEL} 处理出错: {e}")
                out.put(Error(ErrorKind.UNKNOWN, str(e), voice=self._voice_error(ErrorKind.UNKNOWN)))
                return
            finally:
                if task is None and self._busy(st) is None and st.agent and not st.closed:
                    self._schedule_idle_close(st)
        try:
            while not task.done():
                if st.backgrounded:
                    detached["on"] = True
                    if st.agent is not None:
                        st.agent.mark_background()
                    self._watch_progress(st, lambda: self._busy(st) is not None)
                    out.put(Done(interrupted=True))
                    return
                await asyncio.wait({task}, timeout=0.05)
        finally:
            async with st.lock:
                if self._busy(st) is None and st.agent and not st.closed:
                    self._schedule_idle_close(st)

    async def _preamble(self, st: ConnState, text: str, out) -> bool:
        """停止、后台还在跑、或对待确认的回答。处理完返回 True。"""
        if match_stop(text):
            await self._kill_running(st)
            self._clear_pending(st)
            out.put(TextDelta("😶好的，已停下。"))
            out.put(Done())
            return True
        if self._busy(st):
            out.put(TextDelta("😶上一个还在做，做完我告诉你。"))
            out.put(Done())
            return True
        pending = getattr(st, "pending_confirm", None)
        if pending is None:
            return False
        kind = classify_confirm_reply(text)
        if pending.deadline is not None and time.time() >= pending.deadline:
            self._clear_pending(st)
            out.put(TextDelta("😶没等到确认，已取消。"))
            if kind == "other":
                return False
            out.put(Done())
            return True
        if kind == "yes" and self._claim_pending(st):
            await self._speak_execute(st, out)
            return True
        if kind == "no":
            self._clear_pending(st)
            out.put(TextDelta("😶好的，已取消。"))
            out.put(Done())
            return True
        self._clear_pending(st)
        out.put(TextDelta("😶刚才的操作已取消。"))
        return False

    async def _speak_execute(self, st: ConnState, out) -> None:
        if not (st.agent and st.agent.alive):
            await self._ensure_agent(st, "确认", out, "")
        agent = st.agent
        if agent is None:
            out.put(Error(ErrorKind.PROCESS_EXITED, "会话不在了", voice=self._voice_error(ErrorKind.PROCESS_EXITED)))
            out.put(Done())
            return
        async for ev in agent.execute():
            out.put(ev)
            if isinstance(ev, Done) and agent.session_id:
                self.store.touch(st.key, agent.session_id)

    async def _execute_and_report(self, st: ConnState) -> None:
        """按键确认之后在后台执行，做完再念。不占用正在听的那一轮。"""
        if st.lock is None:
            st.lock = asyncio.Lock()
        async with st.lock:
            st.exec_task = asyncio.current_task()
            self._watch_progress(st, lambda: getattr(st, "exec_task", None) is asyncio.current_task())
            spoken = []
            try:
                agent = st.agent
                if agent is None or not agent.alive:
                    holder: queue.Queue = queue.Queue()
                    await self._ensure_agent(st, "确认", holder, "")
                    agent = st.agent
                if agent is None:
                    reports.add(st.key, CODEX_REPORT_ID, "😶这次没能接着做。")
                    return
                async for ev in agent.execute():
                    if isinstance(ev, TextDelta) and ev.text:
                        spoken.append(ev.text)
                    elif isinstance(ev, Error):
                        reports.add(st.key, CODEX_REPORT_ID, ev.voice_message)
                        return
                if getattr(st, "suppress_report", False):
                    return
                text = "".join(spoken).strip() or "🙂做完了。"
                reports.add(st.key, CODEX_REPORT_ID, text)
                if agent.session_id:
                    self.store.touch(st.key, agent.session_id)
            except AgentSessionError as e:
                reports.add(st.key, CODEX_REPORT_ID, self._voice_error(e.kind))
            except Exception as e:
                logger.bind(tag=TAG).exception(f"确认后执行失败: {e}")
                reports.add(st.key, CODEX_REPORT_ID, self._voice_error(ErrorKind.UNKNOWN))
            finally:
                self._stop_progress(st)
                st.suppress_report = False
                if getattr(st, "exec_task", None) is asyncio.current_task():
                    st.exec_task = None
                if self._busy(st) is None and st.agent and not st.closed:
                    self._schedule_idle_close(st)

    async def _drive(self, st: ConnState, text: str, out, detached: dict) -> None:
        agent = st.agent
        spoken = []
        try:
            if agent is None:
                out.put(Error(ErrorKind.PROCESS_EXITED, "没有会话", voice=self._voice_error(ErrorKind.PROCESS_EXITED)))
                return
            async for ev in agent.send(text):
                if st.backgrounded and not detached["on"]:
                    detached["on"] = True
                    agent.mark_background()
                    self._watch_progress(st, lambda: self._busy(st) is not None)
                if isinstance(ev, ConfirmRequest):
                    self._arm_confirm(st, ev)
                    if detached["on"] and not getattr(st, "suppress_report", False):
                        reports.add(st.key, CODEX_REPORT_ID, ev.prompt)
                    continue
                if isinstance(ev, TextDelta) and ev.text:
                    spoken.append(ev.text)
                if not detached["on"]:
                    out.put(ev)
            if getattr(agent, "resume_dropped", False):
                self.store.forget(st.key)
            if agent.session_id:
                self.store.touch(st.key, agent.session_id)
            if detached["on"] and not getattr(st, "pending_confirm", None) and not getattr(st, "suppress_report", False):
                said = "".join(spoken).strip()
                if said:
                    reports.add(st.key, CODEX_REPORT_ID, said)
        except AgentSessionError as e:
            err = Error(e.kind, e.detail, voice=self._voice_error(e.kind))
            if detached["on"] or st.backgrounded:
                reports.add(st.key, CODEX_REPORT_ID, err.voice_message)
            else:
                out.put(err)
        except asyncio.CancelledError:
            if not detached["on"]:
                out.put(Done(interrupted=True))
            raise
        except Exception as e:
            logger.bind(tag=TAG).exception(f"{self.AGENT_LABEL} 处理出错: {e}")
            if not detached["on"]:
                out.put(Error(ErrorKind.UNKNOWN, str(e), voice=self._voice_error(ErrorKind.UNKNOWN)))
        finally:
            self._stop_progress(st)
            if getattr(st, "bg_task", None) is asyncio.current_task():
                st.bg_task = None
            if self._busy(st) is None and st.agent and not st.closed and not (st.lock and st.lock.locked()):
                self._schedule_idle_close(st)

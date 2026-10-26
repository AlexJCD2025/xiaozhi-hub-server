"""本机 Codex CLI 会话。

每一轮是一次 `codex exec --json`（或 `codex exec resume <线程号> --json`）。
不使用 --full-auto、--yolo、dangerously-bypass。计划阶段只读沙箱；需要写入时另起一个进程，
沙箱换成 workspace-write，而且只 resume 我们自己从 thread.started 里记下的线程号。
"""

import asyncio
import os
import re
from dataclasses import dataclass, field
from typing import AsyncIterator, List, Optional

from core.agent.agent_session import (
    Activity,
    AgentEvent,
    AgentSession,
    AgentSessionError,
    ConfirmRequest,
    Done,
    Error,
    ErrorKind,
    TextDelta,
)
from core.agent.codex_events import parse_line
from core.agent.codex_safety import (
    assess_plan,
    confirmation_speech,
    looks_like_auth,
    looks_like_missing_session,
    redact,
    summarize_for_speech,
)
from core.utils.textUtils import EMOJI_MAP

THREAD_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{7,127}$")
ALLOWED_SANDBOX = ("read-only", "workspace-write")
FORBIDDEN_FLAGS = (
    "--full-auto",
    "--yolo",
    "--dangerously-bypass-approvals-and-sandbox",
    "danger-full-access",
)
DEFAULT_MODEL = "gpt-5.6-sol"

PLAN_HEADER = (
    "你在给一个语音设备回答。这一阶段只能只读查看，不要改文件、不要删除、不要付款、不要部署、不要发消息或邮件。"
    "如果后面需要做这些，只说明打算做什么，不要动手。"
    "最后用两三句口语回答，不要 Markdown、列表和代码。"
)
EXECUTE_PROMPT = (
    "用户已经在设备上明确确认。请只执行上一轮计划里那一件需要写入或操作的事，不要扩大范围，"
    "也不要额外删除、付款、部署或发送消息。做完用两三句口语说明结果，不要 Markdown。"
)

VOICE_FOR = {
    ErrorKind.NOT_INSTALLED: "电脑上没有找到 Codex，请先安装并登录。",
    ErrorKind.AUTH: "Codex 还没登录或者登录过期了，请在电脑上重新登录。",
    ErrorKind.RATE_LIMIT: "Codex 现在用量到上限了，稍后再试吧。",
    ErrorKind.BILLING: "Codex 的账户额度好像不够了，请检查一下账户。",
    ErrorKind.SERVER: "Codex 那边暂时有点问题，稍后再试吧。",
    ErrorKind.SESSION_MISSING: "没有找到之前的对话，我们重新开始吧。",
    ErrorKind.PROCESS_EXITED: "Codex 意外退出了，再说一次试试。",
    ErrorKind.TIMEOUT: "这件事做得太久，我先停下了。想接着做的话，跟我说继续就行。",
    ErrorKind.STALLED: "Codex 一直没有回应，我先停下了。稍后跟我说继续，就能接着做。",
    ErrorKind.UNKNOWN: "出了点小问题，再说一次试试。",
}


def voice_for(kind: ErrorKind) -> str:
    return VOICE_FOR.get(kind, VOICE_FOR[ErrorKind.UNKNOWN])


def valid_thread_id(value: Optional[str]) -> bool:
    return bool(value) and bool(THREAD_ID_RE.match(value))


@dataclass
class CodexSessionConfig:
    cli_path: Optional[str] = None
    model: str = DEFAULT_MODEL
    workdir: Optional[str] = None
    sandbox_plan: str = "read-only"
    sandbox_execute: str = "workspace-write"


@dataclass
class _Phase:
    commands: List[str] = field(default_factory=list)
    messages: List[str] = field(default_factory=list)
    file_changes: int = 0
    errors: List[str] = field(default_factory=list)
    missing: bool = False
    interrupted: bool = False


def build_exec_argv(cli: str, *, sandbox: str, model: str, workdir: Optional[str], prompt: str,
                    resume_id: Optional[str] = None) -> List[str]:
    """拼出 argv。线程号只能是调用方已经校验过的 id，提示词放在 `--` 后面，避免被当成参数。"""
    if sandbox not in ALLOWED_SANDBOX:
        raise AgentSessionError(ErrorKind.UNKNOWN, f"不允许的沙箱: {sandbox}")
    model = (model or DEFAULT_MODEL).strip()
    if not re.match(r"^[A-Za-z0-9._:-]+$", model):
        raise AgentSessionError(ErrorKind.UNKNOWN, "模型名不合法")
    if resume_id is not None and not valid_thread_id(resume_id):
        raise AgentSessionError(ErrorKind.SESSION_MISSING, "线程号无效")
    argv = [cli, "exec"]
    if resume_id:
        argv.append("resume")
    argv += ["--json", "--sandbox", sandbox, "--model", model, "--skip-git-repo-check"]
    if workdir:
        argv += ["-C", workdir]
    if resume_id:
        argv.append(resume_id)
    argv += ["--", prompt]
    for flag in FORBIDDEN_FLAGS:
        if flag in argv:
            raise AgentSessionError(ErrorKind.UNKNOWN, f"禁止的参数: {flag}")
    return argv


class CodexSession(AgentSession):
    def __init__(self, config: CodexSessionConfig, resume_id: Optional[str] = None, system_prompt: str = "",
                 logger=None, spawn=None):
        self.config = config
        self.resume_id = resume_id if valid_thread_id(resume_id) else None
        self.system_prompt = system_prompt or ""
        self.logger = logger
        self._spawn_impl = spawn
        self._thread_id = self.resume_id
        self._proc = None
        self._alive = False
        self._closed = False
        self._stop = False
        self._backgrounded = False
        self.resume_dropped = False  # 接续失败、已经改成新开时为 True，方便上层忘掉旧记录

    @property
    def session_id(self) -> Optional[str]:
        return self._thread_id

    @property
    def alive(self) -> bool:
        return self._alive and not self._closed

    def mark_background(self) -> None:
        """连接关掉或用户把这一轮转到后台时调用：close 不再杀掉还在跑的进程。"""
        self._backgrounded = True

    def _log(self, level: str, message: str) -> None:
        if self.logger:
            prefix = (self._thread_id or "new")[:8]
            getattr(self.logger, level)(f"[{prefix}] {redact(message)}")

    async def start(self) -> None:
        path = self.config.cli_path
        # 测试会注入 spawn，不要求本机真有 codex。正式运行必须能找到可执行文件。
        if not path or (self._spawn_impl is None and not os.path.exists(path)):
            raise AgentSessionError(ErrorKind.NOT_INSTALLED, "未找到 codex")
        if self.resume_id and not valid_thread_id(self.resume_id):
            raise AgentSessionError(ErrorKind.SESSION_MISSING, "保存的线程号不能用")
        self._thread_id = self.resume_id
        self._closed = False
        self._alive = True

    async def close(self) -> None:
        self._closed = True
        self._alive = False
        if not self._backgrounded:
            await self.interrupt()

    async def interrupt(self) -> None:
        """停掉当前这个子进程。线程号还留着，下一轮可以 resume。"""
        self._stop = True
        proc = self._proc
        if proc is None or proc.returncode is not None:
            return
        try:
            proc.terminate()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(proc.wait(), timeout=2)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass

    async def send(self, text: str) -> AsyncIterator[AgentEvent]:
        self._stop = False
        self.resume_dropped = False
        phase = _Phase()
        resume = self._thread_id
        async for ev in self._phase(self.config.sandbox_plan, self._plan_prompt(text), resume, phase):
            yield ev
            if isinstance(ev, Error):
                return
        if phase.missing and resume:
            self._log("warning", "接续失败，改为新开一轮")
            self._thread_id = None
            self.resume_id = None
            self.resume_dropped = True
            yield TextDelta("😶" + voice_for(ErrorKind.SESSION_MISSING))
            phase = _Phase()
            async for ev in self._phase(self.config.sandbox_plan, self._plan_prompt(text), None, phase):
                yield ev
                if isinstance(ev, Error):
                    return
        if phase.interrupted or self._stop:
            yield Done(session_id=self.session_id, interrupted=True)
            return
        decision = assess_plan(phase.commands, "\n".join(phase.messages), phase.file_changes)
        self._log(
            "info",
            f"计划结束：确认={decision.needs_confirmation} 原因={','.join(decision.reasons) or '无'} "
            f"只读命令 {len(decision.readonly_commands)} 条，写入命令 {len(decision.write_commands)} 条",
        )
        if not decision.needs_confirmation:
            yield TextDelta(self._spoken("\n".join(phase.messages) or "好了。"))
            yield Done(session_id=self.session_id)
            return
        prompt = confirmation_speech(decision, "\n".join(phase.messages))
        yield TextDelta(prompt)
        yield ConfirmRequest(prompt=prompt, summary=summarize_for_speech("\n".join(phase.messages)),
                             reasons=decision.reasons)
        yield Done(session_id=self.session_id)

    async def execute(self) -> AsyncIterator[AgentEvent]:
        """用户确认之后，另起一个有写入权限的进程。只用计划阶段记下的线程号。"""
        self._stop = False
        if not valid_thread_id(self._thread_id):
            yield Error(ErrorKind.SESSION_MISSING, "没有可恢复的线程号",
                        voice="没有拿到对话编号，这次先不执行。")
            return
        phase = _Phase()
        async for ev in self._phase(self.config.sandbox_execute, EXECUTE_PROMPT, self._thread_id, phase):
            yield ev
            if isinstance(ev, Error):
                return
        if phase.interrupted or self._stop:
            yield Done(session_id=self.session_id, interrupted=True)
            return
        yield TextDelta(self._spoken("\n".join(phase.messages) or "做完了。"))
        yield Done(session_id=self.session_id)

    def _plan_prompt(self, text: str) -> str:
        persona = redact((self.system_prompt or "").strip(), limit=500)
        extra = f"\n人设参考：{persona}" if persona else ""
        return f"{PLAN_HEADER}{extra}\n\n用户说：{text}"

    def _spoken(self, text: str) -> str:
        body = summarize_for_speech(text) or "好了。"
        if body[0] in EMOJI_MAP:
            return body
        return "🙂" + body

    def _log_argv(self, argv: List[str]) -> None:
        shown = []
        for arg in argv:
            if arg == "--":
                shown.append("--")
                shown.append(f"<提示词 {len(argv[-1])} 字>")
                break
            shown.append(arg)
        self._log("info", "启动 " + " ".join(shown))

    async def _spawn(self, argv: List[str]):
        if self._spawn_impl is not None:
            return await self._spawn_impl(argv)
        return await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.config.workdir or None,
        )

    async def _phase(self, sandbox: str, prompt: str, resume_id: Optional[str], phase: _Phase) -> AsyncIterator[AgentEvent]:
        if not self.config.cli_path:
            yield Error(ErrorKind.NOT_INSTALLED, "未找到 codex", voice=voice_for(ErrorKind.NOT_INSTALLED))
            return
        try:
            argv = build_exec_argv(
                self.config.cli_path, sandbox=sandbox, model=self.config.model,
                workdir=self.config.workdir, prompt=prompt, resume_id=resume_id,
            )
        except AgentSessionError as e:
            yield Error(e.kind, e.detail, voice=voice_for(e.kind))
            return
        self._log_argv(argv)
        try:
            proc = await self._spawn(argv)
        except FileNotFoundError as e:
            yield Error(ErrorKind.NOT_INSTALLED, str(e), voice=voice_for(ErrorKind.NOT_INSTALLED))
            return
        except OSError as e:
            yield Error(ErrorKind.PROCESS_EXITED, str(e), voice=voice_for(ErrorKind.PROCESS_EXITED))
            return
        self._proc = proc
        stderr_lines: List[str] = []
        returncode = None

        async def drain_stderr():
            if proc.stderr is None:
                return
            while True:
                raw = await proc.stderr.readline()
                if not raw:
                    break
                stderr_lines.append(raw.decode("utf-8", "replace").rstrip())
                del stderr_lines[:-40]

        drain = asyncio.create_task(drain_stderr())
        running = 0
        try:
            while True:
                if self._stop:
                    phase.interrupted = True
                    if proc.returncode is None:
                        try:
                            proc.terminate()
                        except ProcessLookupError:
                            pass
                    break
                if proc.stdout is None:
                    break
                raw = await proc.stdout.readline()
                if not raw:
                    break
                event = parse_line(raw.decode("utf-8", "replace"))
                if event is None:
                    continue
                if event.kind == "thread" and event.thread_id:
                    if valid_thread_id(event.thread_id):
                        self._thread_id = event.thread_id
                        self._log("info", f"线程 {event.thread_id}")
                    else:
                        self._log("warning", "忽略不合规则的线程号")
                    continue
                if event.kind == "command_started":
                    running += 1
                    self._log("info", f"命令开始（只记名称）：{_basename(event.command)}")
                    yield Activity("command", tools_running=running)
                    continue
                if event.kind == "command_finished":
                    running = max(0, running - 1)
                    if event.command:
                        phase.commands.append(event.command)
                    self._log("info", f"命令结束 exit={event.exit_code} {_basename(event.command)}")
                    yield Activity("command", tools_running=running)
                    continue
                if event.kind == "message" and event.text:
                    phase.messages.append(event.text)
                    yield Activity("message", tools_running=running)
                    continue
                if event.kind == "file_change":
                    phase.file_changes += 1
                    yield Activity("file_change", tools_running=running)
                    continue
                if event.kind in ("error", "turn_failed"):
                    phase.errors.append(event.message or event.kind)
                    self._log("warning", f"Codex 报错：{event.message}")
                    continue
                if event.kind == "turn_started":
                    yield Activity("turn", tools_running=running)
                    continue
                if event.kind != "turn_completed":
                    yield Activity(event.raw_type or "event", tools_running=running)
            try:
                returncode = await proc.wait()
            except ProcessLookupError:
                returncode = proc.returncode
        finally:
            drain.cancel()
            try:
                await drain
            except (asyncio.CancelledError, Exception):
                pass
            if proc.returncode is None:
                try:
                    proc.terminate()
                except ProcessLookupError:
                    pass
            if self._proc is proc:
                self._proc = None
        if phase.interrupted or self._stop:
            phase.interrupted = True
            return
        detail = "\n".join(phase.errors + stderr_lines[-8:])
        if looks_like_auth(detail):
            yield Error(ErrorKind.AUTH, redact(detail), voice=voice_for(ErrorKind.AUTH))
            return
        if resume_id and looks_like_missing_session(detail):
            phase.missing = True
            return
        if returncode not in (0, None):
            self._log("warning", f"进程退出码 {returncode}：{detail}")
            yield Error(ErrorKind.PROCESS_EXITED, redact(detail or f"exit {returncode}"),
                        voice=voice_for(ErrorKind.PROCESS_EXITED))


def _basename(command: str) -> str:
    text = (command or "").strip()
    if not text:
        return ""
    first = text.split()[0]
    return os.path.basename(first)[:40]

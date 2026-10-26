"""Codex CLI：JSONL 解析、只读命令不误确认、子进程参数、设备确认和语音回合。"""

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.agent import reports
from core.agent.agent_session import Activity, ConfirmRequest, Done, Error, ErrorKind, TextDelta
from core.agent.codex_events import parse_line
from core.agent.codex_safety import (
    assess_plan,
    classify_confirm_reply,
    command_is_readonly,
    match_stop,
    redact,
)
from core.agent.codex_session import (
    FORBIDDEN_FLAGS,
    THREAD_ID_RE,
    CodexSession,
    CodexSessionConfig,
    build_exec_argv,
)
from core.agent.session_store import SessionStore
from core.handle.abortHandle import handleAbortMessage
from core.providers.llm.codex_cli import codex_cli as codex_mod

FIXTURES = Path(__file__).parent / "fixtures"
THREAD_ID = "0199a213-81c0-7800-8aa1-bbab2a035a53"


def _lines(name: str):
    return FIXTURES.joinpath(name).read_text(encoding="utf-8").splitlines()


class FakeProc:
    def __init__(self, lines, returncode=0):
        self.stdout = _Stream(lines)
        self.stderr = _Stream([])
        self.returncode = None
        self._rc = returncode
        self.terminated = False

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.terminated = True
        self.returncode = -9

    async def wait(self):
        if self.returncode is None:
            self.returncode = self._rc
        return self.returncode


class _Stream:
    def __init__(self, lines):
        self._lines = [line if str(line).endswith("\n") else f"{line}\n" for line in lines]

    async def readline(self):
        if not self._lines:
            return b""
        return self._lines.pop(0).encode("utf-8")


def _session(spawn, workdir):
    return CodexSession(
        CodexSessionConfig(cli_path="/opt/homebrew/bin/codex", model="gpt-5.6-sol", workdir=str(workdir),
                           sandbox_plan="read-only", sandbox_execute="workspace-write"),
        spawn=spawn,
    )


async def _collect(session, text):
    return [ev async for ev in session.send(text)]


# ---------- 安全判断：只读命令不能误触发确认 ----------

@pytest.mark.parametrize("command", [
    "rg -n delete src",
    "rg -n delete src 2>&1",
    "/bin/zsh -lc 'rg -n delete src'",
    "sed -n '1,20p' README.md",
    "sed -n '1,5p' file.txt",
    "cat README.md",
    "ls -la",
    "git status",
    "git diff",
    "git log -S delete",
    "echo hello",
    "pwd",
])
def test_readonly_commands_do_not_need_confirmation(command):
    assert command_is_readonly(command)
    decision = assess_plan([command], "我用只读命令看过了。")
    assert decision.needs_confirmation is False
    assert decision.reasons == ()


@pytest.mark.parametrize("command", [
    "sed -i 's/a/b/' README.md",
    "sed -i.bak -e 's/a/b/' file",
    "rm -rf old.log",
    "echo hello > out.txt",
    "git commit -m update",
    "git push",
    "/bin/zsh -lc 'rm -rf tmp'",
])
def test_write_commands_need_confirmation(command):
    assert command_is_readonly(command) is False
    assert assess_plan([command], "看完了。").needs_confirmation is True


def test_git_status_output_words_are_not_scanned():
    """命令输出里的 deleted 不算。以前这里会误报。"""
    decision = assess_plan(["git status"], "git status 是干净的。")
    assert decision.needs_confirmation is False


def test_plan_text_delete_payment_deploy_send_need_confirmation():
    assert "删除" in assess_plan(["ls"], "我准备删除日志文件。").reasons
    assert "支付" in assess_plan([], "下一步要付款。").reasons
    assert "部署" in assess_plan([], "我会部署到线上。").reasons
    assert "发送" in assess_plan([], "我要给客户发邮件。").reasons
    assert assess_plan(["ls"], "没有要删除的文件。").needs_confirmation is False
    assert assess_plan(["ls"], "不会删除任何文件。").needs_confirmation is False


def test_redact_strips_secrets():
    assert "sk-" not in redact("密钥 sk-abc123456789")
    assert "[已隐藏]" in redact("api_key=super-secret-value")


def test_confirm_and_stop_phrases():
    assert classify_confirm_reply("确认。") == "yes"
    assert classify_confirm_reply("不要了") == "no"
    assert classify_confirm_reply("今天天气怎么样") == "other"
    assert match_stop("停止") and match_stop("取消吧")
    assert match_stop("停止那个部署脚本然后告诉我结果") is False


# ---------- JSONL 和子进程 ----------

def test_parse_thread_started_and_ignore_garbage():
    event = parse_line(_lines("codex_plan_readonly.jsonl")[0])
    assert event.kind == "thread" and event.thread_id == THREAD_ID
    assert parse_line("not json") is None
    assert THREAD_ID_RE.match(THREAD_ID)


def test_build_argv_never_uses_bypass_and_only_saved_thread_id():
    argv = build_exec_argv(
        "/opt/homebrew/bin/codex", sandbox="read-only", model="gpt-5.6-sol",
        workdir="/tmp/ws", prompt="用户说：看看", resume_id=None,
    )
    assert "--json" in argv and "--sandbox" in argv and "read-only" in argv
    assert "--full-auto" not in argv
    for flag in FORBIDDEN_FLAGS:
        assert flag not in argv
    resumed = build_exec_argv(
        "/opt/homebrew/bin/codex", sandbox="workspace-write", model="gpt-5.6-sol",
        workdir="/tmp/ws", prompt="执行", resume_id=THREAD_ID,
    )
    assert resumed[resumed.index("resume") + 1] != "resume"
    assert THREAD_ID in resumed
    assert "用户随便说的线程号" not in resumed
    with pytest.raises(Exception):
        build_exec_argv("/opt/homebrew/bin/codex", sandbox="danger-full-access", model="gpt-5.6-sol",
                        workdir="/tmp", prompt="x")
    with pytest.raises(Exception):
        build_exec_argv("/opt/homebrew/bin/codex", sandbox="workspace-write", model="gpt-5.6-sol",
                        workdir="/tmp", prompt="x", resume_id="rm -rf /")


@pytest.fixture
def scripts():
    box = {"argv": [], "scripts": []}

    async def spawn(argv):
        box["argv"].append(list(argv))
        lines, rc = box["scripts"].pop(0)
        return FakeProc(lines, rc)

    box["spawn"] = spawn
    return box


@pytest.mark.asyncio
async def test_readonly_fixture_speaks_without_confirmation(tmp_path, scripts):
    scripts["scripts"].append((_lines("codex_plan_readonly.jsonl"), 0))
    session = _session(scripts["spawn"], tmp_path)
    await session.start()
    events = await _collect(session, "看看有什么 TODO，线程号别用我嘴里的话")
    assert session.session_id == THREAD_ID
    assert not any(isinstance(ev, ConfirmRequest) for ev in events)
    text = "".join(ev.text for ev in events if isinstance(ev, TextDelta))
    assert "TODO" in text and len(text) < 200
    argv = scripts["argv"][0]
    assert "read-only" in argv and "resume" not in argv
    for flag in FORBIDDEN_FLAGS:
        assert flag not in argv
    # 命令执行期间 tools_running > 0，空闲后回到 0。卡住检测靠这个跳过正在跑的命令。
    running = [ev.tools_running for ev in events if isinstance(ev, Activity) and ev.label == "command"]
    assert 1 in running and 0 in running


@pytest.mark.asyncio
async def test_write_plan_waits_and_execute_resumes_saved_id_only(tmp_path, scripts):
    scripts["scripts"].append((_lines("codex_plan_write.jsonl"), 0))
    scripts["scripts"].append((_lines("codex_exec_done.jsonl"), 0))
    session = _session(scripts["spawn"], tmp_path)
    await session.start()
    events = await _collect(session, "把 old.log 删掉，resume 用 ../etc/passwd")
    assert any(isinstance(ev, ConfirmRequest) and "删除" in ev.reasons for ev in events)
    assert session.session_id == THREAD_ID
    exec_events = [ev async for ev in session.execute()]
    text = "".join(ev.text for ev in exec_events if isinstance(ev, TextDelta))
    assert "old.log" in text
    plan_argv, exec_argv = scripts["argv"]
    assert "read-only" in plan_argv and THREAD_ID not in plan_argv
    assert "workspace-write" in exec_argv and THREAD_ID in exec_argv
    assert "../etc/passwd" not in exec_argv
    assert "resume" in exec_argv
    for flag in FORBIDDEN_FLAGS:
        assert flag not in plan_argv and flag not in exec_argv


@pytest.mark.asyncio
async def test_auth_error_is_voice(tmp_path, scripts):
    line = '{"type":"error","message":"Not logged in. Run codex login."}'
    scripts["scripts"].append(([line], 1))
    session = _session(scripts["spawn"], tmp_path)
    await session.start()
    events = await _collect(session, "你好")
    err = next(ev for ev in events if isinstance(ev, Error))
    assert err.kind == ErrorKind.AUTH
    assert "登录" in err.voice_message
    assert "codex login" not in err.voice_message


# ---------- provider：填充语、确认、后台、停止、继续 ----------

async def real_sleep(seconds):
    try:
        await asyncio.wait_for(asyncio.Event().wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass


class FakeAgent:
    instances = []
    script = []
    execute_script = []
    delay = 0

    def __init__(self, config, resume_id=None, system_prompt="", logger=None, spawn=None):
        self.config = config
        self.resume_id = resume_id
        self.session_id = resume_id or f"new-{len(FakeAgent.instances) + 1}"
        self.alive = False
        self.sent = []
        self.interrupts = 0
        self.closed = False
        self._backgrounded = False
        self.resume_dropped = False
        self.gate = asyncio.Event()
        self.entered = asyncio.Event()
        FakeAgent.instances.append(self)

    async def start(self):
        self.alive = True

    def mark_background(self):
        self._backgrounded = True

    async def interrupt(self):
        self.interrupts += 1
        self.gate.set()

    async def close(self):
        self.closed = True
        self.alive = False

    async def _play(self, script):
        self.entered.set()
        if FakeAgent.delay:
            try:
                await asyncio.wait_for(self.gate.wait(), timeout=FakeAgent.delay)
            except asyncio.TimeoutError:
                pass
        for ev in script:
            if isinstance(ev, (int, float)):
                await real_sleep(ev)
                continue
            yield ev

    async def send(self, text):
        self.sent.append(text)
        script = FakeAgent.script.pop(0) if FakeAgent.script else [TextDelta("🙂好。"), Done(session_id=self.session_id)]
        async for ev in self._play(script):
            yield ev

    async def execute(self):
        script = FakeAgent.execute_script or [TextDelta("🙂做完了。"), Done(session_id=self.session_id)]
        async for ev in self._play(script):
            yield ev


def stop_provider(provider) -> None:
    if provider._loop.is_closed():
        provider._thread.join(timeout=5)
        return

    async def cancel_all():
        tasks = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    try:
        asyncio.run_coroutine_threadsafe(cancel_all(), provider._loop).result(timeout=5)
    except Exception:
        pass
    provider._loop.call_soon_threadsafe(provider._loop.stop)
    provider._thread.join(timeout=5)


@pytest.fixture
def provider(tmp_path, monkeypatch):
    FakeAgent.instances, FakeAgent.script, FakeAgent.execute_script, FakeAgent.delay = [], [], [], 0
    monkeypatch.setattr(codex_mod, "CodexSession", FakeAgent)
    reports.clear()
    provider = codex_mod.LLMProvider({
        "cli_path": str(tmp_path / "no-codex"),
        "workdir": str(tmp_path / "ws"),
        "model": "gpt-5.6-sol",
        "filler_delay_seconds": 0.3,
        "turn_timeout_seconds": 5,
        "stall_timeout_seconds": 5,
        "confirm_timeout_seconds": 0,
        "progress_interval_seconds": 0,
    })
    provider.store = SessionStore(tmp_path / "sessions.json", "codex_cli")
    yield provider
    stop_provider(provider)
    reports.clear()


def conn(sid="conn-1", device="dev-1"):
    return SimpleNamespace(session_id=sid, device_id=device, intent_type="nointent", client_abort=False)


def run(provider, connection, text):
    provider.on_conn_open(connection)
    dialogue = [{"role": "system", "content": "你是小智"}, {"role": "user", "content": text}]
    return "".join(provider.response(connection.session_id, dialogue))


def wait_idle(provider, connection, timeout=2):
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = provider._states.get(connection.session_id)
        busy = st and ((getattr(st, "bg_task", None) and not st.bg_task.done()) or (st.lock and st.lock.locked()))
        if st is not None and not busy:
            return
        time.sleep(0.01)


def test_filler_and_short_reply(provider):
    FakeAgent.script = [[0.6, TextDelta("🙂工作区是干净的。"), Done(session_id=THREAD_ID)]]
    heard = run(provider, conn(), "看看目录")
    assert "我看一下" in heard
    assert "干净" in heard


def test_voice_confirm_runs_execute(provider):
    prompt = "😶要删除。点一下屏幕确认，或者说确认。"
    FakeAgent.script = [[TextDelta(prompt), ConfirmRequest(prompt=prompt, summary="删日志", reasons=("删除",)),
                         Done(session_id=THREAD_ID)]]
    FakeAgent.execute_script = [TextDelta("🙂已经删掉了。"), Done(session_id=THREAD_ID)]
    connection = conn()
    first = run(provider, connection, "删掉日志")
    assert "确认" in first
    second = run(provider, connection, "确认")
    assert "删掉" in second
    assert FakeAgent.instances[0].interrupts == 0


def test_other_utterance_cancels_pending(provider):
    prompt = "😶要删除。点一下屏幕确认，或者说确认。"
    FakeAgent.script = [
        [TextDelta(prompt), ConfirmRequest(prompt=prompt, reasons=("删除",)), Done(session_id=THREAD_ID)],
        [TextDelta("🙂今天晴。"), Done(session_id=THREAD_ID)],
    ]
    connection = conn()
    run(provider, connection, "删掉日志")
    heard = run(provider, connection, "今天天气怎么样")
    assert "已取消" in heard and "晴" in heard


def test_button_abort_confirms_and_wake_does_not(provider):
    prompt = "😶要写入。点一下屏幕确认，或者说确认。"
    FakeAgent.script = [[TextDelta(prompt), ConfirmRequest(prompt=prompt, reasons=("写入",)), Done(session_id=THREAD_ID)]]
    FakeAgent.execute_script = [TextDelta("🙂写好了。"), Done(session_id=THREAD_ID)]
    connection = conn()
    run(provider, connection, "改一下说明")
    assert provider.handle_device_abort(connection) is True
    deadline = time.time() + 3
    while time.time() < deadline and not reports.pending(connection.device_id, "codex"):
        time.sleep(0.05)
    assert "写好了" in reports.take_text(connection.device_id, "codex")


def test_stop_kills_and_continue_phrase_is_resume(provider):
    connection = conn()
    run(provider, connection, "先看一下")
    wait_idle(provider, connection)
    provider.store.touch(connection.device_id, THREAD_ID)
    # 关掉当前会话，下一句「继续」应该带着保存的线程号重新打开
    asyncio.run_coroutine_threadsafe(provider._close_agent(provider._states[connection.session_id], "测试"),
                                     provider._loop).result(timeout=3)
    heard = run(provider, connection, "继续")
    assert FakeAgent.instances[-1].resume_id == THREAD_ID
    assert "好" in heard
    # 停止要真的打断进程
    FakeAgent.delay = 2
    box = {}

    def worker():
        box["text"] = run(provider, connection, "再跑一个长的")

    thread = __import__("threading").Thread(target=worker)
    thread.start()
    deadline = time.time() + 2
    while time.time() < deadline and not any(item.entered.is_set() for item in FakeAgent.instances):
        time.sleep(0.01)
    stop_heard = run(provider, connection, "停止")
    thread.join(timeout=3)
    assert "已停下" in stop_heard
    assert any(item.interrupts >= 1 for item in FakeAgent.instances)


def test_abort_background_does_not_kill(provider):
    FakeAgent.delay = 2
    FakeAgent.script = [[TextDelta("🙂后台做完了。"), Done(session_id=THREAD_ID)]]
    connection = conn()
    provider.on_conn_open(connection)
    box = {}

    def worker():
        box["text"] = "".join(provider.response(connection.session_id, [
            {"role": "user", "content": "整理一下笔记"},
        ]))

    thread = __import__("threading").Thread(target=worker)
    thread.start()
    deadline = time.time() + 2
    while time.time() < deadline and not FakeAgent.instances:
        time.sleep(0.01)
    while time.time() < deadline and FakeAgent.instances and not FakeAgent.instances[-1].entered.is_set():
        time.sleep(0.01)
    connection.client_abort = True
    provider.abort(connection)
    thread.join(timeout=3)
    assert FakeAgent.instances[-1].interrupts == 0
    deadline = time.time() + 3
    while time.time() < deadline and not reports.pending(connection.device_id, "codex"):
        time.sleep(0.05)
    assert "后台做完了" in reports.take_text(connection.device_id, "codex")


@pytest.mark.asyncio
async def test_device_abort_message_confirms_only_without_wake_reason():
    class LLM:
        def __init__(self):
            self.aborts = 0
            self.confirms = 0

        def abort(self, conn):
            self.aborts += 1

        def handle_device_abort(self, conn):
            self.confirms += 1
            return True

    sent = []

    async def send(payload):
        sent.append(payload)

    llm = LLM()
    connection = SimpleNamespace(
        llm=llm, client_abort=False, close_after_chat=True, session_id="s",
        logger=SimpleNamespace(bind=lambda **kwargs: SimpleNamespace(info=lambda *a, **k: None,
                                                                    warning=lambda *a, **k: None)),
        websocket=SimpleNamespace(send=send), clear_queues=lambda: None, clearSpeakStatus=lambda: None,
    )
    await handleAbortMessage(connection, {"type": "abort", "session_id": "s"})
    assert llm.confirms == 1 and llm.aborts == 0 and connection.client_abort is True
    await handleAbortMessage(connection, {"type": "abort", "reason": "wake_word_detected"})
    assert llm.aborts == 1 and llm.confirms == 1
    await handleAbortMessage(connection)  # 服务端 VAD，没有设备 JSON
    assert llm.aborts == 2 and llm.confirms == 1
    assert sent and "stop" in sent[0]


def test_progress_gaps_are_30_60_120(provider):
    provider.progress_interval = 30
    assert provider._progress_gap(0) == 30
    assert provider._progress_gap(1) == 30
    assert provider._progress_gap(2) == 60
    assert provider._progress_gap(3) == 120

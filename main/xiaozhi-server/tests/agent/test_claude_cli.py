"""claude_cli provider 与 Claude 会话层的单元测试。用假的 SDK 消息和假的会话，不需要真实账号。"""

import asyncio
import time
from types import SimpleNamespace

import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, StreamEvent, TextBlock, ToolUseBlock

from core.agent import claude_session as cs_mod
from core.agent.agent_session import AgentSessionError, Done, Error, ErrorKind, TextDelta
from core.agent.claude_session import ClaudeSession, ClaudeSessionConfig
from core.agent.session_store import SessionStore, choose_resume
from core.agent.voice_commands import wants_resume
from core.providers.llm.claude_cli import claude_cli as provider_mod


# ---------- 假 SDK 消息 ----------

def delta(text, parent=None):
    return StreamEvent(uuid="u", session_id="s", parent_tool_use_id=parent,
                       event={"type": "content_block_delta", "delta": {"type": "text_delta", "text": text}})


def assistant(*blocks, model="claude-haiku", error=None, parent=None):
    return AssistantMessage(content=list(blocks), model=model, error=error, parent_tool_use_id=parent)


def result(is_error=False, subtype="success", sid="sid-1"):
    return ResultMessage(subtype=subtype, duration_ms=1, duration_api_ms=1, is_error=is_error,
                         num_turns=1, session_id=sid)


class FakeClient:
    def __init__(self, messages):
        self.messages = messages
        self.queries = []
        self.interrupted = False

    async def query(self, text):
        self.queries.append(text)

    async def receive_response(self):
        for m in self.messages:
            yield m

    async def interrupt(self):
        self.interrupted = True


async def collect(session, text="你好"):
    return [ev async for ev in session.send(text)]


def session_with(messages):
    s = ClaudeSession(ClaudeSessionConfig())
    s._client = FakeClient(messages)
    return s


# ---------- ClaudeSession 输出解析 ----------

async def test_stream_deltas_dedupe_full_message():
    s = session_with([delta("🙂你"), delta("好"), assistant(TextBlock("🙂你好")), result()])
    evs = await collect(s)
    assert [e.text for e in evs if isinstance(e, TextDelta)] == ["🙂你", "好"]  # 整条消息不再重复
    assert isinstance(evs[-1], Done) and evs[-1].session_id == "sid-1"


async def test_full_message_used_when_no_stream():
    s = session_with([assistant(TextBlock("🙂好的")), result()])
    assert [e.text for e in await collect(s) if isinstance(e, TextDelta)] == ["🙂好的"]


async def test_tool_turn_inserts_sentence_break_and_skips_tool_blocks():
    s = session_with([
        delta("🤔我先看一下"), assistant(TextBlock("🤔我先看一下"), ToolUseBlock(id="t", name="Glob", input={})),
        delta("目录里有两个文件。"), assistant(TextBlock("目录里有两个文件。")), result()])
    texts = "".join(e.text for e in await collect(s) if isinstance(e, TextDelta))
    assert texts == "🤔我先看一下。目录里有两个文件。"


async def test_subagent_output_is_ignored():
    s = session_with([delta("子代理的话", parent="tool-1"), delta("主回复"), result()])
    assert [e.text for e in await collect(s) if isinstance(e, TextDelta)] == ["主回复"]


async def test_synthetic_error_message_not_spoken():
    s = session_with([assistant(TextBlock("API Error: rate limit"), model="<synthetic>", error="rate_limit"),
                      result()])
    evs = await collect(s)
    assert not [e for e in evs if isinstance(e, TextDelta)]
    assert isinstance(evs[0], Error) and evs[0].kind == ErrorKind.RATE_LIMIT


async def test_interrupted_result_is_done_not_error():
    s = session_with([delta("🙂一"), result(is_error=True, subtype="error_during_execution")])
    s._interrupting = True  # 模拟 send 过程中被打断：send 开头会重置，这里改由 FakeClient 设置
    client = s._client

    async def receive():
        yield delta("🙂一")
        await s.interrupt()
        yield result(is_error=True, subtype="error_during_execution")
    client.receive_response = receive
    evs = await collect(s)
    assert isinstance(evs[-1], Done) and evs[-1].interrupted
    assert client.interrupted


async def test_error_result_without_interrupt():
    s = session_with([result(is_error=True, subtype="error_during_execution")])
    assert isinstance((await collect(s))[-1], Error)


def test_options_restrict_tools_and_new_session_id():
    s = ClaudeSession(ClaudeSessionConfig(allowed_tools=["Read"], system_prompt_append="规则"))
    o = s._options()
    assert o.tools == ["Read"] and o.allowed_tools == ["Read"]
    assert o.session_id == s.session_id and o.resume is None
    assert o.system_prompt["append"] == "规则" and o.setting_sources == []
    r = ClaudeSession(ClaudeSessionConfig(), resume_id="old")._options()
    assert r.resume == "old"


async def test_can_use_tool_denies_outside_whitelist():
    o = ClaudeSession(ClaudeSessionConfig(allowed_tools=["Read"]))._options()
    assert type(await o.can_use_tool("Read", {}, None)).__name__ == "PermissionResultAllow"
    assert type(await o.can_use_tool("Bash", {}, None)).__name__ == "PermissionResultDeny"


# ---------- 会话存储与接续策略、关键字 ----------

def test_session_store_roundtrip(tmp_path):
    st = SessionStore(tmp_path / "s.json", "claude_cli")
    assert st.get("dev") is None
    st.touch("dev", "sid", now=100)
    assert st.get("dev") == {"session_id": "sid", "last_active": 100}
    st.forget("dev")
    assert st.get("dev") is None


def test_choose_resume():
    rec = {"session_id": "sid", "last_active": 1000}
    assert choose_resume(rec, False, 120, now=1000 + 60) == ("sid", "window")
    assert choose_resume(rec, False, 120, now=1000 + 3 * 3600) == (None, "expired")
    assert choose_resume(rec, True, 120, now=1000 + 30 * 86400) == ("sid", "keyword")
    assert choose_resume(None, True, 120) == (None, "no_record")
    assert choose_resume(None, False, 120) == (None, "new")


def test_wants_resume():
    assert wants_resume("嗯，接着上次聊吧！")
    assert wants_resume("我们 继续刚才的话题")
    assert not wants_resume("上次那个文件在哪")


# ---------- provider ----------

async def real_sleep(seconds):
    # conftest 把 asyncio.sleep 替换成了立即返回，这里用事件循环的定时器真正等待
    try:
        await asyncio.wait_for(asyncio.Event().wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass


class FakeAgent:
    """替代 ClaudeSession：按脚本产出事件。"""
    instances = []
    script = []        # 每轮的事件列表
    start_error = None  # 首次 start 抛出的错误
    delay = 0.0

    def __init__(self, config, resume_id=None, logger=None):
        self.config, self.resume_id = config, resume_id
        self.session_id = resume_id or f"new-{len(FakeAgent.instances)}"
        self.alive = False
        self.sent, self.interrupts, self.closed = [], 0, False
        FakeAgent.instances.append(self)

    async def start(self):
        if FakeAgent.start_error and self.resume_id:
            raise FakeAgent.start_error
        self.alive = True

    async def send(self, text):
        self.sent.append(text)
        if FakeAgent.delay:
            await real_sleep(FakeAgent.delay)
        for ev in (FakeAgent.script.pop(0) if FakeAgent.script else [TextDelta("🙂好"), Done()]):
            yield ev

    async def interrupt(self):
        self.interrupts += 1

    async def close(self):
        self.closed = True
        self.alive = False


@pytest.fixture
def provider(tmp_path, monkeypatch):
    FakeAgent.instances, FakeAgent.script, FakeAgent.start_error, FakeAgent.delay = [], [], None, 0.0
    monkeypatch.setattr(provider_mod, "ClaudeSession", FakeAgent)
    p = provider_mod.LLMProvider({"cli_path": str(tmp_path / "no-claude"), "workdir": str(tmp_path / "ws"),
                                  "filler_delay_seconds": 0.3, "turn_timeout_seconds": 2})
    p.store = SessionStore(tmp_path / "sessions.json", "claude_cli")
    yield p
    p._loop.call_soon_threadsafe(p._loop.stop)


def conn(sid="conn-1", device="dev-1", intent="nointent"):
    return SimpleNamespace(session_id=sid, device_id=device, intent_type=intent)


def run(p, c, text, system="你是小智"):
    return "".join(p.response(c.session_id, [{"role": "system", "content": system},
                                             {"role": "user", "content": text}]))


def test_provider_reuses_one_process_across_turns(provider):
    c = conn()
    provider.on_conn_open(c)
    assert run(provider, c, "第一句") == "🙂好"
    assert run(provider, c, "第二句") == "🙂好"
    assert len(FakeAgent.instances) == 1
    agent = FakeAgent.instances[0]
    assert agent.sent == ["第一句", "第二句"]
    assert "你是小智" in agent.config.system_prompt_append and "语音助手规则" in agent.config.system_prompt_append
    assert provider.store.get("dev-1")["session_id"] == agent.session_id


def test_provider_resumes_within_window_after_reconnect(provider):
    provider.store.touch("dev-1", "old-sid")
    c = conn()
    provider.on_conn_open(c)
    run(provider, c, "你好")
    assert FakeAgent.instances[0].resume_id == "old-sid"


def test_provider_starts_new_when_window_expired(provider):
    provider.store.touch("dev-1", "old-sid", now=time.time() - 10 * 3600)
    c = conn()
    provider.on_conn_open(c)
    run(provider, c, "你好")
    assert FakeAgent.instances[0].resume_id is None


def test_resume_keyword_resumes_old_session_without_sending(provider):
    provider.store.touch("dev-1", "old-sid", now=time.time() - 10 * 3600)
    c = conn()
    provider.on_conn_open(c)
    out = run(provider, c, "接着上次聊")
    assert "接着上次聊" in out
    agent = FakeAgent.instances[0]
    assert agent.resume_id == "old-sid" and agent.sent == []


def test_resume_keyword_without_history_says_so(provider):
    c = conn()
    provider.on_conn_open(c)
    out = run(provider, c, "接着上次聊")
    assert "没有找到之前的对话" in out and FakeAgent.instances[0].sent == []


def test_resume_failure_falls_back_to_new(provider):
    FakeAgent.start_error = AgentSessionError(ErrorKind.SESSION_MISSING, "gone")
    provider.store.touch("dev-1", "old-sid")
    c = conn()
    provider.on_conn_open(c)
    assert run(provider, c, "你好") == "🙂好"
    assert [a.resume_id for a in FakeAgent.instances] == ["old-sid", None]


def test_filler_when_slow(provider):
    FakeAgent.delay = 0.8
    c = conn()
    provider.on_conn_open(c)
    out = run(provider, c, "查个东西")
    assert out.startswith("😶好的，我看一下。") and out.endswith("🙂好")


def test_error_is_spoken_friendly(provider):
    FakeAgent.script = [[Error(ErrorKind.AUTH, "401 raw details")]]
    c = conn()
    provider.on_conn_open(c)
    out = run(provider, c, "你好")
    assert "登录" in out and "401" not in out


def test_turn_timeout_interrupts(provider):
    FakeAgent.delay = 5
    provider.filler = ""
    c = conn()
    provider.on_conn_open(c)
    out = run(provider, c, "很久的任务")
    assert "有点久" in out
    deadline = time.time() + 2
    while FakeAgent.instances[0].interrupts == 0 and time.time() < deadline:
        time.sleep(0.05)
    assert FakeAgent.instances[0].interrupts >= 1


def test_abort_and_close_hooks(provider):
    c = conn()
    provider.on_conn_open(c)
    run(provider, c, "你好")
    agent = FakeAgent.instances[0]
    provider.abort(c)
    provider.on_conn_close(c)
    deadline = time.time() + 2
    while not agent.closed and time.time() < deadline:
        time.sleep(0.05)
    assert agent.interrupts == 1 and agent.closed
    assert "conn-1" not in provider._states


def test_early_generator_close_interrupts(provider):
    FakeAgent.script = [[TextDelta("🙂一"), TextDelta("二"), Done()]]
    FakeAgent.delay = 0
    c = conn()
    provider.on_conn_open(c)
    gen = provider.response(c.session_id, [{"role": "user", "content": "数数"}])
    assert next(gen) == "🙂一"
    gen.close()  # 服务端因打断停止迭代
    deadline = time.time() + 2
    while FakeAgent.instances[0].interrupts == 0 and time.time() < deadline:
        time.sleep(0.05)
    assert FakeAgent.instances[0].interrupts == 1


def test_idle_timeout_closes_process(provider):
    provider.idle_timeout = 0.3
    c = conn()
    provider.on_conn_open(c)
    run(provider, c, "你好")
    agent = FakeAgent.instances[0]
    deadline = time.time() + 2
    while not agent.closed and time.time() < deadline:
        time.sleep(0.05)
    assert agent.closed

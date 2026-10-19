"""claude_cli provider 与 Claude 会话层的单元测试。用假的 SDK 消息和假的会话，不需要真实账号。"""

import asyncio
import threading
import time
from types import SimpleNamespace

import pytest
from claude_agent_sdk import (AssistantMessage, ResultMessage, StreamEvent, TextBlock, ToolResultBlock, ToolUseBlock,
                              UserMessage)

from core.agent import claude_session as cs_mod
from core.agent.agent_session import AgentSessionError, Done, Error, ErrorKind, TextDelta
from core.agent.claude_session import ClaudeSession, ClaudeSessionConfig
from core.agent.session_store import SessionStore, choose_resume
from core.agent.voice_commands import match_resume
from core.providers.llm.claude_cli import claude_cli as provider_mod


def wants_resume(text, keywords=None):
    return match_resume(text, keywords) is not None


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
    errors = [e for e in evs if isinstance(e, Error)]
    assert errors and errors[0].kind == ErrorKind.RATE_LIMIT


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


async def test_activity_reported_for_non_text_events_and_throttled():
    s = session_with([assistant(ToolUseBlock(id="t", name="Glob", input={"pattern": "*"})),
                      assistant(TextBlock("先看看")), assistant(TextBlock("再看看")), delta("好"), result()])
    evs = await collect(s)
    acts = [e for e in evs if type(e).__name__ == "Activity"]
    assert len(acts) == 1  # 1 秒内的多个非文字事件只上报一次
    assert any(isinstance(e, TextDelta) and e.text == "好" for e in evs)


async def test_transition_sentence_closed_before_tool():
    """过渡语没有句末标点就去调用工具时补一个句号，TTS 才会马上念出来，不用等工具跑完。"""
    s = session_with([delta("🙂我先看一下文件"), assistant(TextBlock("🙂我先看一下文件"), ToolUseBlock(id="t", name="Read", input={})),
                      delta("看完了。"), result()])
    texts = [e.text for e in await collect(s) if isinstance(e, TextDelta)]
    assert texts == ["🙂我先看一下文件", "。", "看完了。"]  # 只补一次，下一段话前不再重复补
    s = session_with([delta("🙂我看看："), assistant(ToolUseBlock(id="t", name="Read", input={})), result()])
    assert [e.text for e in await collect(s) if isinstance(e, TextDelta)] == ["🙂我看看：", "。"]
    s = session_with([delta("🙂我看看。"), assistant(ToolUseBlock(id="t", name="Read", input={})), result()])
    assert [e.text for e in await collect(s) if isinstance(e, TextDelta)] == ["🙂我看看。"]  # 已经有句号就不补


async def test_activity_reports_running_tools_immediately():
    """正在执行的工具数一变就上报（不受 1 秒节流限制），调用方据此判断"在跑工具"还是"卡住"。"""
    s = session_with([assistant(ToolUseBlock(id="t1", name="Bash", input={"command": "make"})),
                      assistant(ToolUseBlock(id="t2", name="Read", input={})),
                      UserMessage(content=[ToolResultBlock(tool_use_id="t1", content="ok")]),
                      UserMessage(content=[ToolResultBlock(tool_use_id="t2", content="ok")]),
                      assistant(ToolUseBlock(id="sub", name="Grep", input={}), parent="t9"),  # 子代理的不算
                      delta("好"), result()])
    evs = await collect(s)
    assert [e.tools_running for e in evs if type(e).__name__ == "Activity"] == [1, 2, 1, 0]


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
    assert r.resume == "old" and r.effort is None
    assert ClaudeSession(ClaudeSessionConfig(effort="low"))._options().effort == "low"


async def test_can_use_tool_denies_outside_whitelist():
    o = ClaudeSession(ClaudeSessionConfig(allowed_tools=["Read"]))._options()
    assert type(await o.can_use_tool("Read", {}, None)).__name__ == "PermissionResultAllow"
    assert type(await o.can_use_tool("Bash", {}, None)).__name__ == "PermissionResultDeny"


async def test_tool_guard_blocks_paths_outside_workdir(tmp_path):
    """工作目录之外的文件：权限回调和 PreToolUse 钩子都要拒绝（实测读文件不会经过权限回调）。"""
    o = ClaudeSession(ClaudeSessionConfig(allowed_tools=["Read", "Write"], workdir=str(tmp_path)))._options()
    name = lambda r: type(r).__name__
    assert name(await o.can_use_tool("Read", {"file_path": str(tmp_path / "a.txt")}, None)) == "PermissionResultAllow"
    assert name(await o.can_use_tool("Read", {"file_path": "notes/a.txt"}, None)) == "PermissionResultAllow"
    assert name(await o.can_use_tool("Read", {"file_path": "/etc/hosts"}, None)) == "PermissionResultDeny"
    assert name(await o.can_use_tool("Write", {"file_path": "../escape.txt"}, None)) == "PermissionResultDeny"
    hook = o.hooks["PreToolUse"][0].hooks[0]
    denied = await hook({"tool_name": "Read", "tool_input": {"file_path": "/etc/hosts"}}, "t1", None)
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert await hook({"tool_name": "Read", "tool_input": {"file_path": "a.txt"}}, "t2", None) == {}
    denied = await hook({"tool_name": "mcp__claude_ai_Gmail__send", "tool_input": {}}, "t3", None)
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"  # 列表之外的工具
    assert o.strict_mcp_config is True  # 不加载账号里的 claude.ai 连接器


def test_glob_pattern_cannot_escape_workdir(tmp_path):
    """Glob 的 pattern 本身能指定位置，也要限制在工作目录里；Grep 的 pattern 是正则，不当路径检查。"""
    from core.agent.claude_session import outside_workdir

    root = str(tmp_path)
    escapes = ["/etc/*", "~/.ssh/*", "../*", "src/../../*", "{/etc,.}/*", "{., ~/.ssh}/*"]
    for pattern in escapes:
        assert outside_workdir({"pattern": pattern}, root, "Glob") == pattern, pattern
    for pattern in ["**/*.py", "*.md", "notes/**/a?.txt", f"{root}/**/*.py"]:
        assert outside_workdir({"pattern": pattern}, root, "Glob") is None, pattern
    assert outside_workdir({"pattern": "/api/v1"}, root, "Grep") is None  # 正则表达式，不是路径
    assert outside_workdir({"pattern": "*.py", "path": "/etc"}, root, "Glob") == "/etc"


async def test_glob_guard_in_hook(tmp_path):
    o = ClaudeSession(ClaudeSessionConfig(allowed_tools=["Glob"], workdir=str(tmp_path)))._options()
    hook = o.hooks["PreToolUse"][0].hooks[0]
    denied = await hook({"tool_name": "Glob", "tool_input": {"pattern": "/Users/*/.ssh/*"}}, "t1", None)
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert await hook({"tool_name": "Glob", "tool_input": {"pattern": "**/*.txt"}}, "t2", None) == {}


async def test_auto_mode_denies_unapproved_risky_tools(tmp_path):
    """Auto 模式下走到人工审批的操作：只读工具放行，其余拒绝（没人能点同意）。"""
    o = ClaudeSession(ClaudeSessionConfig(allowed_tools=["Read", "Bash", "WebSearch"], workdir=str(tmp_path),
                                          permission_mode="auto"))._options()
    assert o.permission_mode == "auto"
    assert type(await o.can_use_tool("Read", {"file_path": "a.txt"}, None)).__name__ == "PermissionResultAllow"
    assert type(await o.can_use_tool("WebSearch", {"query": "x"}, None)).__name__ == "PermissionResultAllow"
    assert type(await o.can_use_tool("Bash", {"command": "ls"}, None)).__name__ == "PermissionResultDeny"


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
    assert match_resume("嗯，接着上次聊吧！") is not None
    assert match_resume("我们 继续刚才的话题") is not None
    assert match_resume("上次那个文件在哪") is None


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
            if isinstance(ev, (int, float)):
                await real_sleep(ev)  # 脚本里的数字表示停顿几秒
                continue
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
    if not p._loop.is_closed():  # 退役测试里循环已经自己停了
        p._loop.call_soon_threadsafe(p._loop.stop)


def conn(sid="conn-1", device="dev-1", intent="nointent"):
    return SimpleNamespace(session_id=sid, device_id=device, intent_type=intent)


def run(p, c, text, system="你是小智"):
    return "".join(p.response(c.session_id, [{"role": "system", "content": system},
                                             {"role": "user", "content": text}]))


def test_effort_defaults_to_low(provider, tmp_path):
    assert provider.session_config.effort == "low"
    p = provider_mod.LLMProvider({"cli_path": str(tmp_path / "x"), "workdir": str(tmp_path / "w"), "effort": ""})
    assert p.session_config.effort is None  # 留空表示用 Claude Code 默认值
    p._loop.call_soon_threadsafe(p._loop.stop)


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


# ---------- 热加载后旧实例退役 ----------

def wait_thread_exit(p, timeout=3.0):
    p._thread.join(timeout)
    return not p._thread.is_alive()


def test_shutdown_without_connections_stops_thread(provider):
    assert provider._thread.is_alive()
    provider.shutdown()
    assert wait_thread_exit(provider)
    assert provider._loop.is_closed()


def test_shutdown_waits_for_open_connections(provider):
    c = conn()
    provider.on_conn_open(c)
    assert run(provider, c, "第一句") == "🙂好"
    provider.shutdown()
    assert not wait_thread_exit(provider, timeout=0.3)  # 还有连接：线程保留，不打断对话
    assert run(provider, c, "第二句") == "🙂好"  # 已连接的设备继续用旧实例
    assert FakeAgent.instances[0].sent == ["第一句", "第二句"]
    provider.on_conn_close(c)
    assert wait_thread_exit(provider)
    assert FakeAgent.instances[0].closed  # 退出前先关掉 Claude Code 进程


def test_shutdown_waits_for_turn_still_running(provider):
    FakeAgent.delay = 0.6
    c = conn()
    provider.on_conn_open(c)
    gen = provider.response(c.session_id, [{"role": "user", "content": "慢一点"}])
    assert next(gen).startswith("😶")  # 0.3 秒后先出填充语，这一轮还在 Claude Code 里跑
    provider.shutdown()
    provider.on_conn_close(c)
    assert provider._thread.is_alive()
    assert "".join(gen) == "🙂好"
    assert wait_thread_exit(provider)
    assert FakeAgent.instances[0].closed


def test_connection_arriving_after_stop_gets_new_loop(provider):
    provider.shutdown()
    assert wait_thread_exit(provider)
    c = conn()
    provider.on_conn_open(c)  # 替换瞬间连上旧实例的设备仍然能用
    assert run(provider, c, "你好") == "🙂好"
    provider.on_conn_close(c)
    assert wait_thread_exit(provider)


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


def test_resume_keyword_with_more_content_is_sent_to_claude(provider):
    provider.store.touch("dev-1", "old-sid", now=time.time() - 10 * 3600)
    c = conn()
    provider.on_conn_open(c)
    out = run(provider, c, "接着上次聊那个 bug 吧")
    agent = FakeAgent.instances[0]
    # 接续旧会话，并且整句话照常发给 Claude，后半句不会被吞掉
    assert agent.resume_id == "old-sid" and agent.sent == ["接着上次聊那个 bug 吧"]
    assert out == "🙂好"


def test_resume_keyword_with_content_but_no_history(provider):
    c = conn()
    provider.on_conn_open(c)
    out = run(provider, c, "接着上次聊那个 bug")
    assert out.startswith("😶没有找到之前的对话") and out.endswith("🙂好")
    assert FakeAgent.instances[0].sent == ["接着上次聊那个 bug"]


def test_match_resume_remainder():
    from core.agent.voice_commands import match_resume
    assert match_resume("嗯，接着上次聊吧！") == ""
    assert match_resume("帮我接着上次聊") == ""
    assert match_resume("接着上次量。") == ""
    assert match_resume("接着上次聊那个bug") == "那个bug"
    assert match_resume("继续刚才的话题，那个函数改好了吗") == "那个函数改好"
    assert match_resume("别接着上次聊了") is None
    assert match_resume("今天天气怎么样") is None


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
    assert out.startswith("😶好的 我看一下。") and out.endswith("🙂好")


def test_single_sentence():
    assert provider_mod.single_sentence("好的，我看一下") == "好的 我看一下。"
    assert provider_mod.single_sentence("稍等~马上好！") == "稍等 马上好。"
    assert provider_mod.single_sentence("") == ""


def test_emotion_resent_after_filler(provider, monkeypatch):
    sent = []

    async def fake_get_emotion(conn, text):
        sent.append(text)
    monkeypatch.setattr(provider_mod.textUtils, "get_emotion", fake_get_emotion)
    FakeAgent.delay = 0.8
    FakeAgent.script = [[TextDelta("😆哈"), TextDelta("哈"), Done()]]
    c = conn()
    c.loop = provider._loop          # 测试里借用 provider 的事件循环执行
    c.features = {}
    provider.on_conn_open(c)
    out = run(provider, c, "讲个笑话")
    assert out.startswith("😶好的 我看一下。")
    deadline = time.time() + 2
    while not sent and time.time() < deadline:
        time.sleep(0.05)
    assert sent == ["😆"]


def test_no_emotion_resent_without_filler(provider, monkeypatch):
    sent = []

    async def fake_get_emotion(conn, text):
        sent.append(text)
    monkeypatch.setattr(provider_mod.textUtils, "get_emotion", fake_get_emotion)
    FakeAgent.script = [[TextDelta("😆哈哈"), Done()]]
    c = conn()
    c.loop = provider._loop
    provider.on_conn_open(c)
    run(provider, c, "讲个笑话")
    time.sleep(0.2)
    assert sent == []  # 没有填充语时，第一段文字的表情由原项目自己处理


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
    assert "做得太久" in out and "说继续" in out
    deadline = time.time() + 2
    while FakeAgent.instances[0].interrupts == 0 and time.time() < deadline:
        time.sleep(0.05)
    assert FakeAgent.instances[0].interrupts >= 1


def test_stall_timeout_interrupts_when_no_events(provider):
    FakeAgent.delay = 5          # 5 秒内没有任何事件
    provider.filler = ""
    provider.stall_timeout = 0.5
    provider.turn_timeout = 30
    c = conn()
    provider.on_conn_open(c)
    out = run(provider, c, "你好")
    assert "没有回应" in out and out.startswith("😶")
    deadline = time.time() + 2
    while FakeAgent.instances[0].interrupts == 0 and time.time() < deadline:
        time.sleep(0.05)
    assert FakeAgent.instances[0].interrupts >= 1


def test_activity_keeps_turn_alive(provider):
    from core.agent.agent_session import Activity
    provider.stall_timeout = 0.5
    FakeAgent.script = [[Activity("tool")] * 1 + [TextDelta("🙂查完了"), Done()]]
    FakeAgent.delay = 0.3       # 每段等待都短于无响应超时
    c = conn()
    provider.on_conn_open(c)
    assert run(provider, c, "查一下").endswith("🙂查完了")


def test_running_tool_is_not_a_stall(provider):
    """工具执行期间（比如一条很慢的命令）很久没有事件，不算卡住，只受单轮超时限制。"""
    from core.agent.agent_session import Activity
    provider.filler, provider.stall_timeout, provider.turn_timeout = "", 0.3, 5
    FakeAgent.script = [[Activity("AssistantMessage", tools_running=1), 1.0,
                         Activity("UserMessage", tools_running=0), TextDelta("🙂编译好了"), Done()]]
    c = conn()
    provider.on_conn_open(c)
    assert run(provider, c, "编译一下") == "🙂编译好了"


def test_long_turn_keeps_connection_alive(provider):
    """干活期间不断刷新连接的活动时间，否则服务端会把"没有任何动静"的连接兜底断开，进程也跟着关。"""
    provider.filler, provider.stall_timeout, provider.turn_timeout = "", 5, 5
    FakeAgent.script = [[1.0, TextDelta("🙂好了"), Done()]]
    c = conn()
    c.last_activity_time = 0
    provider.on_conn_open(c)
    t0 = time.time() * 1000
    run(provider, c, "干个活")
    assert c.last_activity_time >= t0 + 800


def test_progress_reply_when_silent_too_long(provider):
    provider.filler, provider.stall_timeout, provider.turn_timeout = "好的我看一下。", 5, 5
    provider.filler_delay, provider.progress_interval = 0.1, 0.5
    FakeAgent.script = [[1.8, TextDelta("🙂做完了"), Done()]]
    c = conn()
    provider.on_conn_open(c)
    out = run(provider, c, "干个活")
    assert out.startswith("😶好的我看一下。") and out.endswith("🙂做完了")
    assert out.count(provider.progress_reply) >= 2  # 填充语之后，每 0.5 秒没开口说一次


def test_progress_not_inserted_mid_sentence(provider):
    provider.filler, provider.stall_timeout, provider.turn_timeout, provider.progress_interval = "", 5, 5, 0.3
    FakeAgent.script = [[TextDelta("🙂我先看看那个"), 0.8, TextDelta("文件。"), Done()]]
    c = conn()
    provider.on_conn_open(c)
    assert run(provider, c, "看看") == "🙂我先看看那个文件。"  # 半句话中间不插进度


def test_session_recorded_as_soon_as_claude_starts(provider):
    """新会话的第一轮超时卡住时，会话也已经记下：进程被强制关闭后，下一句话还能接回来。"""
    from core.agent.agent_session import Activity
    provider.filler, provider.turn_timeout, provider.stall_timeout = "", 0.5, 5
    FakeAgent.script = [[Activity("SystemMessage"), 3.0, Done()]]  # 开始处理后一直不结束
    c = conn()
    provider.on_conn_open(c)
    out = run(provider, c, "整理一下文件")
    assert "说继续" in out  # 超时提示告诉用户怎么接着做
    assert provider.store.get("dev-1")["session_id"] == "new-0"


def test_stuck_turn_does_not_block_next_sentence(provider):
    """Claude Code 卡死：中断无效、关闭进程也没让那一轮退出时，要取消它，否则下一句话一直排队。"""
    from core.agent.agent_session import Activity
    provider.filler, provider.turn_timeout, provider.stall_timeout, provider.abandon_grace = "", 0.5, 30, 0.3
    FakeAgent.script = [[Activity("x"), 60.0, Done()], [TextDelta("🙂接着做完了"), Done()]]  # 第一轮卡住不动
    c = conn()
    provider.on_conn_open(c)
    assert "说继续" in run(provider, c, "做个长任务")
    time.sleep(0.6)  # 等强制关闭
    provider.turn_timeout = 3
    t0 = time.time()
    assert run(provider, c, "继续").endswith("🙂接着做完了")
    assert time.time() - t0 < 2
    assert FakeAgent.instances[0].closed and len(FakeAgent.instances) == 2  # 卡死的进程关掉，重新启动了一个


def test_notice_not_glued_to_half_sentence(provider):
    """超时提示前如果 Claude 只说了半句，先补句号收住，不能和半句话粘成一句念。"""
    from core.agent.agent_session import Activity
    provider.filler, provider.turn_timeout, provider.stall_timeout = "", 0.5, 30
    FakeAgent.script = [[TextDelta("🙂我先看一下文件"), Activity("x", tools_running=1), 5.0, Done()]]
    c = conn()
    provider.on_conn_open(c)
    assert run(provider, c, "看看").startswith("🙂我先看一下文件。这件事做得太久")


def test_session_not_recorded_when_first_event_is_error(provider):
    """一开始就出错（例如没登录）时不记，免得覆盖之前能接续的会话。"""
    provider.store.touch("dev-1", "old-session")
    FakeAgent.script = [[Error(ErrorKind.AUTH, "not logged in")]]
    c = conn()
    provider.on_conn_open(c)
    run(provider, c, "你好")
    assert provider.store.get("dev-1")["session_id"] == "old-session"


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


# ---------- 全量审查后补充的回归测试 ----------

def test_zero_values_are_respected(tmp_path):
    p = provider_mod.LLMProvider({"cli_path": str(tmp_path / "x"), "workdir": str(tmp_path / "w"),
                                  "resume_window_minutes": 0, "filler_delay_seconds": 0,
                                  "stall_timeout_seconds": 0, "turn_timeout_seconds": 0})
    try:
        assert p.resume_window == 0 and p.filler_delay == 0
        assert p.stall_timeout == float("inf") and p.turn_timeout == float("inf")  # 0 表示不限制
    finally:
        p._loop.call_soon_threadsafe(p._loop.stop)


def test_resume_window_zero_disables_auto_resume(provider):
    provider.resume_window = 0
    provider.store.touch("dev-1", "old-sid")
    c = conn()
    provider.on_conn_open(c)
    run(provider, c, "你好")
    assert FakeAgent.instances[0].resume_id is None


def test_resume_failure_keeps_record_when_fresh_start_also_fails(provider):
    class AlwaysFail(FakeAgent):
        async def start(self):
            raise AgentSessionError(ErrorKind.SESSION_MISSING if self.resume_id else ErrorKind.PROCESS_EXITED, "x")
    provider_mod.ClaudeSession = AlwaysFail
    provider.store.touch("dev-1", "old-sid")
    c = conn()
    provider.on_conn_open(c)
    out = run(provider, c, "你好")
    assert "意外退出" in out
    assert provider.store.get("dev-1")["session_id"] == "old-sid"  # 临时故障不删除可接续的会话


def test_connection_closed_during_start_does_not_leak_process(provider):
    c = conn()
    provider.on_conn_open(c)
    st = provider._states[c.session_id]

    class SlowStart(FakeAgent):
        async def start(self):
            provider.on_conn_close(c)  # 启动过程中连接被关闭
            await super().start()
    provider_mod.ClaudeSession = SlowStart
    run(provider, c, "你好")
    deadline = time.time() + 2
    while not FakeAgent.instances[0].closed and time.time() < deadline:
        time.sleep(0.05)
    assert FakeAgent.instances[0].closed and st.agent is None


def test_idle_timer_does_not_close_after_new_turn(provider):
    c = conn()
    provider.on_conn_open(c)
    run(provider, c, "第一句")
    st = provider._states[c.session_id]
    stale_gen = st.turn_gen
    run(provider, c, "第二句")  # 新的一轮让旧定时器失效
    fut = asyncio.run_coroutine_threadsafe(provider._idle_close(st, stale_gen), provider._loop)
    fut.result(timeout=2)
    assert not FakeAgent.instances[0].closed


def test_stuck_turn_is_abandoned(provider):
    FakeAgent.delay = 5          # 中断无效：这一轮一直不结束
    provider.filler = ""
    provider.stall_timeout = 0.5
    c = conn()
    provider.on_conn_open(c)
    out = run(provider, c, "你好")
    assert "没有回应" in out
    deadline = time.time() + 3
    while not FakeAgent.instances[0].closed and time.time() < deadline:
        time.sleep(0.05)
    assert FakeAgent.instances[0].closed  # 卡死的进程被强制关闭，下一轮会重新启动


def test_abort_closes_stuck_turn(provider):
    FakeAgent.delay = 5          # 设备打断后 interrupt 无效：这一轮一直不结束
    provider.filler = ""
    c = conn()
    provider.on_conn_open(c)
    threading.Thread(target=run, args=(provider, c, "你好"), daemon=True).start()
    st = provider._states[c.session_id]
    deadline = time.time() + 2
    while not (st.agent and st.lock and st.lock.locked()) and time.time() < deadline:
        time.sleep(0.05)
    provider.abort(c)
    agent = FakeAgent.instances[0]
    deadline = time.time() + 2
    while not agent.closed and time.time() < deadline:
        time.sleep(0.05)
    assert agent.interrupts == 1 and agent.closed


def test_abort_after_turn_keeps_process(provider):
    c = conn()
    provider.on_conn_open(c)
    run(provider, c, "你好")
    provider.abort(c)
    time.sleep(0.3)
    agent = FakeAgent.instances[0]
    assert agent.interrupts == 1 and not agent.closed  # 这一轮已经结束，不该关进程


class ErrorClient(FakeClient):
    def __init__(self):
        super().__init__([])
        self.disconnected = False

    async def receive_response(self):
        from claude_agent_sdk import ClaudeSDKError
        raise ClaudeSDKError("bad json")
        yield  # pragma: no cover

    async def disconnect(self):
        self.disconnected = True


async def test_sdk_error_closes_process():
    s = ClaudeSession(ClaudeSessionConfig())
    client = ErrorClient()
    s._client = client
    evs = await collect(s)
    assert isinstance(evs[-1], Error) and evs[-1].kind == ErrorKind.PROCESS_EXITED
    assert client.disconnected and s._client is None


async def test_stream_event_with_null_delta():
    ev = StreamEvent(uuid="u", session_id="s", event={"type": "content_block_delta", "delta": None})
    s = session_with([ev, delta("好"), result()])
    assert [e.text for e in await collect(s) if isinstance(e, TextDelta)] == ["好"]


def test_claude_cli_version_parsing(tmp_path):
    script = tmp_path / "fake-claude"
    script.write_text("#!/bin/sh\nprintf '\\n  \\n2.1.302 (Claude Code)\\n'\n")
    script.chmod(0o755)
    assert provider_mod.claude_cli_version(str(script)) == "2.1.302 (Claude Code)"
    empty = tmp_path / "empty-claude"
    empty.write_text("#!/bin/sh\nprintf '\\n'\n")
    empty.chmod(0o755)
    assert provider_mod.claude_cli_version(str(empty)) == ""


def test_wants_resume_ignores_negation():
    assert not wants_resume("别接着上次聊了，换个话题")
    assert not wants_resume("不要继续刚才的话题")
    assert wants_resume("别管了，接着上次聊吧")  # 否定词不紧挨关键字


def test_wants_resume_tolerates_one_wrong_char():
    # 真机录音：SenseVoice 把"接着上次聊"识别成了"接着上次量"
    assert wants_resume("接着上次量。")
    assert wants_resume("嗯，继续刚才的话提")
    assert not wants_resume("接着上次聊", keywords=["接着上次聊吧"])  # 只容忍错字，不容忍少字
    assert not wants_resume("别接着上次量了")  # 否定词判断同样适用
    assert not wants_resume("接着说上次量")  # 错 2 个字以上不算
    assert not wants_resume("上次那个文件在哪")
    assert wants_resume("嗯，我们接着上次量吧")  # 首尾的语气词不影响
    # 长句里只差 1 个字的片段多半是真问题：命中接续指令后这句话不会发给 Claude，不能误判
    assert not wants_resume("接着上次买的那个东西怎么用")
    assert not wants_resume("接着刚才写代码")
    # 4 个字以内的关键字必须完全一致，避免误触发
    assert wants_resume("接着聊", keywords=["接着聊"])
    assert not wants_resume("接着量", keywords=["接着聊"])

"""Grok Bot 客户端、事件流中枢、会话层和 provider 的单元测试。

用一个假的"云端"（同时扮演 api2.cursor.sh 和云端电脑的网关）跑完整流程，不需要真实账号。
真实账号的端到端实测记录见工作空间的 NOTES.md。
"""

import asyncio
import base64
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from core.agent import grokbot_client as gc
from core.agent import grokbot_session as gs
from core.agent.agent_session import Activity, AgentSessionError, Done, Error, ErrorKind, TextDelta
from core.agent.grokbot_client import Credentials, GrokBotClient, GrokBotError, GrokBotHub
from core.agent.grokbot_session import GrokBotSession, GrokBotSessionConfig
from core.agent.session_store import SessionStore
from core.providers.llm.grok_bot import grok_bot as grok_mod

real_sleep = gc._sleep  # conftest 把 asyncio.sleep 改成了不等待的桩


def jwt(exp_in: float) -> str:
    claims = base64.urlsafe_b64encode(json.dumps({"exp": time.time() + exp_in}).encode()).rstrip(b"=").decode()
    return f"h.{claims}.s"


async def wait_until(cond, timeout=3.0):
    deadline = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > deadline:
            raise AssertionError("等待超时")
        await real_sleep(0.01)


# ---------- 假云端 ----------

class FakeCloud:
    def __init__(self):
        self.app = web.Application()
        self.app.add_routes([
            web.get("/auth/poll", self.auth_poll),
            web.post("/oauth/token", self.oauth_token),
            web.post("/aiserver.v1.GrokBotService/EnsureSandBox", self.ensure),
            web.post("/api/listAgents", self.list_agents),
            web.post("/api/sendPrompt", self.send_prompt),
            web.post("/api/promptAcceptanceStatus", self.acceptance_status),
            web.post("/api/getAgentTranscriptTail", self.tail),
            web.get("/events", self.events),
        ])
        self.url = None
        self.approved = False
        self.refresh_calls = 0
        self.refresh_logout = False
        self.ensure_calls = 0
        self.ensure_states = []  # 依次返回的 runState（没有 gatewayUrl）；用完后正常返回
        self.ensure_status = []  # 依次返回的 HTTP 状态码（401 等）
        self.agents = [{"id": "bot-1", "name": "孙悟空", "isRunning": False, "newestEntryId": "e0", "harness": "temporal"}]
        self.entries = [{"id": "e0", "kind": "message", "role": "user", "content": "旧消息"}]
        self.sent = []
        self.send_fail_once = False
        self.acceptance = {"status": "accepted"}
        self.hibernate_once = False
        self.events_q: asyncio.Queue = asyncio.Queue()
        self.stream_dead = False  # 连上了但什么都不推
        self.streams = 0
        self.gateway_tokens = []

    async def start(self):
        self.server = TestServer(self.app)
        await self.server.start_server()
        self.url = f"http://127.0.0.1:{self.server.port}"

    async def stop(self):
        await self.server.close()

    async def push(self, channel, payload):
        await self.events_q.put((channel, payload))

    # ----- api2 -----

    async def auth_poll(self, request):
        if not self.approved:
            return web.Response(status=404, text="Not found")
        return web.json_response({"accessToken": jwt(3600), "refreshToken": "r-1", "selectedTeamId": 0})

    async def oauth_token(self, request):
        self.refresh_calls += 1
        if self.refresh_logout:
            return web.json_response({"shouldLogout": True}, status=401)
        return web.json_response({"access_token": jwt(3600), "refresh_token": "r-2"})

    async def ensure(self, request):
        self.ensure_calls += 1
        if self.ensure_status:
            return web.json_response({"error": "x"}, status=self.ensure_status.pop(0))
        if self.ensure_states:
            return web.json_response({"runState": self.ensure_states.pop(0)})
        return web.json_response({"gatewayUrl": self.url, "gatewayToken": "gw-1", "networkToken": "nt-1",
                                  "runState": "SAND_BOX_RUN_STATE_RUNNING"})

    # ----- 网关 -----

    def _check(self, request):
        self.gateway_tokens.append(request.headers.get("authorization"))
        if self.hibernate_once:
            self.hibernate_once = False
            return web.Response(status=417, headers={"x-anyrun-failure-reason": "pod_hibernated"})
        return None

    async def list_agents(self, request):
        return self._check(request) or web.json_response(self.agents)

    async def send_prompt(self, request):
        body = await request.json()
        self.sent.append(body)
        if self.send_fail_once:
            self.send_fail_once = False
            return web.Response(status=500, text="boom")
        return web.json_response({"accepted": True})

    async def acceptance_status(self, request):
        return web.json_response(self.acceptance)

    async def tail(self, request):
        body = await request.json()
        return web.json_response({"entries": self.entries[-int(body.get("limit", 10)):], "nextBeforeSeq": 0})

    async def events(self, request):
        self.streams += 1
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await resp.prepare(request)
        await resp.write(b"retry: 1000\n\n")
        if not self.stream_dead:
            await resp.write(b'data: {"channel":"box-disk-pressure","payload":null}\n\n')
        try:
            while True:
                try:
                    channel, payload = await asyncio.wait_for(self.events_q.get(), timeout=0.1)
                except asyncio.TimeoutError:
                    await resp.write(b":ping\n\n")
                    continue
                if channel == "__close__":
                    break
                if self.stream_dead:
                    continue
                await resp.write(f"data: {json.dumps({'channel': channel, 'payload': payload}, ensure_ascii=False)}\n\n".encode())
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        return resp


@pytest.fixture
async def cloud(monkeypatch):
    monkeypatch.setattr(gc, "STREAM_BACKOFF", (0.05,))
    monkeypatch.setattr(gc, "WAKE_POLL", 0.01)
    c = FakeCloud()
    await c.start()
    yield c
    await c.stop()


@pytest.fixture
def creds(tmp_path):
    c = Credentials(tmp_path / "creds.json")
    c.save({"accessToken": jwt(3600), "refreshToken": "r-1"})
    return c


@pytest.fixture
async def client(cloud, creds):
    c = GrokBotClient(creds, backend=cloud.url, website=cloud.url)
    yield c
    await c.close()


@pytest.fixture
async def hub(client):
    h = GrokBotHub(client, idle_seconds=0.1)
    yield h
    await h.close()


def cfg(**kw):
    base = dict(bot_id="bot-1", bot_name="孙悟空", echo_timeout=5, poll_interval=5, done_delay=0.15, no_reply_timeout=5)
    base.update(kw)
    return GrokBotSessionConfig(**base)


def echo_entry(cloud, text="你好"):
    return {"id": "e1", "kind": "message", "role": "user", "content": text, "clientNonce": cloud.sent[-1]["clientNonce"]}


def reply_entry(text="**收到**", eid="e2"):
    return {"id": eid, "kind": "send-message", "message": {"type": "text", "content": text}}


async def collect(session, text="你好"):
    return [ev async for ev in session.send(text)]


def texts(evs):
    return [e.text for e in evs if isinstance(e, TextDelta)]


# ---------- 凭证 ----------

def test_credentials_file_mode_and_reload(tmp_path):
    c = Credentials(tmp_path / "c.json")
    assert not c.logged_in and c.status()["expires_at"] == 0
    c.save({"accessToken": jwt(100), "refreshToken": "r"})
    assert oct(os.stat(c.path).st_mode & 0o777) == "0o600"
    assert c.logged_in and 90 < c.expires_at - time.time() <= 100
    # 别的进程改了文件：下次读状态时重读
    other = Credentials(c.path)
    other.save({"accessToken": jwt(5000), "refreshToken": "r"})
    os.utime(c.path, ns=(os.stat(c.path).st_atime_ns, os.stat(c.path).st_mtime_ns + 1))
    assert c.status()["expires_at"] - time.time() > 4000
    c.mark_needs_login()
    assert c.needs_login and not c.logged_in
    c.clear()
    assert not c.path.exists() and c.status()["logged_in"] is False


def test_jwt_exp_tolerates_garbage():
    assert gc.jwt_exp("") == 0 and gc.jwt_exp("a.b.c") == 0


# ---------- 登录、续期、云端电脑 ----------

async def test_login_flow(cloud, tmp_path):
    client = GrokBotClient(Credentials(tmp_path / "c.json"), backend=cloud.url, website=cloud.url)
    flow = client.start_login()
    assert "/loginDeepControl?" in flow.url and "challenge=" in flow.url and flow.verifier not in flow.url
    assert await client.poll_login_once(flow) is None  # 还没批准
    cloud.approved = True
    assert await client.poll_login_once(flow) is True
    assert client.creds.logged_in and client.creds.team_id == "0"
    await client.close()


async def test_refresh_when_token_expiring(cloud, tmp_path):
    creds = Credentials(tmp_path / "c.json")
    creds.save({"accessToken": jwt(100), "refreshToken": "r-1"})
    client = GrokBotClient(creds, backend=cloud.url)
    await client.fresh_token()
    assert cloud.refresh_calls == 1 and creds.refresh_token == "r-2" and creds.expires_at - time.time() > 3000
    await client.fresh_token()
    assert cloud.refresh_calls == 1  # 还没到续期时间不再续
    await client.close()


async def test_refresh_rejected_marks_needs_login(cloud, tmp_path):
    creds = Credentials(tmp_path / "c.json")
    creds.save({"accessToken": jwt(100), "refreshToken": "r-1"})
    cloud.refresh_logout = True
    client = GrokBotClient(creds, backend=cloud.url)
    with pytest.raises(GrokBotError) as e:
        await client.fresh_token()
    assert e.value.kind == ErrorKind.AUTH and creds.needs_login
    with pytest.raises(GrokBotError) as e2:
        await client.ensure_box()
    assert e2.value.kind == ErrorKind.AUTH and cloud.refresh_calls == 1  # 标记后不再反复续期
    await client.close()


async def test_not_logged_in_is_auth_error(cloud, tmp_path):
    client = GrokBotClient(Credentials(tmp_path / "none.json"), backend=cloud.url)
    with pytest.raises(GrokBotError) as e:
        await client.ensure_box()
    assert e.value.kind == ErrorKind.AUTH
    await client.close()


async def test_ensure_box_waits_for_wake(cloud, client):
    cloud.ensure_states = ["SAND_BOX_RUN_STATE_ASLEEP", "SAND_BOX_RUN_STATE_STARTING"]
    conn = await client.ensure_box()
    assert conn["gatewayUrl"] == cloud.url and cloud.ensure_calls == 3


async def test_ensure_box_401_refreshes_once_then_gives_up(cloud, client):
    cloud.ensure_status = [401]
    await client.ensure_box()
    assert cloud.refresh_calls == 1 and cloud.ensure_calls == 2
    cloud.ensure_status = [401, 401]
    client.conn = None
    with pytest.raises(GrokBotError) as e:
        await client.ensure_box()
    assert e.value.kind == ErrorKind.AUTH and client.creds.needs_login


async def test_hibernated_gateway_is_woken_and_retried(cloud, client):
    cloud.hibernate_once = True
    agents = await client.list_agents()
    assert agents[0]["name"] == "孙悟空" and cloud.ensure_calls == 2
    assert cloud.gateway_tokens[-1] == "Bearer gw-1"


# ---------- 事件流中枢 ----------

async def test_hub_reconnects_when_stream_closes(cloud, hub):
    sub = hub.subscribe("bot-1")
    await wait_until(lambda: hub.generation == 1)
    await cloud.push("__close__", {})
    await wait_until(lambda: hub.generation == 2)
    assert cloud.streams == 2
    sub.close()
    await wait_until(lambda: hub._task is None, timeout=2)  # 没人订阅后关闭


async def test_hub_dispatches_by_agent_and_broadcasts(cloud, hub):
    a, b = hub.subscribe("bot-1"), hub.subscribe("bot-2")
    await wait_until(lambda: hub.generation == 1)
    await cloud.push("transcript", {"agentId": "bot-1", "type": "appended", "entry": {"id": "x"}})
    await cloud.push("agent-upserted", {"agent": {"id": "bot-2"}})
    ch_a, _ = await asyncio.wait_for(a.queue.get(), 2)
    while ch_a in ("hub-connected", "box-disk-pressure"):
        ch_a, _ = await asyncio.wait_for(a.queue.get(), 2)
    assert ch_a == "transcript"
    ch_b, _ = await asyncio.wait_for(b.queue.get(), 2)
    while ch_b in ("hub-connected", "box-disk-pressure"):
        ch_b, _ = await asyncio.wait_for(b.queue.get(), 2)
    assert ch_b == "agent-upserted"
    assert a.queue.empty() and b.queue.empty()


# ---------- 会话 ----------

async def test_session_normal_reply(cloud, hub):
    s = GrokBotSession(hub, cfg())
    await s.start()

    async def script():
        await wait_until(lambda: cloud.sent)
        await cloud.push("agent-upserted", {"agent": {"id": "bot-1", "isRunning": True}})
        await cloud.push("transcript", {"agentId": "bot-1", "type": "appended", "entry": echo_entry(cloud)})
        await cloud.push("transcript", {"agentId": "bot-1", "type": "appended", "entry": reply_entry()})
        await cloud.push("transcript", {"agentId": "bot-1", "type": "updated", "entry": reply_entry()})  # 同一条更新不重播
        await cloud.push("agent-upserted", {"agent": {"id": "bot-1", "isRunning": False}})

    task = asyncio.ensure_future(script())
    evs = await collect(s)
    await task
    assert texts(evs) == ["收到。"]
    assert isinstance(evs[-1], Done) and not evs[-1].interrupted and evs[-1].session_id == "bot-1"
    assert cloud.sent[0]["agentId"] == "bot-1" and cloud.sent[0]["prompt"] == "你好" and cloud.sent[0]["clientNonce"]
    assert any(isinstance(e, Activity) and e.tools_running == 1 for e in evs)
    await s.close()


async def test_session_ignores_old_entries_and_streaming(cloud, hub):
    s = GrokBotSession(hub, cfg())
    await s.start()

    async def script():
        await wait_until(lambda: cloud.sent)
        await cloud.push("agent-upserted", {"agent": {"id": "bot-1", "isRunning": True}})
        await cloud.push("transcript", {"agentId": "bot-1", "type": "appended",
                                        "entry": {"id": "e0", "kind": "send-message", "message": {"type": "text", "content": "旧的"}}})
        await cloud.push("transcript", {"agentId": "bot-1", "type": "appended", "entry": {**reply_entry("写到一半"), "isStreaming": True}})
        await cloud.push("transcript", {"agentId": "bot-1", "type": "updated", "entry": reply_entry("写完了。")})
        await cloud.push("transcript", {"agentId": "bot-1", "type": "appended",
                                        "entry": {"id": "e3", "kind": "tool-call", "content": "不念"}})
        await cloud.push("agent-upserted", {"agent": {"id": "bot-1", "isRunning": False}})

    task = asyncio.ensure_future(script())
    evs = await collect(s)
    await task
    assert texts(evs) == ["写完了。"]
    await s.close()


async def test_session_dead_stream_falls_back_to_polling(cloud, hub):
    """事件流"假活"：连着、有 ping，但不推事件。发送后没回显就重连并补拉，之后靠轮询拿到回复和结束。"""
    cloud.stream_dead = True
    s = GrokBotSession(hub, cfg(echo_timeout=0.3, poll_interval=0.3))
    await s.start()
    gen_before = None

    async def script():
        nonlocal gen_before
        await wait_until(lambda: cloud.sent)
        gen_before = hub.generation
        cloud.entries += [echo_entry(cloud), reply_entry("还在。")]
        cloud.agents[0]["newestEntryId"] = "e2"

    task = asyncio.ensure_future(script())
    evs = await collect(s)
    await task
    assert texts(evs) == ["还在。"] and isinstance(evs[-1], Done)
    assert hub.generation > gen_before and cloud.streams >= 2  # 重连过
    await s.close()


async def test_session_recovers_after_stream_closes_mid_turn(cloud, hub):
    s = GrokBotSession(hub, cfg())
    await s.start()

    async def script():
        await wait_until(lambda: cloud.sent)
        await cloud.push("agent-upserted", {"agent": {"id": "bot-1", "isRunning": True}})
        await cloud.push("transcript", {"agentId": "bot-1", "type": "appended", "entry": echo_entry(cloud)})
        await cloud.push("__close__", {})  # 服务端约 57 秒断一次
        # 断线期间 Bot 回复并空闲：重连后补拉尾部拿到
        cloud.entries += [echo_entry(cloud), reply_entry("在。")]
        cloud.agents[0]["newestEntryId"] = "e2"

    task = asyncio.ensure_future(script())
    evs = await collect(s)
    await task
    assert texts(evs) == ["在。"] and isinstance(evs[-1], Done) and cloud.streams >= 2
    await s.close()


async def test_send_failure_not_delivered(cloud, hub):
    cloud.send_fail_once = True
    cloud.acceptance = {"status": "not-found"}
    s = GrokBotSession(hub, cfg())
    await s.start()
    evs = await collect(s)
    assert isinstance(evs[-1], Error) and evs[-1].kind == ErrorKind.SERVER and "没收到" in evs[-1].voice_message
    await s.close()


async def test_send_failure_but_delivered_continues(cloud, hub):
    cloud.send_fail_once = True
    cloud.acceptance = {"status": "accepted", "acceptedAt": 1}
    s = GrokBotSession(hub, cfg())
    await s.start()

    async def script():
        await wait_until(lambda: cloud.sent)
        await cloud.push("agent-upserted", {"agent": {"id": "bot-1", "isRunning": True}})
        await cloud.push("transcript", {"agentId": "bot-1", "type": "appended", "entry": reply_entry("收到。")})
        await cloud.push("agent-upserted", {"agent": {"id": "bot-1", "isRunning": False}})

    task = asyncio.ensure_future(script())
    evs = await collect(s)
    await task
    assert texts(evs) == ["收到。"] and isinstance(evs[-1], Done)
    await s.close()


async def test_interrupt_stops_speaking_only(cloud, hub):
    s = GrokBotSession(hub, cfg())
    await s.start()

    async def script():
        await wait_until(lambda: cloud.sent)
        await cloud.push("agent-upserted", {"agent": {"id": "bot-1", "isRunning": True}})
        await s.interrupt()

    task = asyncio.ensure_future(script())
    evs = await collect(s)
    await task
    assert isinstance(evs[-1], Done) and evs[-1].interrupted
    assert len(cloud.sent) == 1  # 没有向云端发任何停止指令
    await s.close()


async def test_waiting_for_user_in_app(cloud, hub):
    s = GrokBotSession(hub, cfg())
    await s.start()

    async def script():
        await wait_until(lambda: cloud.sent)
        await cloud.push("agent-upserted", {"agent": {"id": "bot-1", "isRunning": True}})
        await cloud.push("agent-upserted", {"agent": {"id": "bot-1", "isRunning": False, "awaitingUserResponse": "approval"}})

    task = asyncio.ensure_future(script())
    evs = await collect(s)
    await task
    assert texts(evs) == [gs.WAITING_NOTICE] and isinstance(evs[-1], Done)
    await s.close()


async def test_no_reply_notice(cloud, hub):
    s = GrokBotSession(hub, cfg(no_reply_timeout=0.3, poll_interval=0.1))
    await s.start()

    async def script():
        await wait_until(lambda: cloud.sent)
        await cloud.push("transcript", {"agentId": "bot-1", "type": "appended", "entry": echo_entry(cloud)})

    task = asyncio.ensure_future(script())
    evs = await collect(s)
    await task
    assert texts(evs) == [gs.NO_REPLY_NOTICE] and isinstance(evs[-1], Done)
    await s.close()


async def test_session_auth_error_on_start(cloud, tmp_path):
    client = GrokBotClient(Credentials(tmp_path / "none.json"), backend=cloud.url)
    hub = GrokBotHub(client)
    with pytest.raises(AgentSessionError) as e:
        await GrokBotSession(hub, cfg()).start()
    assert e.value.kind == ErrorKind.AUTH
    await hub.close()


def test_plain_text_and_spoken_form():
    md = "# 结论\n\n- **第一**：看了 `58` 条帖子\n- 第二：[链接](https://x.com) 里的 *重点*\n\n```py\nprint(1)\n```\n| a | b |\n|---|---|\n| 1 | 2 |"
    out = gs.plain_text(md)
    assert out.startswith("结论\n第一：看了 58 条帖子\n第二：链接 里的 重点")
    assert "**" not in out and "`" not in out and "https://" not in out and "|" not in out and "print" not in out
    assert gs.spoken_form("收到") == "收到。" and gs.spoken_form("好的！") == "好的！" and gs.spoken_form("") == ""
    assert gs.reply_text({"kind": "message", "role": "assistant", "content": "x"}) == "x"
    assert gs.reply_text({"kind": "message", "role": "user", "content": "x"}) is None
    assert gs.reply_text({"kind": "send-message", "message": {"type": "attachment"}}) is None
    assert gs.reply_text({"kind": "tool-call"}) is None


# ---------- provider ----------

class FakeGrokAgent:
    instances = []
    script = []
    start_error = None
    delay = 0.0

    def __init__(self, hub, config, logger=None):
        self.hub, self.config = hub, config
        self.session_id = config.bot_id
        self.alive = False
        self.sent, self.interrupts, self.closed = [], 0, False
        FakeGrokAgent.instances.append(self)

    async def start(self):
        if FakeGrokAgent.start_error:
            raise FakeGrokAgent.start_error
        self.alive = True

    async def send(self, text):
        self.sent.append(text)
        if FakeGrokAgent.delay:
            await real_sleep(FakeGrokAgent.delay)
        for ev in (FakeGrokAgent.script.pop(0) if FakeGrokAgent.script else [TextDelta("收到。"), Done(session_id="bot-1")]):
            yield ev

    async def interrupt(self):
        self.interrupts += 1

    async def close(self):
        self.closed = True
        self.alive = False


@pytest.fixture
def provider(tmp_path, monkeypatch):
    FakeGrokAgent.instances, FakeGrokAgent.script, FakeGrokAgent.start_error, FakeGrokAgent.delay = [], [], None, 0.0
    monkeypatch.setattr(grok_mod, "GrokBotSession", FakeGrokAgent)
    monkeypatch.setattr(grok_mod, "get_hub", lambda *a, **k: SimpleNamespace(name="hub"))
    p = grok_mod.LLMProvider({"bot_id": "bot-1", "bot_name": "孙悟空", "credentials_path": str(tmp_path / "c.json"),
                              "filler_delay_seconds": 0.3, "turn_timeout_seconds": 3})
    p.store = SessionStore(tmp_path / "sessions.json", "grok_bot")
    return p


def conn(sid="conn-1", device="dev-1"):
    return SimpleNamespace(session_id=sid, device_id=device, intent_type="nointent")


def run(p, c, text):
    return "".join(p.response(c.session_id, [{"role": "system", "content": "你是小智"}, {"role": "user", "content": text}]))


def test_provider_speaks_reply_and_reuses_session(provider):
    c = conn()
    provider.on_conn_open(c)
    assert run(provider, c, "第一句") == "收到。"
    assert run(provider, c, "第二句") == "收到。"
    assert len(FakeGrokAgent.instances) == 1 and FakeGrokAgent.instances[0].sent == ["第一句", "第二句"]
    assert provider.model_name == "grok_bot:孙悟空" and provider.store.get("dev-1")["session_id"] == "bot-1"


def test_provider_filler_mentions_grok(provider):
    FakeGrokAgent.delay = 0.6
    c = conn()
    provider.on_conn_open(c)
    out = run(provider, c, "慢一点")
    assert out.startswith("😶交给 Grok Bot 了。") and out.endswith("收到。")


def test_provider_auth_error_has_grok_wording(provider):
    FakeGrokAgent.start_error = AgentSessionError(ErrorKind.AUTH, "no creds")
    c = conn()
    provider.on_conn_open(c)
    assert "Grok Bot 还没登录" in run(provider, c, "你好")


def test_provider_timeout_has_grok_wording(provider, tmp_path, monkeypatch):
    monkeypatch.setattr(grok_mod, "get_hub", lambda *a, **k: None)
    p = grok_mod.LLMProvider({"bot_id": "bot-1", "bot_name": "孙悟空", "credentials_path": str(tmp_path / "c.json"),
                              "turn_timeout_seconds": 0.3, "first_reply_filler": ""})
    p.store = SessionStore(tmp_path / "s2.json", "grok_bot")
    FakeGrokAgent.script = [[Activity("agent", tools_running=1), 2.0]]

    async def slow_send(self, text):
        yield Activity("agent", tools_running=1)
        await real_sleep(2)
        yield Done()

    monkeypatch.setattr(FakeGrokAgent, "send", slow_send)
    c = conn()
    p.on_conn_open(c)
    assert "Grok Bot 这件事做得太久" in run(p, c, "你好")


def test_providers_share_one_loop(provider, tmp_path):
    p2 = grok_mod.LLMProvider({"bot_id": "bot-2", "bot_name": "林黛玉", "credentials_path": str(tmp_path / "c.json")})
    assert p2._loop is provider._loop and p2._loop.is_running()
    provider.shutdown()
    assert provider._loop.is_running()  # 共用的循环不随实例退役而停止


def test_provider_self_check_without_login(tmp_path, monkeypatch, caplog):
    p = grok_mod.LLMProvider({"bot_id": "", "bot_name": "x", "credentials_path": str(tmp_path / "c.json")})
    assert p.bot_id == "" and p.model_name == "grok_bot:x"


def test_voice_messages_cover_kinds():
    for kind in (ErrorKind.AUTH, ErrorKind.SERVER, ErrorKind.TIMEOUT, ErrorKind.STALLED, ErrorKind.PROCESS_EXITED):
        assert "Grok Bot" in gs.voice_for(kind)
    assert gs.voice_for(ErrorKind.NOT_INSTALLED) == gs.VOICE_MESSAGES[ErrorKind.UNKNOWN]

"""管理后台：配置读写、密码、接口鉴权。全部使用临时配置文件，不碰 data/.config.yaml。"""

from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from core.admin import auth, config_store
from core.admin.admin_handler import AdminHandler

CUSTOM_YAML = """\
# 用户注释应被保留
selected_module:
  LLM: ClaudeAPI   # 行尾注释
LLM:
  ClaudeAPI:
    type: openai
    base_url: https://api.anthropic.com/v1/
    model_name: claude-haiku-5-5
    api_key: 你的Claude API Key
    max_tokens: 500
"""


@pytest.fixture
def cfg_path(tmp_path) -> Path:
    p = tmp_path / ".config.yaml"
    p.write_text(CUSTOM_YAML, encoding="utf-8")
    return p


def merged(cfg_path):
    return config_store.load_merged(custom_path=cfg_path)


# ---------- config_store ----------

def entry(section, name, path):
    return next(e for e in config_store.describe_section(merged(path), section)["entries"] if e["name"] == name)


def field(e, name):
    return next(f for f in e["fields"] if f["name"] == name)


def test_describe_masks_secret_and_flags_placeholder(cfg_path):
    e = entry("LLM", "ClaudeAPI", cfg_path)
    key = field(e, "api_key")
    assert key["kind"] == "secret" and key["set"] is False and "value" not in key
    assert e["needs_key"] is True
    assert field(e, "model_name") == {"name": "model_name", "kind": "text", "value": "claude-haiku-5-5"}
    assert field(e, "extra_body")["kind"] == "json"  # openai 类型允许新增 extra_body
    # max_tokens 之类的字段不能被误判成密钥
    assert config_store.field_kind("max_tokens", 500) == "number"
    assert config_store.field_kind("access_token", "x") == "secret"
    assert config_store.field_kind("token", "x") == "secret"
    assert "ChatGLMLLM" in [x["name"] for x in config_store.describe_section(merged(cfg_path), "LLM")["entries"]]


def test_update_writes_key_and_keeps_comments(cfg_path):
    changed = config_store.update_entry("LLM", "ClaudeAPI", {"api_key": "sk-ant-1234567890abcdef"}, merged(cfg_path), cfg_path)
    assert changed == ["api_key"]
    text = cfg_path.read_text(encoding="utf-8")
    assert "sk-ant-1234567890abcdef" in text
    assert "# 用户注释应被保留" in text and "# 行尾注释" in text
    key = field(entry("LLM", "ClaudeAPI", cfg_path), "api_key")
    assert key["set"] is True and key["hint"] == "sk-a…cdef"


def test_update_empty_secret_means_unchanged(cfg_path):
    config_store.update_entry("LLM", "ClaudeAPI", {"api_key": "sk-old-1234567890"}, merged(cfg_path), cfg_path)
    config_store.update_entry("LLM", "ClaudeAPI", {"api_key": "", "model_name": "claude-sonnet-5-5"}, merged(cfg_path), cfg_path)
    e = merged(cfg_path)["LLM"]["ClaudeAPI"]
    assert e["api_key"] == "sk-old-1234567890" and e["model_name"] == "claude-sonnet-5-5"


def test_update_converts_types(cfg_path):
    config_store.update_entry("LLM", "ClaudeAPI", {"max_tokens": "800", "extra_body": '{"reasoning_effort": "none"}'},
                              merged(cfg_path), cfg_path)
    e = merged(cfg_path)["LLM"]["ClaudeAPI"]
    assert e["max_tokens"] == 800 and e["extra_body"] == {"reasoning_effort": "none"}
    with pytest.raises(ValueError):
        config_store.update_entry("LLM", "ClaudeAPI", {"extra_body": "[1, 2]"}, merged(cfg_path), cfg_path)
    with pytest.raises(ValueError):
        config_store.update_entry("LLM", "ClaudeAPI", {"max_tokens": "abc"}, merged(cfg_path), cfg_path)


def test_update_list_and_bool_fields(cfg_path):
    cfg_path.write_text(CUSTOM_YAML + """  ClaudeCLI:
    type: claude_cli
    allowed_tools: [Read, Grep, Glob]
    turn_timeout_seconds: 120
""", encoding="utf-8")
    config_store.update_entry("LLM", "ClaudeCLI", {"allowed_tools": "Read, Glob，Grep", "turn_timeout_seconds": "60"},
                              merged(cfg_path), cfg_path)
    e = merged(cfg_path)["LLM"]["ClaudeCLI"]
    assert e["allowed_tools"] == ["Read", "Glob", "Grep"] and e["turn_timeout_seconds"] == 60
    assert config_store.field_kind("enabled", True) == "bool"
    assert config_store._convert("bool", "true", False) is True


def test_update_ignores_unknown_fields(cfg_path):
    assert config_store.update_entry("LLM", "ClaudeAPI", {"type": "evil", "foo": "bar"}, merged(cfg_path), cfg_path) == []
    e = merged(cfg_path)["LLM"]["ClaudeAPI"]
    assert e["type"] == "openai" and "foo" not in e


def test_update_default_entry_only_overrides_changed_field(cfg_path):
    config_store.update_entry("LLM", "ChatGLMLLM", {"api_key": "glm-key-1234567890"}, merged(cfg_path), cfg_path)
    custom = config_store.load_custom(cfg_path)
    assert dict(custom["LLM"]["ChatGLMLLM"]) == {"api_key": "glm-key-1234567890"}
    assert merged(cfg_path)["LLM"]["ChatGLMLLM"]["type"] == "openai"


def test_select_entry_other_sections(cfg_path):
    config_store.select_entry("TTS", "EdgeTTS", merged(cfg_path), cfg_path)
    config_store.select_entry("Intent", "nointent", merged(cfg_path), cfg_path)
    m = merged(cfg_path)["selected_module"]
    assert m["TTS"] == "EdgeTTS" and m["Intent"] == "nointent" and m["LLM"] == "ClaudeAPI"
    with pytest.raises(KeyError):
        config_store.select_entry("LLM", "NoSuchLLM", merged(cfg_path), cfg_path)
    with pytest.raises(KeyError):
        config_store.select_entry("VAD", "SileroVAD", merged(cfg_path), cfg_path)


# ---------- auth ----------

def test_password_hash_roundtrip():
    h = auth.hash_password("correct horse")
    assert auth.verify_password("correct horse", h)
    assert not auth.verify_password("wrong", h)
    assert not auth.verify_password("x", "garbage")


def test_session_store_expiry():
    store = auth.SessionStore(ttl_seconds=-1)
    assert not store.valid(store.create())
    store = auth.SessionStore()
    t = store.create()
    assert store.valid(t)
    store.revoke(t)
    assert not store.valid(t)


# ---------- HTTP 接口 ----------

@pytest.fixture
async def client(cfg_path):
    app = web.Application()
    config = {"selected_module": {"LLM": "ClaudeAPI"}, "LLM": {}}
    AdminHandler(config, ws_server=None, config_path=cfg_path).register(app)
    async with TestClient(TestServer(app)) as c:
        yield c


async def test_setup_login_and_update_flow(client, cfg_path):
    st = await (await client.get("/admin/api/state")).json()
    assert st == {"needs_setup": True, "is_local": True, "logged_in": False}

    r = await client.post("/admin/api/setup", json={"password": "abc12"})
    assert r.status == 400
    r = await client.post("/admin/api/setup", json={"password": "pass-1234"})
    assert r.status == 200
    assert (await (await client.get("/admin/api/state")).json())["logged_in"] is True

    r = await client.post("/admin/api/setup", json={"password": "another-pass"})
    assert r.status == 409

    r = await client.post("/admin/api/modules/LLM/entries/ClaudeAPI", json={"api_key": "sk-ant-abcdefghijklmnop"})
    assert r.status == 200 and (await r.json())["changed"] == ["api_key"]
    data = await (await client.get("/admin/api/modules/LLM")).json()
    item = next(i for i in data["entries"] if i["name"] == "ClaudeAPI")
    assert next(f for f in item["fields"] if f["name"] == "api_key")["set"] is True
    assert "sk-ant-abcdefghijklmnop" not in str(data)  # 完整 Key 不回传

    r = await client.post("/admin/api/modules/LLM/entries/ClaudeAPI", json={"extra_body": "not json"})
    assert r.status == 400
    r = await client.post("/admin/api/modules/TTS/select", json={"name": "EdgeTTS"})
    assert r.status == 200 and (await r.json())["note"]
    assert (await client.get("/admin/api/modules/VAD")).status == 404

    await client.post("/admin/api/logout", json={})
    assert (await client.get("/admin/api/modules/LLM")).status == 401
    assert (await client.post("/admin/api/login", json={"password": "pass-1234"})).status == 200
    assert (await client.get("/admin/api/modules/LLM")).status == 200


async def test_requires_login_and_json(client):
    assert (await client.get("/admin/api/modules/LLM")).status == 401
    assert (await client.get("/admin/api/status")).status == 401
    assert (await client.post("/admin/api/modules/LLM/select", json={"name": "ChatGLMLLM"})).status == 401
    r = await client.post("/admin/api/setup", data="password=pass-1234",
                          headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert r.status == 415


async def test_setup_rejected_from_remote(cfg_path, monkeypatch):
    monkeypatch.setattr(AdminHandler, "_is_local", staticmethod(lambda request: False))
    app = web.Application()
    AdminHandler({"selected_module": {}}, config_path=cfg_path).register(app)
    async with TestClient(TestServer(app)) as c:
        r = await c.post("/admin/api/setup", json={"password": "pass-1234"})
        assert r.status == 403
    assert config_store.get_password_hash(cfg_path) is None


async def test_wrong_password(client):
    await client.post("/admin/api/setup", json={"password": "pass-1234"})
    await client.post("/admin/api/logout", json={})
    r = await client.post("/admin/api/login", json={"password": "nope-nope"})
    assert r.status == 401


async def test_status_endpoint(client):
    await client.post("/admin/api/setup", json={"password": "pass-1234"})
    data = await (await client.get("/admin/api/status")).json()
    assert data["ok"] and data["addresses"]["ota"].endswith("/xiaozhi/ota/")
    names = [c["name"] for c in data["checks"]]
    assert "ffmpeg" in names and "opus" in names
    assert set(data["modules"]) == {"LLM", "ASR", "TTS", "Intent"}


def test_unchanged_fields_are_not_written(cfg_path):
    before = cfg_path.read_text(encoding="utf-8")
    changed = config_store.update_entry(
        "LLM", "ClaudeAPI",
        {"model_name": "claude-haiku-5-5", "base_url": "https://api.anthropic.com/v1/", "max_tokens": "500",
         "api_key": "", "extra_body": "{}"},
        merged(cfg_path), cfg_path)
    assert changed == []  # 值都没变，空的 extra_body 也不算新增
    assert "model_name: claude-haiku-5-5" in cfg_path.read_text(encoding="utf-8") and before != ""


async def test_devices_and_emotion_test(client):
    from types import SimpleNamespace
    from core.admin import connections

    await client.post("/admin/api/setup", json={"password": "pass-1234"})
    assert (await (await client.get("/admin/api/devices")).json())["devices"] == []
    sent = []

    class FakeWS:
        async def send(self, data):
            sent.append(data)

    fake = SimpleNamespace(session_id="s-1", device_id="dev-1", client_ip="192.168.6.211", websocket=FakeWS())
    connections.register(fake)
    try:
        devices = (await (await client.get("/admin/api/devices")).json())["devices"]
        assert devices[0]["device_id"] == "dev-1"
        r = await client.post("/admin/api/devices/s-1/emotion-test", json={"interval": 0.5})
        data = await r.json()
        assert r.status == 200 and len(data["sequence"]) == 21
        import asyncio
        for _ in range(200):  # conftest 让 asyncio.sleep 立即返回，后台任务几轮调度即可发完
            if len(sent) == 21:
                break
            await asyncio.sleep(0)
        assert '"emotion": "funny"' in sent[0]
        assert (await client.post("/admin/api/devices/none/emotion-test", json={})).status == 404
    finally:
        connections.unregister(fake)


def test_authorization_like_fields_are_secret():
    for key in ("authorization", "api_token", "refresh_token", "cookie", "credential"):
        assert config_store.field_kind(key, "Bearer abc") == "secret", key
    assert config_store.field_kind("max_tokens", 500) == "number"
    assert config_store.field_kind("max_new_tokens", "1024") == "text"


async def test_emotion_test_rejects_bad_interval_and_survives_disconnect(client):
    from types import SimpleNamespace
    from core.admin import connections

    await client.post("/admin/api/setup", json={"password": "pass-1234"})

    class BrokenWS:
        async def send(self, data):
            raise ConnectionError("closed")

    fake = SimpleNamespace(session_id="s-2", device_id="dev-2", client_ip="x", websocket=BrokenWS())
    connections.register(fake)
    try:
        assert (await client.post("/admin/api/devices/s-2/emotion-test", json={"interval": "abc"})).status == 400
        r = await client.post("/admin/api/devices/s-2/emotion-test", json={"interval": 1})
        assert r.status == 200  # 发送失败只记日志，不产生未处理的任务异常
        import asyncio
        for _ in range(20):
            await asyncio.sleep(0)
    finally:
        connections.unregister(fake)


async def test_apply_failure_keeps_running_config(cfg_path, monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from core.admin import admin_handler as ah

    def boom(*a, **k):
        raise RuntimeError("load failed")
    monkeypatch.setattr(ah, "initialize_modules", boom)
    ws = SimpleNamespace(config_lock=asyncio.Lock(), _llm="old-llm")
    running = {"selected_module": {"LLM": "ClaudeAPI"}, "LLM": {"ClaudeAPI": {"type": "openai"}}}
    h = ah.AdminHandler(running, ws_server=ws, config_path=cfg_path)
    config_store.select_entry("LLM", "ChatGLMLLM", config_store.load_merged(custom_path=cfg_path), cfg_path)
    result = await h._apply("LLM")
    assert result["applied"] is False
    assert running["selected_module"]["LLM"] == "ClaudeAPI" and ws._llm == "old-llm"  # 失败时服务保持原状

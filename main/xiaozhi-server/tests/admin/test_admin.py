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
"""


@pytest.fixture
def cfg_path(tmp_path) -> Path:
    p = tmp_path / ".config.yaml"
    p.write_text(CUSTOM_YAML, encoding="utf-8")
    return p


def merged(cfg_path):
    return config_store.load_merged(custom_path=cfg_path)


# ---------- config_store ----------

def test_list_llms_masks_and_flags_placeholder(cfg_path):
    items = {i["name"]: i for i in config_store.list_llms(merged(cfg_path))}
    assert items["ClaudeAPI"]["api_key_set"] is False
    assert items["ClaudeAPI"]["api_key_hint"] == ""
    assert items["ClaudeAPI"]["fields"]["model_name"] == "claude-haiku-5-5"
    # 来自默认 config.yaml 的模型也在列表里
    assert "ChatGLMLLM" in items


def test_update_llm_writes_key_and_keeps_comments(cfg_path):
    config_store.update_llm("ClaudeAPI", {"api_key": "sk-ant-1234567890abcdef"}, merged(cfg_path), cfg_path)
    text = cfg_path.read_text(encoding="utf-8")
    assert "sk-ant-1234567890abcdef" in text
    assert "# 用户注释应被保留" in text and "# 行尾注释" in text
    item = next(i for i in config_store.list_llms(merged(cfg_path)) if i["name"] == "ClaudeAPI")
    assert item["api_key_set"] is True
    assert item["api_key_hint"] == "sk-a…cdef"


def test_update_llm_empty_key_means_unchanged(cfg_path):
    config_store.update_llm("ClaudeAPI", {"api_key": "sk-old-1234567890"}, merged(cfg_path), cfg_path)
    config_store.update_llm("ClaudeAPI", {"api_key": "", "model_name": "claude-sonnet-5-5"}, merged(cfg_path), cfg_path)
    entry = merged(cfg_path)["LLM"]["ClaudeAPI"]
    assert entry["api_key"] == "sk-old-1234567890"
    assert entry["model_name"] == "claude-sonnet-5-5"


def test_update_llm_ignores_unknown_fields(cfg_path):
    config_store.update_llm("ClaudeAPI", {"type": "evil", "foo": "bar"}, merged(cfg_path), cfg_path)
    entry = merged(cfg_path)["LLM"]["ClaudeAPI"]
    assert entry["type"] == "openai" and "foo" not in entry


def test_update_default_llm_only_overrides_changed_field(cfg_path):
    config_store.update_llm("ChatGLMLLM", {"api_key": "glm-key-1234567890"}, merged(cfg_path), cfg_path)
    custom = config_store.load_custom(cfg_path)
    assert dict(custom["LLM"]["ChatGLMLLM"]) == {"api_key": "glm-key-1234567890"}
    assert merged(cfg_path)["LLM"]["ChatGLMLLM"]["type"] == "openai"


def test_select_llm(cfg_path):
    config_store.select_llm("ChatGLMLLM", merged(cfg_path), cfg_path)
    assert merged(cfg_path)["selected_module"]["LLM"] == "ChatGLMLLM"
    with pytest.raises(KeyError):
        config_store.select_llm("NoSuchLLM", merged(cfg_path), cfg_path)


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

    r = await client.post("/admin/api/llm/ClaudeAPI", json={"api_key": "sk-ant-abcdefghijklmnop"})
    assert r.status == 200
    data = await (await client.get("/admin/api/llm")).json()
    item = next(i for i in data["items"] if i["name"] == "ClaudeAPI")
    assert item["api_key_set"] is True
    assert "sk-ant-abcdefghijklmnop" not in str(data)  # 完整 Key 不回传

    await client.post("/admin/api/logout", json={})
    assert (await client.get("/admin/api/llm")).status == 401
    assert (await client.post("/admin/api/login", json={"password": "pass-1234"})).status == 200
    assert (await client.get("/admin/api/llm")).status == 200


async def test_requires_login_and_json(client):
    assert (await client.get("/admin/api/llm")).status == 401
    assert (await client.post("/admin/api/llm/select", json={"name": "ChatGLMLLM"})).status == 401
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

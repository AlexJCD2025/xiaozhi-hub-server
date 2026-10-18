"""管理后台页面接口：大模型、语音合成、意图识别的读写，切换失败回滚，表情单发。全部使用临时配置文件。"""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from core.admin import catalog, config_store, connections, pages
from core.admin import admin_handler as ah

CUSTOM_YAML = """\
selected_module:
  LLM: ClaudeCLI
  Intent: nointent
LLM:
  ClaudeCLI:
    type: claude_cli
    model: haiku
    effort: low
    workdir: ~/xiaozhi-workspace
    allowed_tools: [Read, Grep, Glob]
    permission_mode: default
    turn_timeout_seconds: 120
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


def merged(path):
    return config_store.load_merged(custom_path=path)


# ---------- 大模型 ----------

def test_describe_llm_kinds_and_vendors(cfg_path):
    d = pages.describe_llm(merged(cfg_path))
    assert d["kind"] == "cli" and d["vendor"] is None
    vendors = {v["id"]: v for v in d["vendors"]}
    assert [v["id"] for v in catalog.LLM_VENDORS] == list(vendors)
    assert vendors["claude"]["configured"] is False  # 占位 Key 不算配置好
    assert vendors["openai"]["base_url"] == "https://api.openai.com/v1" and vendors["openai"]["configured"] is False
    assert vendors["deepseek"]["base_url"] == "https://api.deepseek.com"  # 上游条目用的是 url 字段
    assert d["claude"]["values"]["allowed_tools"] == ["Read", "Grep", "Glob"]
    assert pages.llm_kind(merged(cfg_path), "OllamaLLM") == "other"


def test_save_vendor_creates_new_entry_and_masks_key(cfg_path):
    changed = pages.save_vendor("kimi", {"base_url": "https://api.moonshot.cn/v1", "model": "kimi-latest",
                                         "api_key": "sk-kimi-1234567890"}, merged(cfg_path), cfg_path)
    assert set(changed) == {"base_url", "model_name", "api_key"}
    entry = merged(cfg_path)["LLM"]["KimiLLM"]
    assert entry["type"] == "openai" and entry["api_key"] == "sk-kimi-1234567890"
    v = pages.describe_vendor(catalog.VENDOR_BY_ID["kimi"], merged(cfg_path))
    assert v["configured"] and v["key_hint"] == "sk-k…7890" and "sk-kimi-1234567890" not in str(v)
    # 再保存一次、Key 留空：不修改
    assert pages.save_vendor("kimi", {"base_url": "https://api.moonshot.cn/v1", "model": "kimi-latest", "api_key": ""},
                             merged(cfg_path), cfg_path) == []


def test_save_vendor_keeps_upstream_url_field(cfg_path):
    pages.save_vendor("deepseek", {"base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat",
                                   "api_key": "sk-ds-1234567890"}, merged(cfg_path), cfg_path)
    custom = config_store.load_custom(cfg_path)["LLM"]["DeepSeekLLM"]
    assert custom["url"] == "https://api.deepseek.com/v1" and "base_url" not in custom and "type" not in custom


@pytest.mark.parametrize("payload, message", [
    ({"base_url": "ftp://x", "model": "m"}, "http"),
    ({"base_url": "https://x/v1", "model": ""}, "模型"),
    ({"base_url": "https://x/v1", "model": "m", "api_key": "sk bad"}, "空格"),
])
def test_save_vendor_validation(cfg_path, payload, message):
    with pytest.raises(ValueError, match=message):
        pages.save_vendor("openai", payload, merged(cfg_path), cfg_path)
    with pytest.raises(KeyError):
        pages.save_vendor("nope", payload, merged(cfg_path), cfg_path)


def test_save_claude_cli_validates_choices(cfg_path):
    with pytest.raises(ValueError, match="思考力度"):
        pages.save_claude_cli({"effort": "superhigh"}, merged(cfg_path), cfg_path)
    with pytest.raises(ValueError, match="权限模式"):
        pages.save_claude_cli({"permission_mode": "whatever"}, merged(cfg_path), cfg_path)
    with pytest.raises(ValueError, match="工具名"):
        pages.save_claude_cli({"allowed_tools": ["Read", "rm -rf"]}, merged(cfg_path), cfg_path)
    with pytest.raises(ValueError, match="工作目录"):
        pages.save_claude_cli({"workdir": " "}, merged(cfg_path), cfg_path)
    with pytest.raises(ValueError, match="大于 0"):
        pages.save_claude_cli({"turn_timeout_seconds": "-1"}, merged(cfg_path), cfg_path)
    changed = pages.save_claude_cli({"effort": "medium", "allowed_tools": ["Read", "WebSearch"], "model": "sonnet"},
                                    merged(cfg_path), cfg_path)
    entry = merged(cfg_path)["LLM"]["ClaudeCLI"]
    assert set(changed) == {"effort", "allowed_tools", "model"}
    assert entry["effort"] == "medium" and entry["allowed_tools"] == ["Read", "WebSearch"]


def test_vendor_test_config_prefers_unsaved_values(cfg_path):
    with pytest.raises(ValueError, match="API Key"):
        pages.vendor_test_config("claude", {}, merged(cfg_path))
    cfg = pages.vendor_test_config("claude", {"api_key": "sk-new-123", "model": "claude-sonnet-5-5"}, merged(cfg_path))
    assert cfg["api_key"] == "sk-new-123" and cfg["model_name"] == "claude-sonnet-5-5"
    assert cfg["base_url"] == "https://api.anthropic.com/v1/" and cfg["type"] == "openai"
    assert pages.vendor_test_config("custom", {"base_url": "http://127.0.0.1:1234/v1", "model": "m"},
                                    merged(cfg_path))["api_key"] == "not-needed"


def test_desired_intent(cfg_path):
    m = merged(cfg_path)
    assert pages.desired_intent(m, "ClaudeCLI", cfg_path) == "nointent"
    assert pages.desired_intent(m, "ClaudeAPI", cfg_path) == "nointent"  # Claude 在 function_call 下表现不好
    assert pages.desired_intent(m, "DeepSeekLLM", cfg_path) == "function_call"
    pages.remember_api_intent("nointent", cfg_path)
    assert pages.desired_intent(m, "DeepSeekLLM", cfg_path) == "nointent"


# ---------- 语音识别、语音合成、意图识别 ----------

def test_describe_asr_and_tts(cfg_path):
    asr = pages.describe_asr(merged(cfg_path))
    assert asr["selected"] and "label" in asr
    tts = pages.describe_tts(merged(cfg_path))
    assert tts["voice"] == "zh-CN-XiaoxiaoNeural" and len(tts["voices"]) == 14
    assert all(v["value"].startswith("zh-") for v in tts["voices"])


def test_save_tts_voice(cfg_path):
    with pytest.raises(ValueError):
        pages.save_tts_voice("en-US-AvaNeural", merged(cfg_path), cfg_path)
    assert pages.save_tts_voice("zh-CN-YunxiNeural", merged(cfg_path), cfg_path) == ["voice"]
    assert merged(cfg_path)["TTS"]["EdgeTTS"]["voice"] == "zh-CN-YunxiNeural"


def test_intent_plugins_keep_manual_extras(cfg_path):
    cfg_path.write_text(CUSTOM_YAML + """Intent:
  function_call:
    functions: [get_weather, my_custom_plugin]
""", encoding="utf-8")
    d = pages.describe_intent(merged(cfg_path))
    assert d["locked"] is True and d["mode"] == "nointent" and d["extra"] == ["my_custom_plugin"]
    assert next(p for p in d["plugins"] if p["name"] == "web_search")["needs_config"] is True  # 上游默认 Key 是 mk-xxx
    with pytest.raises(ValueError):
        pages.save_intent_plugins(["evil"], merged(cfg_path), cfg_path)
    pages.save_intent_plugins(["play_music", "get_weather"], merged(cfg_path), cfg_path)
    assert merged(cfg_path)["Intent"]["function_call"]["functions"] == ["get_weather", "play_music", "my_custom_plugin"]


# ---------- HTTP：切换、回滚、联动 ----------

@pytest.fixture
async def client(cfg_path):
    app = web.Application()
    config = {"selected_module": {"LLM": "ClaudeCLI", "Intent": "nointent"}, "LLM": {}, "Intent": {}}
    handler = ah.AdminHandler(config, ws_server=None, config_path=cfg_path)
    handler.register(app)
    async with TestClient(TestServer(app)) as c:
        await c.post("/admin/api/setup", json={"password": "pass-1234"})
        c.handler = handler
        yield c


async def test_switch_failure_restores_selection(cfg_path, monkeypatch):
    """切换失败时，磁盘上的选择要恢复成原来的，避免下次启动出问题。"""
    def boom(*a, **k):
        raise RuntimeError("load failed.")
    monkeypatch.setattr(ah, "initialize_modules", boom)
    ws = SimpleNamespace(config_lock=asyncio.Lock(), _llm="old-llm")
    running = {"selected_module": {"LLM": "ClaudeCLI"}, "LLM": {}}
    h = ah.AdminHandler(running, ws_server=ws, config_path=cfg_path)
    result = await h._switch("LLM", "ChatGLMLLM")
    assert result["ok"] is False and "已恢复为原来的 ClaudeCLI" in result["note"] and "。。" not in result["note"]
    assert merged(cfg_path)["selected_module"]["LLM"] == "ClaudeCLI" and ws._llm == "old-llm"


async def test_llm_use_links_intent(client, cfg_path):
    r = await client.post("/admin/api/llm/use", json={"kind": "api", "vendor": "deepseek"})
    assert r.status == 400 and "还没配置好" in (await r.json())["error"]
    r = await client.post("/admin/api/llm/vendors/deepseek",
                          json={"base_url": "https://api.deepseek.com", "model": "deepseek-chat", "api_key": "sk-ds-123456789"})
    assert r.status == 200
    r = await client.post("/admin/api/llm/use", json={"kind": "api", "vendor": "deepseek"})
    data = await r.json()
    assert r.status == 200 and "用插件" in data["note"]
    m = merged(cfg_path)["selected_module"]
    assert m["LLM"] == "DeepSeekLLM" and m["Intent"] == "function_call"
    # 回到 Claude CLI：意图识别自动改回 nointent；API 模式下不能选插件的限制随之生效
    r = await client.post("/admin/api/llm/use", json={"kind": "cli"})
    assert r.status == 200 and merged(cfg_path)["selected_module"]["Intent"] == "nointent"
    r = await client.post("/admin/api/intent", json={"mode": "function_call", "functions": []})
    assert r.status == 400


async def test_llm_and_page_endpoints(client):
    data = await (await client.get("/admin/api/llm")).json()
    assert data["kind"] == "cli" and "install" in data["claude"]
    r = await client.post("/admin/api/llm/claude", json={"effort": "nope"})
    assert r.status == 400 and "思考力度" in (await r.json())["error"]
    assert (await client.get("/admin/api/asr")).status == 200
    tts = await (await client.get("/admin/api/tts")).json()
    assert len(tts["voices"]) == 14
    assert (await client.get("/admin/api/tts/preview?voice=en-US-AvaNeural")).status == 400
    r = await client.post("/admin/api/tts", json={"voice": "zh-CN-YunxiNeural"})
    assert r.status == 200 and (await r.json())["changed"] == ["voice"]
    intent = await (await client.get("/admin/api/intent")).json()
    assert intent["locked"] is True
    for path in ("llm", "asr", "tts", "intent"):
        await client.post("/admin/api/logout", json={})
        assert (await client.get(f"/admin/api/{path}")).status == 401
        await client.post("/admin/api/login", json={"password": "pass-1234"})


async def test_vendor_test_endpoint(client, monkeypatch):
    seen = {}

    def fake_test(config):
        seen.update(config)
        return {"ok": False, "error": "API Key 不对或已失效"}
    monkeypatch.setattr(ah.AdminHandler, "_test_llm", staticmethod(fake_test))
    r = await client.post("/admin/api/llm/vendors/openai/test", json={"model": "gpt-5-mini", "api_key": "sk-x"})
    assert r.status == 400 and (await r.json())["error"] == "API Key 不对或已失效"
    assert seen["base_url"] == "https://api.openai.com/v1" and seen["timeout"] == 20
    r = await client.post("/admin/api/llm/vendors/openai/test", json={"model": "gpt-5-mini"})
    assert r.status == 400 and "API Key" in (await r.json())["error"]


async def test_emotion_single_and_stop(client):
    sent = []

    class FakeWS:
        async def send(self, data):
            sent.append(data)

    fake = SimpleNamespace(session_id="s-9", device_id="dev-9", client_ip="x", websocket=FakeWS())
    connections.register(fake)
    try:
        r = await client.post("/admin/api/devices/s-9/emotion", json={"emotion": "happy"})
        assert r.status == 200 and '"emotion": "happy"' in sent[-1]
        assert (await client.post("/admin/api/devices/s-9/emotion", json={"emotion": "nope"})).status == 400
        assert (await client.post("/admin/api/devices/none/emotion", json={"emotion": "happy"})).status == 404
        await client.post("/admin/api/devices/s-9/emotion-test", json={"interval": 1})
        r = await client.post("/admin/api/devices/s-9/emotion-test/stop", json={})
        assert r.status == 200
        for _ in range(50):
            await asyncio.sleep(0)
        devices = (await (await client.get("/admin/api/devices")).json())
        progress = devices["devices"][0]["emotion_test"]
        assert progress["running"] is False and progress["total"] == 21
        assert len(devices["emotions"]) == 21 and devices["emotions"][0]["label"]
    finally:
        connections.unregister(fake)

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


def test_claude_levels_and_web_switch(cfg_path):
    d = pages.describe_claude_cli(merged(cfg_path))
    assert d["level"] == "read" and d["web"] is False
    assert [o["value"] for o in d["options"]["model"]] == ["haiku", "sonnet", "opus", "fable"]
    assert [o["value"] for o in d["options"]["effort"]] == ["low", "medium", "high", "xhigh", "max"]
    pages.save_claude_cli({"level": "edit", "web": True, "effort": "xhigh"}, merged(cfg_path), cfg_path)
    e = merged(cfg_path)["LLM"]["ClaudeCLI"]
    assert e["permission_mode"] == "acceptEdits" and e["effort"] == "xhigh"
    assert e["allowed_tools"] == ["Read", "Grep", "Glob", "Write", "Edit", "WebSearch", "WebFetch"]
    d = pages.describe_claude_cli(merged(cfg_path))
    assert d["level"] == "edit" and d["web"] is True
    pages.save_claude_cli({"level": "auto", "web": False}, merged(cfg_path), cfg_path)
    e = merged(cfg_path)["LLM"]["ClaudeCLI"]
    assert e["permission_mode"] == "auto" and "Bash" in e["allowed_tools"] and "WebSearch" not in e["allowed_tools"]
    with pytest.raises(ValueError, match="权限级别"):
        pages.save_claude_cli({"level": "root"}, merged(cfg_path), cfg_path)


def test_claude_custom_level_keeps_tools(cfg_path):
    cfg_path.write_text(CUSTOM_YAML.replace("allowed_tools: [Read, Grep, Glob]", "allowed_tools: [Read, Bash]"), encoding="utf-8")
    assert pages.describe_claude_cli(merged(cfg_path))["level"] == "custom"
    pages.save_claude_cli({"level": "custom", "web": True}, merged(cfg_path), cfg_path)
    e = merged(cfg_path)["LLM"]["ClaudeCLI"]
    assert e["allowed_tools"] == ["Read", "Bash", "WebSearch", "WebFetch"] and e["permission_mode"] == "default"


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


def test_save_vad_silence(cfg_path):
    v = pages.describe_asr(merged(cfg_path))["vad"]
    assert v["selected"] == "SileroVAD" and v["supported"] and v["silence_ms"] == 800
    for bad in ("abc", "100", "6000", "1.5"):
        with pytest.raises(ValueError, match="200～5000"):
            pages.save_vad_silence(bad, merged(cfg_path), cfg_path)
    assert pages.save_vad_silence("1000", merged(cfg_path), cfg_path) == 1000
    assert merged(cfg_path)["VAD"]["SileroVAD"]["min_silence_duration_ms"] == 1000
    assert pages.save_vad_silence(1000, merged(cfg_path), cfg_path) is None  # 没变不写


def test_describe_vad_flags_stream_asr(cfg_path):
    cfg_path.write_text(CUSTOM_YAML.replace("selected_module:\n", "selected_module:\n  ASR: AliyunStreamASR\n"),
                        encoding="utf-8")
    assert pages.describe_vad(merged(cfg_path))["asr_stream"] is True


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


# ---------- 呼名 ----------

def test_claude_cli_new_fields_can_be_added(cfg_path):
    """老配置里没有的进度提示两项，后台也能设置；留空不写入、不报错。"""
    assert pages.save_claude_cli({"progress_interval_seconds": "", "progress_reply": ""}, merged(cfg_path), cfg_path) == []
    changed = pages.save_claude_cli({"progress_interval_seconds": "45", "progress_reply": "还在弄"}, merged(cfg_path), cfg_path)
    assert set(changed) == {"progress_interval_seconds", "progress_reply"}
    entry = merged(cfg_path)["LLM"]["ClaudeCLI"]
    assert entry["progress_interval_seconds"] == 45 and entry["progress_reply"] == "还在弄"
    with pytest.raises(ValueError):
        pages.save_claude_cli({"progress_interval_seconds": "-1"}, merged(cfg_path), cfg_path)
    assert pages.save_claude_cli({"progress_interval_seconds": "0"}, merged(cfg_path), cfg_path)  # 0 表示关闭


def test_describe_call_names_defaults(cfg_path):
    d = pages.describe_call_names(merged(cfg_path))
    assert d["enabled"] is True and d["default"] == "ClaudeCLI"
    assert [m["entry"] for m in d["models"]] == ["ClaudeCLI"] and "cloud" in d["models"][0]["aliases"]
    assert d["models"][0]["ready"] is True
    assert [t["entry"] for t in d["targets"]] == ["ClaudeCLI"]  # API 厂商都还没填 Key，不能选


def _configure_deepseek(path):
    pages.save_vendor("deepseek", {"base_url": "https://api.deepseek.com", "api_key": "sk-1234567890abcdef",
                                   "model": "deepseek-chat"}, merged(path), path)


def test_describe_call_names_legacy_key_and_targets(cfg_path):
    with config_store.edit_custom(cfg_path) as data:
        data["call_names"] = {"models": {"claude": ["cloud"]}}  # 第一期的写法
    _configure_deepseek(cfg_path)
    d = pages.describe_call_names(merged(cfg_path))
    assert [(m["entry"], m["aliases"]) for m in d["models"]] == [("ClaudeCLI", ["cloud"])]
    assert {t["entry"]: t["suggest"] for t in d["targets"]}["DeepSeekLLM"] == ["deepseek"]


def test_save_call_names_validates_and_writes(cfg_path):
    _configure_deepseek(cfg_path)
    payload = {"reply": "在呢", "models": [{"entry": "ClaudeCLI", "aliases": ["Cloud", "克劳德", "cloud", " "]},
                                          {"entry": "DeepSeekLLM", "aliases": ["深度求索"]}]}
    changed = pages.save_call_names(payload, merged(cfg_path), cfg_path)
    assert set(changed) == {"models", "reply"}
    d = pages.describe_call_names(merged(cfg_path))
    assert [(m["entry"], m["aliases"]) for m in d["models"]] == [("ClaudeCLI", ["cloud", "克劳德"]),
                                                                ("DeepSeekLLM", ["深度求索"])]  # 去重、去空、转小写
    assert d["reply"] == "在呢"
    assert pages.save_call_names(payload, merged(cfg_path), cfg_path) == []

    def models(*items):
        return {"models": [{"entry": e, "aliases": a} for e, a in items]}

    for bad, msg in ((models(("ClaudeCLI", ["cloud code"])), "空格或标点"), (models(("ClaudeCLI", ["c"])), "2 到 20"),
                     (models(("ClaudeCLI", ["hi"])), "招呼语"), (models(("ClaudeCLI", [])), "至少要有一个称呼"),
                     (models(), "至少要给一个模型"), (models(("KimiLLM", ["kimi"])), "还没配置好"),
                     (models(("ClaudeCLI", ["cloud"]), ("DeepSeekLLM", ["cloud"])), "只能给一个模型"),
                     (models(("ClaudeCLI", ["cloud"]), ("ClaudeCLI", ["claude"])), "重复")):
        with pytest.raises(ValueError, match=msg):
            pages.save_call_names(bad, merged(cfg_path), cfg_path)
    assert pages.save_call_names({"models": [], "enabled": False}, merged(cfg_path), cfg_path)  # 关闭时可以不留称呼


def test_save_call_names_keeps_model_no_longer_configured(cfg_path):
    _configure_deepseek(cfg_path)
    pages.save_call_names({"models": [{"entry": "DeepSeekLLM", "aliases": ["deepseek"]}]}, merged(cfg_path), cfg_path)
    with config_store.edit_custom(cfg_path) as data:
        data["LLM"]["DeepSeekLLM"]["api_key"] = "你的key"  # 之后 Key 被清掉了
    d = pages.describe_call_names(merged(cfg_path))
    assert d["models"][0]["ready"] is False
    # 原来就有的可以保留，只改称呼也能保存
    assert pages.save_call_names({"models": [{"entry": "DeepSeekLLM", "aliases": ["deepseek", "深度"]}]},
                                 merged(cfg_path), cfg_path) == ["models"]


async def test_call_names_endpoints_apply_to_online_connections(client):
    from core.agent.call_names import learner

    fake = SimpleNamespace(session_id="s-7", device_id="dev-7", client_ip="x", config={})
    connections.register(fake)
    try:
        d = await (await client.get("/admin/api/call-names")).json()
        assert d["ok"] and d["devices"][0]["session_id"] == "s-7" and d["learning"]["active"] is False
        r = await client.post("/admin/api/call-names", json={"models": [{"entry": "ClaudeCLI", "aliases": ["cloud", "克老德"]}]})
        assert r.status == 200 and (await r.json())["changed"] == ["models"]
        assert fake.config["call_names"]["models"]["ClaudeCLI"] == ["cloud", "克老德"]  # 在线连接马上生效
        assert client.handler.config["call_names"]["models"]["ClaudeCLI"] == ["cloud", "克老德"]
        bad = {"models": [{"entry": "ClaudeCLI", "aliases": ["x y"]}]}
        assert (await client.post("/admin/api/call-names", json=bad)).status == 400

        r = await (await client.post("/admin/api/call-names/learn", json={"target": "ClaudeCLI"})).json()
        assert r["learning"]["active"] and r["learning"]["session_id"] == "s-7"  # 只有一台在线时自动选它
        assert r["learning"]["target"] == "ClaudeCLI"
        assert (await client.post("/admin/api/call-names/learn", json={"session_id": "gone"})).status == 400
        r = await (await client.post("/admin/api/call-names/learn/stop", json={})).json()
        assert r["learning"]["active"] is False
    finally:
        learner.stop()
        connections.unregister(fake)
    assert (await client.post("/admin/api/call-names/learn", json={})).status == 400  # 没有设备在线


async def test_vad_save_applies_to_running_instances(cfg_path):
    shared = SimpleNamespace(silence_threshold_ms=200)
    own = SimpleNamespace(silence_threshold_ms=200)  # 设备有私有配置时，连接用的是自己的实例
    ws = SimpleNamespace(config_lock=asyncio.Lock(), _vad=shared)
    running = {"selected_module": {"VAD": "SileroVAD"}, "VAD": {"SileroVAD": {"min_silence_duration_ms": 200}}}
    app = web.Application()
    handler = ah.AdminHandler(running, ws_server=ws, config_path=cfg_path)
    handler.register(app)
    fake = SimpleNamespace(session_id="s-8", device_id="dev-8", client_ip="x", vad=own,
                           config={"VAD": {"SileroVAD": {"min_silence_duration_ms": 200}}})
    connections.register(fake)
    try:
        async with TestClient(TestServer(app)) as c:
            await c.post("/admin/api/setup", json={"password": "pass-1234"})
            r = await c.post("/admin/api/vad", json={"silence_ms": "6000"})
            assert r.status == 400 and "200～5000" in (await r.json())["error"]
            r = await (await c.post("/admin/api/vad", json={"silence_ms": "900"})).json()
            assert r["note"] == "已保存，马上生效"
            assert shared.silence_threshold_ms == 900 and own.silence_threshold_ms == 900
            assert running["VAD"]["SileroVAD"]["min_silence_duration_ms"] == 900
            assert fake.config["VAD"]["SileroVAD"]["min_silence_duration_ms"] == 900
            assert merged(cfg_path)["VAD"]["SileroVAD"]["min_silence_duration_ms"] == 900
            r = await (await c.post("/admin/api/vad", json={"silence_ms": 900})).json()
            assert r["note"] == "没有修改"
            await c.post("/admin/api/logout", json={})
            assert (await c.post("/admin/api/vad", json={"silence_ms": 800})).status == 401
    finally:
        connections.unregister(fake)

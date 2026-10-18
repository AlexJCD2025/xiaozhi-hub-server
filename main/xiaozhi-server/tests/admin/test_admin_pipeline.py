"""管理后台"工作原理"页：流程按配置生成，提示词拆解的各段和变量标记。全部使用临时配置文件。"""

import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from core.admin import config_store, connections, pipeline
from core.admin.admin_handler import AdminHandler

CLI_YAML = """\
selected_module:
  LLM: ClaudeCLI
  Intent: nointent
tts_audio_prebuffer_count: 15
LLM:
  ClaudeCLI:
    type: claude_cli
    model: haiku
    effort: low
    workdir: ~/xiaozhi-workspace
    allowed_tools: [Read, Grep, Glob]
    resume_keywords: [接着上次聊]
    first_reply_filler: 好的，我看一下
"""

API_YAML = """\
selected_module:
  LLM: DeepSeekLLM
  Intent: function_call
Intent:
  function_call:
    functions: [get_weather, play_music]
"""


@pytest.fixture
def cli_path(tmp_path) -> Path:
    p = tmp_path / ".config.yaml"
    p.write_text(CLI_YAML, encoding="utf-8")
    return p


def merged(path):
    return config_store.load_merged(custom_path=path)


def marks(text):
    return re.findall(f"{pipeline.MARK_START}(\\w+){pipeline.MARK_SEP}", text)


def test_flow_follows_config(cli_path):
    flow = pipeline.describe_flow(merged(cli_path))
    stages = {s["id"]: s for s in flow["stages"]}
    assert list(stages) == ["listen", "asr", "intent", "llm", "emotion", "tts", "play"]
    assert stages["llm"]["module"] == "Claude Code CLI" and "haiku" in stages["llm"]["summary"]
    assert stages["intent"]["module"] == "不用插件"
    assert stages["play"]["module"] == "预缓冲 15 帧" and "0.9" in stages["play"]["summary"]
    assert "200ms" in stages["listen"]["summary"]  # 上游 SileroVAD 的 min_silence_duration_ms
    titles = [b["title"] for b in flow["branches"]]
    assert "填充语" in titles and "接续上次对话" in titles and "唤醒词" in titles
    assert [m["type"] for m in flow["messages"]][0] == "stt"


def test_prompt_segments_for_claude_cli(cli_path):
    p = pipeline.describe_prompt(merged(cli_path))
    assert [s["id"] for s in p["segments"]] == ["claude_preset", "base", "voice_rules", "turn"]
    base = next(s for s in p["segments"] if s["id"] == "base")["text"]
    found = set(marks(base))
    assert {"base_prompt", "today_date", "lunar_date", "local_address", "weather_info", "emojiList", "language"} <= found
    assert "<identity>" in base and "语音助手规则" in p["segments"][2]["text"]
    assert any("台湾" in w for w in p["warnings"]) and any("进程启动时" in w for w in p["warnings"])
    assert p["actual"] is None  # 没有在线设备


def test_prompt_segments_for_api_with_plugins(tmp_path):
    path = tmp_path / ".config.yaml"
    path.write_text(API_YAML, encoding="utf-8")
    m = merged(path)
    p = pipeline.describe_prompt(m)
    assert p["llm"] == "DeepSeek"
    assert [s["id"] for s in p["segments"]] == ["base", "fewshot", "tools", "history"]
    tools = next(s for s in p["segments"] if s["id"] == "tools")["text"]
    assert "查天气" in tools and "放音乐" in tools and "热点新闻" not in tools
    assert not any("进程启动时" in w for w in p["warnings"])
    flow = pipeline.describe_flow(m)
    assert flow["stages"][2]["module"] == "用插件" and "填充语" not in [b["title"] for b in flow["branches"]]


def test_actual_prompt_from_online_device(cli_path):
    msg = SimpleNamespace(role="system", content="真实的系统提示词")
    conn = SimpleNamespace(session_id="s-p", device_id="dev-p", client_ip="192.168.6.211",
                           dialogue=SimpleNamespace(dialogue=[msg]))
    connections.register(conn)
    try:
        p = pipeline.describe_prompt(merged(cli_path))
        assert p["actual"] == {"device_id": "dev-p", "text": "真实的系统提示词"}
    finally:
        connections.unregister(conn)


async def test_pipeline_endpoint_requires_login(cli_path):
    app = web.Application()
    AdminHandler({"selected_module": {"LLM": "ClaudeCLI"}}, config_path=cli_path).register(app)
    async with TestClient(TestServer(app)) as c:
        assert (await c.get("/admin/api/pipeline")).status == 401
        await c.post("/admin/api/setup", json={"password": "pass-1234"})
        data = await (await c.get("/admin/api/pipeline")).json()
        assert data["ok"] and data["flow"]["stages"] and data["prompt"]["segments"]

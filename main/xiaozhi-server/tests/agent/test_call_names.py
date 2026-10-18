"""呼名匹配的单元测试。用例取自真人录音的语音识别结果（工作空间 asr-test/call-names/report.md）。"""

from types import SimpleNamespace

import pytest

from core.agent import call_names
from core.agent.call_names import handle_call_name, match_call_name


@pytest.mark.parametrize("asr_text", ["Hi, cloud.", "Hi, cloud. Hi, cloud.", "High cloud.", "嗨 Claude", "hey，克劳德！"])
def test_only_call_name(asr_text):
    assert match_call_name(asr_text) == ("claude", "")


@pytest.mark.parametrize("asr_text, rest", [
    ("嗨cloud接着上次聊。", "接着上次聊。"),
    ("嗨cloud记得上次聊。", "记得上次聊。"),
    ("嗨cloud,今天天气怎么样？", "今天天气怎么样？"),
    ("hicloud,今天天气怎么样？", "今天天气怎么样？"),
    ("hiigh cloud,今天天气怎么样？", "今天天气怎么样？"),
    ("Hi Claude, 用 Python 写个脚本", "用 Python 写个脚本"),
])
def test_call_name_with_content(asr_text, rest):
    assert match_call_name(asr_text) == ("claude", rest)


@pytest.mark.parametrize("asr_text", [
    "cloud code现在是什么版本？",      # 没有"嗨"开头
    "codeex和cloud哪个好用？",
    "嗨你好。",
    "嗨,你怎么看？",                  # Grok 被吞掉，按没有称呼处理
    "Hi, Koex.",                      # Codex 暂不支持
    "嗨 cloudflare 怎么配置",          # 称呼后面紧跟字母，不是称呼
    "今天嗨 cloud 一下",               # 不在句首
    "",
])
def test_not_call_name(asr_text):
    assert match_call_name(asr_text) is None


def test_custom_models():
    models = {"grok": ["格罗克"]}
    assert match_call_name("嗨 格罗克，你怎么看", models) == ("grok", "你怎么看")
    assert match_call_name("嗨 cloud", models) is None


conn_messages = []


def make_conn(cfg=None):
    logger = SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None)
    return SimpleNamespace(
        config={"call_names": cfg} if cfg is not None else {},
        logger=SimpleNamespace(bind=lambda **k: logger),
        dialogue=SimpleNamespace(put=conn_messages.append),
        client_abort=True, sentence_id=None, tts=object(),
    )


@pytest.fixture
def spoken(monkeypatch):
    out = {"stt": [], "tts": []}

    async def fake_stt(conn, text):
        out["stt"].append(text)

    import core.handle.intentHandler as intent_mod
    import core.handle.sendAudioHandle as send_mod
    monkeypatch.setattr(send_mod, "send_stt_message", fake_stt)
    monkeypatch.setattr(intent_mod, "speak_txt", lambda conn, text: out["tts"].append(text))
    conn_messages.clear()
    return out


async def test_handle_passes_through_without_call_name(spoken):
    assert await handle_call_name(make_conn(), "今天天气怎么样") == "今天天气怎么样"
    assert spoken == {"stt": [], "tts": []}


async def test_handle_strips_call_name(spoken):
    assert await handle_call_name(make_conn(), "嗨cloud接着上次聊。") == "接着上次聊。"
    assert spoken["tts"] == []


async def test_handle_only_call_name_replies_without_model(spoken):
    c = make_conn()
    assert await handle_call_name(c, "Hi, cloud.") is None
    assert spoken == {"stt": ["Hi, cloud."], "tts": [call_names.DEFAULT_REPLY]}
    assert c.client_abort is False and c.sentence_id
    assert [m.role for m in conn_messages] == ["user"]


async def test_handle_waits_for_tts_then_falls_back(spoken, monkeypatch):
    c = make_conn()
    c.tts = None  # 刚连上，TTS 还没初始化好
    clock = iter(range(100))
    monkeypatch.setattr(call_names.time, "monotonic", lambda: next(clock))
    assert await handle_call_name(c, "Hi, cloud.") == "Hi, cloud."  # 等不到 TTS，按普通对话处理
    assert spoken["tts"] == []


async def test_handle_disabled_and_custom_reply(spoken):
    assert await handle_call_name(make_conn({"enabled": False}), "Hi, cloud.") == "Hi, cloud."
    assert await handle_call_name(make_conn({"reply": "Claude 在呢"}), "嗨 Claude") is None
    assert spoken["tts"] == ["Claude 在呢"]

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
    "嗨cloud code现在是什么版本？",     # 问的是 Claude Code，不是在叫 Claude
    "嗨 Claude Code 怎么安装",
    "今天嗨 cloud 一下",               # 不在句首
    "",
])
def test_not_call_name(asr_text):
    assert match_call_name(asr_text) is None


def test_custom_models():
    models = {"grok": ["格罗克"]}
    assert match_call_name("嗨 格罗克，你怎么看", models) == ("grok", "你怎么看")
    assert match_call_name("嗨 cloud", models) is None


def test_alias_written_as_string():
    models = {"claude": "cloud"}  # 配置里写成字符串而不是列表
    assert match_call_name("嗨 cloud", models) == ("claude", "")
    assert match_call_name("嗨 do 一下", models) is None  # 不能拆成单个字符去匹配


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


# ---------- 录入 ----------

@pytest.mark.parametrize("asr_text, alias", [
    ("嗨cloud。", "cloud"), ("Hi, cloud. Hi, cloud.", "cloud"), ("High cloud.", "cloud"),
    ("hicloud", "cloud"), ("嘿 克劳德！", "克劳德"), ("克劳德", "克劳德"), ("嗨。", ""),
])
def test_extract_alias(asr_text, alias):
    assert call_names.extract_alias(asr_text) == alias


def test_learner_counts_and_targets_device():
    lr = call_names.Learner()
    assert lr.capture("s1", "嗨 cloud") is None  # 没开始录入
    lr.start("s1", count=2)
    assert lr.capture("s2", "嗨 cloud") is None  # 不是目标设备
    assert lr.capture("s1", "嗨 cloud") == (1, 2)
    assert lr.capture("s1", "嗨 克劳德") == (2, 2)
    assert lr.capture("s1", "嗨 第三遍") is None  # 录满了
    st = lr.status()
    assert st["active"] is False and [x["alias"] for x in st["samples"]] == ["cloud", "克劳德"]
    lr.start(None, count=1, timeout=-1)
    assert lr.capture("s1", "嗨 cloud") is None  # 超时


async def test_learning_intercepts_before_model(spoken, monkeypatch):
    lr = call_names.Learner()
    monkeypatch.setattr(call_names, "learner", lr)
    c = make_conn()
    c.session_id = "s1"
    c.config["wakeup_words"] = ["嘿你好呀"]
    lr.start("s1", count=2)
    assert await handle_call_name(c, "嘿，你好呀") == "嘿，你好呀"  # 唤醒词不算一遍
    assert await handle_call_name(c, "嗨cloud。") is None
    assert spoken["tts"] == ["收到第 1 遍"]
    assert await handle_call_name(c, "嗨 克劳德") is None
    assert spoken["tts"][-1] == "录好了，请到管理后台确认"
    assert await handle_call_name(c, "今天天气怎么样") == "今天天气怎么样"  # 录满后恢复正常

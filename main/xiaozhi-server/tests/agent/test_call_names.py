"""呼名匹配的单元测试。用例取自真人录音的语音识别结果（工作空间 asr-test/call-names/report.md）。"""

from types import SimpleNamespace

import pytest

from core.admin import catalog  # noqa: F401  提前导入：在事件循环里第一次导入会触发加载配置
from core.agent import call_names, llm_router
from core.agent.call_names import handle_call_name, match_call_name


@pytest.mark.parametrize("asr_text", ["Hi, cloud.", "Hi, cloud. Hi, cloud.", "High cloud.", "嗨 Claude", "hey，克劳德！",
                                      "He cloud.", "Heい cloud。", "Hei cloud", "H, cloud."])  # "嘿"被识别成 he、h（真机实测）
def test_only_call_name(asr_text):
    assert match_call_name(asr_text) == ("ClaudeCLI", "")


@pytest.mark.parametrize("asr_text, rest", [
    ("嗨cloud接着上次聊。", "接着上次聊。"),
    ("嗨cloud记得上次聊。", "记得上次聊。"),
    ("嗨cloud,今天天气怎么样？", "今天天气怎么样？"),
    ("hicloud,今天天气怎么样？", "今天天气怎么样？"),
    ("hiigh cloud,今天天气怎么样？", "今天天气怎么样？"),
    ("Hi Claude, 用 Python 写个脚本", "用 Python 写个脚本"),
])
def test_call_name_with_content(asr_text, rest):
    assert match_call_name(asr_text) == ("ClaudeCLI", rest)


@pytest.mark.parametrize("asr_text", [
    "cloud code现在是什么版本？",      # 没有"嗨"开头
    "codeex和cloud哪个好用？",
    "嗨你好。",
    "嗨,你怎么看？",                  # Grok 被吞掉，按没有称呼处理
    "Hi, Koex.",                      # 和 codex / codeex 差太多，不当成呼名
    "嗨 cloudflare 怎么配置",          # 称呼后面紧跟字母，不是称呼
    "嗨cloud code现在是什么版本？",     # 问的是 Claude Code，不是在叫 Claude
    "嗨 Claude Code 怎么安装",
    "今天嗨 cloud 一下",               # 不在句首
    "Hello cloud",                    # hello 不是招呼语 he
    "Help cloud 怎么用",
    "Hey.",                           # 称呼没识别出来（真机实测）
    "H5 cloud 页面",                   # h 后面紧跟别的字符，不是招呼语
    "",
])
def test_not_call_name(asr_text):
    assert match_call_name(asr_text) is None


@pytest.mark.parametrize("asr_text, rest", [
    ("嗨 Codex", ""),
    ("Hi, codeex.", ""),
    ("hey codex，看看工作目录", "看看工作目录"),
])
def test_codex_call_name(asr_text, rest):
    assert match_call_name(asr_text) == ("CodexCLI", rest)


def test_custom_models():
    models = {"GrokLLM": ["格罗克"]}
    assert match_call_name("嗨 格罗克，你怎么看", models) == ("GrokLLM", "你怎么看")
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

    async def fake_switch(conn, name):
        out["switch"].append(name)
        return name not in out["unready"]

    import core.handle.intentHandler as intent_mod
    import core.handle.sendAudioHandle as send_mod
    monkeypatch.setattr(send_mod, "send_stt_message", fake_stt)
    monkeypatch.setattr(intent_mod, "speak_txt", lambda conn, text: out["tts"].append(text))
    monkeypatch.setattr(llm_router, "switch_async", fake_switch)
    conn_messages.clear()
    out.update(switch=[], unready=set())
    return out


async def test_handle_passes_through_without_call_name(spoken):
    assert await handle_call_name(make_conn(), "今天天气怎么样") == "今天天气怎么样"
    assert spoken["stt"] == [] and spoken["tts"] == [] and spoken["switch"] == []


async def test_handle_strips_call_name_and_switches(spoken):
    assert await handle_call_name(make_conn(), "嗨cloud接着上次聊。") == "接着上次聊。"
    assert spoken["tts"] == [] and spoken["switch"] == ["ClaudeCLI"]


async def test_handle_routes_by_configured_models(spoken):
    cfg = {"models": {"claude": ["cloud"], "DeepSeekLLM": ["深度求索"]}}  # 第一期的 "claude" 键也认
    assert await handle_call_name(make_conn(cfg), "嗨 深度求索，你好") == "你好"
    assert await handle_call_name(make_conn(cfg), "嗨 cloud") is None
    assert spoken["switch"] == ["DeepSeekLLM", "ClaudeCLI"]


async def test_handle_unready_model_tells_user(spoken):
    spoken["unready"].add("KimiLLM")
    c = make_conn({"models": {"KimiLLM": ["kimi"]}})
    assert await handle_call_name(c, "嗨 kimi，你好") is None  # 不交给当前模型，提示还没配置好
    assert spoken["tts"] == ["Kimi 还没配置好，请到管理后台检查"]


async def test_handle_only_call_name_replies_without_model(spoken):
    c = make_conn()
    assert await handle_call_name(c, "Hi, cloud.") is None
    assert spoken["stt"] == ["Hi, cloud."] and spoken["tts"] == [call_names.DEFAULT_REPLY]
    assert spoken["switch"] == ["ClaudeCLI"]  # 只说称呼也会切换并记住
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
    assert await handle_call_name(make_conn({"reply": "我在，请说。"}), "嗨 Claude") is None
    assert spoken["tts"] == ["Claude 在呢", "我在 请说"]  # 去掉句中标点，一次合成，不切成两段


# ---------- 录入 ----------

@pytest.mark.parametrize("asr_text, alias", [
    ("嗨cloud。", "cloud"), ("Hi, cloud. Hi, cloud.", "cloud"), ("High cloud.", "cloud"),
    ("hicloud", "cloud"), ("嘿 克劳德！", "克劳德"), ("Heい cloud。", "cloud"), ("He cloud.", "cloud"), ("H, cloud.", "cloud"), ("克劳德", "克劳德"), ("嗨。", ""),
])
def test_extract_alias(asr_text, alias):
    assert call_names.extract_alias(asr_text) == alias


def test_learner_counts_and_targets_device():
    lr = call_names.Learner()
    assert lr.capture("s1", "嗨 cloud") is None  # 没开始录入
    lr.start("s1", count=2, target="ClaudeCLI")
    assert lr.status()["target"] == "ClaudeCLI"
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


async def test_only_call_name_reply_audio_is_cached(spoken, monkeypatch):
    """固定回复第一次照常合成并在后台缓存，之后直接发缓存的音频，不再等 TTS。"""
    import asyncio

    import core.handle.sendAudioHandle as send_mod

    sent = []
    real_sleep = asyncio.sleep

    async def fake_send_audio(conn, sentence_type, audios, text, sentence_id=None):
        sent.append((sentence_type.name, audios, text))

    async def record_sleep(delay, *args, **kwargs):
        sent.append(("sleep", delay))
        await real_sleep(0)

    monkeypatch.setattr(send_mod, "sendAudioMessage", fake_send_audio)
    monkeypatch.setattr(call_names, "_audio_cache", {})
    c = make_conn()
    c.tts = SimpleNamespace(voice="v1", to_tts=lambda text: [b"opus-" + text.encode()])
    c.sample_rate = 24000

    assert await handle_call_name(c, "嗨 cloud") is None
    assert spoken["tts"] == [call_names.DEFAULT_REPLY] and sent == []  # 第一次走正常合成
    # 等后台缓存任务结束（测试环境的 asyncio.sleep 不真正等待）
    await asyncio.gather(*(t for t in asyncio.all_tasks() if t is not asyncio.current_task()))
    monkeypatch.setattr(asyncio, "sleep", record_sleep)
    assert await handle_call_name(c, "嗨 cloud") is None
    assert spoken["tts"] == [call_names.DEFAULT_REPLY]  # 第二次不再合成
    # 先等设备切到说话状态再发音频，否则音频会被设备丢掉
    assert sent == [("sleep", call_names.CACHED_REPLY_DELAY),
                    ("FIRST", [b"opus-" + call_names.DEFAULT_REPLY.encode()], call_names.DEFAULT_REPLY),
                    ("LAST", [], None)]


async def test_only_call_name_speaks_pending_report_of_grok_bot(spoken):
    from core.agent import reports

    reports.clear()
    c = make_conn({"models": {"GrokBot_孙悟空": ["孙悟空"]}})
    c.config["LLM"] = {"GrokBot_孙悟空": {"type": "grok_bot", "bot_id": "bot-1", "bot_name": "孙悟空"}}
    c.session_id, c.device_id = "s-1", "dev-1"
    reports.add("dev-1", "bot-1", "孙悟空做完了：好。")
    assert await handle_call_name(c, "嗨 孙悟空") is None
    assert spoken["tts"] == [call_names.DEFAULT_REPLY + "。孙悟空做完了：好。"]
    assert not reports.pending("dev-1")
    assert await handle_call_name(c, "嗨 孙悟空") is None
    assert spoken["tts"][-1] == call_names.DEFAULT_REPLY  # 念过就没有了


async def test_pending_report_put_back_when_tts_not_ready(spoken, monkeypatch):
    from core.agent import reports

    reports.clear()
    c = make_conn({"models": {"GrokBot_孙悟空": ["孙悟空"]}})
    c.config["LLM"] = {"GrokBot_孙悟空": {"type": "grok_bot", "bot_id": "bot-1", "bot_name": "孙悟空"}}
    c.session_id, c.device_id = "s-1", "dev-1"
    c.tts = None  # TTS 还没就绪，念不了
    clock = iter(range(100))
    monkeypatch.setattr(call_names.time, "monotonic", lambda: next(clock))
    reports.add("dev-1", "bot-1", "孙悟空做完了：好。")
    assert await handle_call_name(c, "嗨 孙悟空") == "嗨 孙悟空"
    assert spoken["tts"] == [] and reports.pending("dev-1", "bot-1")  # 没念成，放回去了
    reports.clear()

"""识别后纠错替换、热词自动并入、最近识别记录。"""

from types import SimpleNamespace

from core.agent import asr_text


def test_corrections_whole_word_and_spacing():
    table = asr_text.DEFAULT_CORRECTIONS
    fix = lambda t: asr_text.apply_corrections(t, table)
    assert fix("用 cloud code 帮我看一下这段代码。") == "用 Claude Code 帮我看一下这段代码。"
    assert fix("用Cloud Code，帮我") == "用Claude Code，帮我"
    assert fix("用cloudcode帮我") == "用Claude Code帮我"  # 中间没空格也算
    assert fix("你知道克拉德扣的吗") == "你知道Claude Code吗"
    # 英文按整词：不会改到更长的词里，单独的 cloud 也不动
    assert fix("cloud coder 是什么") == "cloud coder 是什么"
    assert fix("我在 cloud 上部署") == "我在 cloud 上部署"
    assert fix("") == ""


def test_corrections_longer_alias_first_and_custom_table():
    table = {"Claude": ["cloud"], "Claude Code": ["cloud code"]}
    assert asr_text.apply_corrections("cloud code 和 cloud", table) == "Claude Code 和 Claude"
    assert asr_text.apply_corrections("cloud code", {}) == "cloud code"


def test_corrections_of_config():
    assert asr_text.corrections_of({}) is asr_text.DEFAULT_CORRECTIONS
    assert asr_text.corrections_of({"asr_corrections": {}}) == {}  # 清空后不再用默认表
    assert asr_text.corrections_of({"asr_corrections": {"A": ["b"]}}) == {"A": ["b"]}


def test_correct_and_record_text_and_dict():
    conn = SimpleNamespace(config={"asr_corrections": {"GitHub": ["get hub"]}}, device_id="dev-1")
    assert asr_text.correct_and_record(conn, "打开 get hub") == "打开 GitHub"
    r = asr_text.recent()[0]
    assert r["raw"] == "打开 get hub" and r["text"] == "打开 GitHub" and r["device_id"] == "dev-1"
    out = asr_text.correct_and_record(conn, {"content": "get hub 上", "emotion": "happy"})
    assert out == {"content": "GitHub 上", "emotion": "happy"}
    assert asr_text.correct_and_record(conn, "") == ""


def test_call_name_hotwords_use_model_names_not_aliases():
    cfg = {"call_names": {"models": {"ClaudeCLI": ["cloud"], "DeepSeekLLM": ["深度"], "ChatGLMLLM": ["智谱"]}}}
    assert asr_text.call_name_hotwords(cfg) == ["Claude", "DeepSeek", "智谱"]
    assert asr_text.call_name_hotwords({}) == ["Claude"]  # 没配时用默认呼名
    assert asr_text.call_name_hotwords({"call_names": {"enabled": False}}) == []
    both = {"call_names": {"models": {"ClaudeCLI": ["cloud"], "ClaudeAPI": ["claude api"]}}}
    assert asr_text.call_name_hotwords(both) == ["Claude"]  # 本机和 API 都叫 Claude，只留一个


def test_effective_hotwords_merge_dedupe():
    entry = {"hotwords": ["claude", "GitHub"]}
    assert asr_text.effective_hotwords(entry, {}) == ["claude", "GitHub"]  # Claude 已有（不分大小写）
    assert asr_text.effective_hotwords({"hotwords": []}, {}) == ["Claude"]
    assert asr_text.effective_hotwords({}, {}) == []  # 条目不支持热词

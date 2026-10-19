"""识别后纠错替换、热词、最近识别记录。"""

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


XASR = {"type": "sherpa_onnx_local", "model_type": "transducer"}
DOUBAO = {"type": "doubao_stream"}


def test_hotwords_supported():
    assert asr_text.hotwords_supported(XASR) and asr_text.hotwords_supported(DOUBAO)
    assert not asr_text.hotwords_supported({"type": "sherpa_onnx_local", "model_type": "sense_voice"})
    assert not asr_text.hotwords_supported({"type": "fun_local"}) and not asr_text.hotwords_supported({})


def test_hotwords_of_shared_legacy_default():
    assert asr_text.hotwords_of({}) == asr_text.DEFAULT_HOTWORDS
    # 旧版本存在 X-ASR 条目里的还能读到
    legacy = {"ASR": {"SherpaXASR": {**XASR, "hotwords": ["GitHub"]}}}
    assert asr_text.hotwords_of(legacy) == ["GitHub"]
    # 顶层的 asr_hotwords 优先，清空后也不再用默认的
    assert asr_text.hotwords_of({**legacy, "asr_hotwords": ["Docker"]}) == ["Docker"]
    assert asr_text.hotwords_of({"asr_hotwords": []}) == []


def test_effective_hotwords_merge_dedupe():
    cfg = {"asr_hotwords": ["claude", "GitHub"]}
    assert asr_text.effective_hotwords(XASR, cfg) == ["claude", "GitHub"]  # Claude 已有（不分大小写）
    # 呼名带入的放在前面，豆包截断时优先保住
    assert asr_text.effective_hotwords(DOUBAO, {"asr_hotwords": ["GitHub"]}) == ["Claude", "GitHub"]
    assert asr_text.effective_hotwords({"type": "fun_local"}, cfg) == []  # 条目不支持热词


def test_doubao_hotwords_token_budget():
    assert asr_text.estimate_tokens("Claude Code") == 2 + 2 + 1 and asr_text.estimate_tokens("纠错") == 3
    default = ["Claude"] + asr_text.DEFAULT_HOTWORDS
    kept, dropped = asr_text.doubao_hotwords(default)
    assert kept == default and dropped == []  # 默认热词放得下
    many = [f"word{i:03d}" for i in range(40)]
    kept, dropped = asr_text.doubao_hotwords(many)
    assert sum(map(asr_text.estimate_tokens, kept)) <= 100 and kept + dropped == many and dropped
    assert asr_text.doubao_hotwords(many, multilingual=True) == (many, [])  # 多语种接口能带 5000 个


def test_corrections_target_with_backslash():
    """正确写法里的反斜杠按原样替换，不能当成正则的转义或分组引用（之前会报错，整句话被丢掉）。"""
    table = {"C:\\Users": ["c 盘用户"], "a\\1b": ["xx"]}
    assert asr_text.apply_corrections("打开 c 盘用户", table) == "打开 C:\\Users"
    assert asr_text.apply_corrections("xx", table) == "a\\1b"


def test_correct_and_record_never_breaks_recognition(monkeypatch):
    def boom(*a):
        raise RuntimeError("bad table")
    monkeypatch.setattr(asr_text, "apply_corrections", boom)
    conn = SimpleNamespace(config={}, device_id="dev-2")
    assert asr_text.correct_and_record(conn, "原样返回") == "原样返回"

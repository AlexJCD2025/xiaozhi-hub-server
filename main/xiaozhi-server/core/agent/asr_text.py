"""语音识别的文字处理：热词、纠错替换、最近识别记录。

纠错替换：有些词发音几乎一样，换模型、加热词都救不回来（实测 Claude Code 总被识别成 "cloud code"
或"克拉德扣的"），只能在识别出文字后按替换表改正。替换表在配置的 asr_corrections 里，
格式是 {正确写法: [识别错的写法, ...]}，管理后台可以编辑。匹配时忽略大小写，中英文之间有没有空格都算；
英文开头或结尾的写法要求整词匹配，"cloud code" 不会改到 "cloud coder" 里。

最近识别记录只保存在内存里，供管理后台查看认错的词，一键加进热词或替换表。
"""

import re
import threading
import time
from collections import deque

from config.logger import setup_logging

TAG = __name__
logger = setup_logging()

# 默认替换表；config.yaml 的 asr_corrections 会整个覆盖它
DEFAULT_CORRECTIONS = {"Claude Code": ["cloud code", "克拉德扣的", "克劳德扣的", "克劳德code"]}

_ASCII = re.compile(r"[A-Za-z0-9]")
_TOKEN = re.compile(r"[A-Za-z0-9]+|\S")


def _aliases(value) -> list:
    if isinstance(value, str):
        value = [value]
    return [str(a).strip() for a in (value or []) if str(a).strip()]


def _alias_regex(alias: str) -> str:
    """把一种写法变成正则：词与词之间允许任意空格；以英文字母或数字开头/结尾时要求整词。"""
    tokens = _TOKEN.findall(alias)
    body = r"\s*".join(re.escape(t) for t in tokens)
    if _ASCII.match(tokens[0]):
        body = r"(?<![A-Za-z0-9])" + body
    if _ASCII.match(tokens[-1]):
        body = body + r"(?![A-Za-z0-9])"
    return body


_cache = {"key": None, "rules": []}
_cache_lock = threading.Lock()


def _rules(corrections: dict) -> list:
    key = repr(sorted((str(k), tuple(_aliases(v))) for k, v in (corrections or {}).items()))
    with _cache_lock:
        if _cache["key"] == key:
            return _cache["rules"]
    pairs = [(alias, str(target).strip()) for target, value in (corrections or {}).items() if str(target).strip()
             for alias in _aliases(value)]
    # 长的写法先替换，避免 "cloud" 抢在 "cloud code" 前面
    pairs.sort(key=lambda p: len(p[0]), reverse=True)
    rules = [(re.compile(_alias_regex(alias), re.IGNORECASE), target) for alias, target in pairs]
    with _cache_lock:
        _cache["key"], _cache["rules"] = key, rules
    return rules


def corrections_of(config: dict) -> dict:
    value = (config or {}).get("asr_corrections")
    return DEFAULT_CORRECTIONS if value is None else (value or {})


def apply_corrections(text: str, corrections: dict) -> str:
    if not text:
        return text
    for pattern, target in _rules(corrections):
        # 用函数返回替换内容：正确写法里的反斜杠（如 C:\Users）不能当成正则的转义或分组引用
        text = pattern.sub(lambda _m, t=target: t, text)
    return text


# ---------- 热词 ----------
# 热词是所有语音识别共用的一份，放在配置顶层的 asr_hotwords；只有支持热词的识别会用上。
# 以前存在 X-ASR 条目的 hotwords 里，读的时候兼容，后台保存时搬到 asr_hotwords。

DEFAULT_HOTWORDS = [
    "Claude", "Claude Code", "Sonnet", "Opus", "Haiku", "Fable", "Codex", "GitHub", "StackChan",
    "Python", "Docker", "VS Code", "Markdown", "README", "JSON", "API", "npm", "commit", "push",
    "pull request", "issue", "PR", "bug", "debug",
]
# 支持热词的 sherpa-onnx 模型类型：transducer（X-ASR）解码时给热词加分；funasr_nano / qwen3_asr 把热词写进提示
HOTWORD_MODEL_TYPES = {"transducer", "funasr_nano", "qwen3_asr"}
# 豆包流式识别把热词直接放在请求里（corpus.context）
HOTWORD_TYPES = {"doubao_stream"}


def hotwords_supported(asr_entry: dict) -> bool:
    entry = asr_entry or {}
    if entry.get("type") == "sherpa_onnx_local":
        return entry.get("model_type") in HOTWORD_MODEL_TYPES
    return entry.get("type") in HOTWORD_TYPES


def legacy_hotwords(config: dict):
    """旧版本存在识别条目里的热词；没有时返回 None。"""
    for entry in ((config or {}).get("ASR") or {}).values():
        if isinstance(entry, dict) and isinstance(entry.get("hotwords"), list):
            return _aliases(entry["hotwords"])
    return None


def hotwords_of(config: dict) -> list:
    """后台维护的热词：asr_hotwords → 旧版条目里的 → 默认列表。"""
    value = (config or {}).get("asr_hotwords")
    if value is not None:
        return _aliases(value)
    legacy = legacy_hotwords(config)
    return DEFAULT_HOTWORDS[:] if legacy is None else legacy


def call_name_hotwords(config: dict) -> list:
    """呼名里配了称呼的模型，把模型的正确名字（如 Claude、DeepSeek）作为热词。

    呼名的别名是"识别错的样子"（如 cloud），当热词反而会让识别更偏向错的写法，所以不用别名。
    """
    from core.admin import catalog
    from core.agent import call_names

    cfg = (config or {}).get("call_names") or {}
    if cfg.get("enabled") is False:
        return []
    models = cfg.get("models") or call_names.DEFAULT_MODELS
    words = []
    for entry in models:
        if entry == "ClaudeCLI":
            words.append("Claude")
        elif entry in catalog.VENDOR_BY_ENTRY:
            words.append(re.sub(r"\s*GLM$", "", catalog.VENDOR_BY_ENTRY[entry]["label"]) or entry)
    # 本机 Claude Code 和 Claude API 都配了称呼时都是 "Claude"，去重
    return list(dict.fromkeys(w for w in words if w != "自定义"))


def effective_hotwords(asr_entry: dict, config: dict) -> list:
    """呼名自动带入的 + 后台维护的热词，去重保序；条目不支持热词时返回空。

    呼名的模型名放在前面：豆包能带的热词有限，超出时从后面截掉，叫模型的名字要优先保住。"""
    if not hotwords_supported(asr_entry):
        return []
    configured = hotwords_of(config)
    seen = {w.lower() for w in configured}
    auto = [w for w in call_name_hotwords(config) if w.lower() not in seen]
    return auto + configured


# 豆包双向流式接口热词直传最多约 100 tokens，流式输入（多语种）接口最多 5000 个词。
# 官方没说怎么算 token，这里往多了估：一个汉字算 1 个，英文每 3 个字母算 1 个，每个词再加 1 个分隔
DOUBAO_HOTWORD_TOKENS = 100
DOUBAO_HOTWORD_WORDS = 5000


def estimate_tokens(word: str) -> int:
    ascii_runs = re.findall(r"[A-Za-z0-9]+", word)
    cjk = len(re.findall(r"[㐀-鿿]", word))
    return cjk + sum(-(-len(r) // 3) for r in ascii_runs) + 1


def doubao_hotwords(words: list, multilingual: bool = False, max_tokens: int = DOUBAO_HOTWORD_TOKENS):
    """按豆包的上限截取热词，返回 (带上的, 截掉的)。"""
    if multilingual:
        return words[:DOUBAO_HOTWORD_WORDS], words[DOUBAO_HOTWORD_WORDS:]
    total = 0
    for i, w in enumerate(words):
        total += estimate_tokens(w)
        if total > max_tokens:
            return words[:i], words[i:]
    return list(words), []


# ---------- 最近识别记录 ----------

_recent = deque(maxlen=30)
_recent_lock = threading.Lock()


def record(device_id: str, raw: str, text: str) -> None:
    if not raw:
        return
    with _recent_lock:
        _recent.appendleft({"time": time.time(), "device_id": device_id or "", "raw": raw, "text": text})


def recent() -> list:
    with _recent_lock:
        return list(_recent)


def correct_and_record(conn, raw_text):
    """识别结果出来后调用：按这台设备当前配置的替换表改正，并记一条最近识别记录。

    raw_text 可能是纯文本，也可能是 FunASR 返回的 dict（文字在 content 里），返回同样类型。
    """
    content = raw_text.get("content", "") if isinstance(raw_text, dict) else raw_text
    if not isinstance(content, str) or not content:
        return raw_text
    try:
        fixed = apply_corrections(content, corrections_of(getattr(conn, "config", None)))
    except Exception as e:  # 纠错出错不能连累整句识别
        logger.bind(tag=TAG).warning(f"识别后纠错失败，使用原文: {e}")
        fixed = content
    record(getattr(conn, "device_id", ""), content, fixed)
    if isinstance(raw_text, dict):
        return {**raw_text, "content": fixed}
    return fixed

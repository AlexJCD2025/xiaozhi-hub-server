"""管理后台各页面的读取与保存逻辑（大模型、语音识别、语音合成、意图识别）。

和 config_store 的通用条目编辑不同，这里按页面组织：只暴露后台要管理的内容，
并对取值做校验（例如思考力度只能是 low / medium / high），错误信息直接给用户看。
所有函数只读写配置文件，不碰运行中的服务；热加载由 AdminHandler 负责。
"""

import re
from pathlib import Path

from ruamel.yaml.comments import CommentedMap, CommentedSeq

from config.config_loader import get_project_dir
from core.admin import catalog, config_store

CLAUDE_CLI_ENTRY = "ClaudeCLI"
CLAUDE_EFFORT_VALUES = {"", "low", "medium", "high", "xhigh", "max"}
CLAUDE_PERMISSION_VALUES = {"default", "acceptEdits", "plan", "auto"}
TOOL_NAME = re.compile(r"^[A-Za-z][\w:.*()\-]*$")
URL = re.compile(r"^https?://\S+$")


def _selected(merged: dict, section: str):
    return (merged.get("selected_module") or {}).get(section)


def _entry(merged: dict, section: str, name: str) -> dict:
    entry = (merged.get(section) or {}).get(name)
    return entry if isinstance(entry, dict) else {}


def _text(value) -> str:
    return "" if config_store.is_placeholder(value) else str(value).strip()


# ---------- 大模型 ----------

def _url_field(entry: dict) -> str:
    """上游有的条目用 url，有的用 base_url（openai 类型两个都认，base_url 优先）。"""
    return "url" if "url" in entry and "base_url" not in entry else "base_url"


def describe_vendor(vendor: dict, merged: dict) -> dict:
    entry = _entry(merged, "LLM", vendor["entry"])
    key = entry.get("api_key")
    base_url = _text(entry.get(_url_field(entry))) or vendor["base_url"]
    model = _text(entry.get("model_name")) or (vendor["models"][0] if vendor["models"] else "")
    key_set = not config_store.is_placeholder(key)
    exists = bool(entry) and entry.get("type") == "openai"
    return {
        "id": vendor["id"],
        "label": vendor["label"],
        "entry": vendor["entry"],
        "base_url": base_url,
        "model": model,
        "models": vendor["models"],
        "hint": vendor.get("hint", ""),
        "key_set": key_set,
        "key_hint": config_store.mask_key(key),
        # 自定义服务（本机代理、LM Studio）可能不需要 Key
        "configured": exists and bool(base_url and model) and (key_set or vendor["id"] == "custom"),
        "conflict": bool(entry) and entry.get("type") != "openai",
    }


def llm_kind(merged: dict, name: str = None) -> str:
    name = name or _selected(merged, "LLM")
    if config_store.entry_type(merged, "LLM", name) == "claude_cli":
        return "cli"
    if name in catalog.VENDOR_BY_ENTRY and config_store.entry_type(merged, "LLM", name) == "openai":
        return "api"
    return "other"


def claude_level(entry: dict) -> str:
    """从配置反推权限级别；工具组合和三档都对不上时返回 custom（手动改过配置）。"""
    mode = entry.get("permission_mode") or "default"
    tools = [str(t) for t in entry.get("allowed_tools") or []]
    core = sorted(t for t in tools if t not in catalog.CLAUDE_WEB_TOOLS)
    for level in catalog.CLAUDE_LEVELS:
        if level["mode"] == mode and core == sorted(level["tools"]):
            return level["value"]
    return "custom"


def claude_web(entry: dict) -> bool:
    tools = entry.get("allowed_tools") or []
    return all(t in tools for t in catalog.CLAUDE_WEB_TOOLS)


def describe_claude_cli(merged: dict) -> dict:
    entry = _entry(merged, "LLM", CLAUDE_CLI_ENTRY)
    values = {}
    for name, value in entry.items():
        if name == "type":
            continue
        kind = config_store.field_kind(name, value)
        if kind in ("text", "number", "bool"):
            values[name] = "" if kind == "text" and config_store.is_placeholder(value) else value
        elif kind == "list":
            values[name] = [str(v) for v in value or []]
    return {
        "exists": entry.get("type") == "claude_cli",
        "values": values,
        "level": claude_level(entry),
        "web": claude_web(entry),
        "options": {
            "model": catalog.CLAUDE_MODELS,
            "effort": catalog.CLAUDE_EFFORTS,
            "level": [{k: lv[k] for k in ("value", "label", "desc")} for lv in catalog.CLAUDE_LEVELS],
            "tools": catalog.CLAUDE_TOOLS,
        },
    }


def describe_llm(merged: dict) -> dict:
    selected = _selected(merged, "LLM")
    kind = llm_kind(merged, selected)
    vendor = catalog.VENDOR_BY_ENTRY.get(selected) if kind == "api" else None
    return {
        "selected": selected,
        "kind": kind,
        "vendor": vendor["id"] if vendor else None,
        "claude": describe_claude_cli(merged),
        "vendors": [describe_vendor(v, merged) for v in catalog.LLM_VENDORS],
    }


def _check_positive_number(name: str, raw) -> None:
    try:
        if float(str(raw).strip()) <= 0:
            raise ValueError
    except ValueError:
        raise ValueError(f"{name} 需要是大于 0 的数字")


def save_claude_cli(payload: dict, merged: dict, path: Path = None) -> list:
    """校验后写入 ClaudeCLI 条目，返回实际修改的字段。"""
    entry = _entry(merged, "LLM", CLAUDE_CLI_ENTRY)
    if entry.get("type") != "claude_cli":
        raise KeyError(CLAUDE_CLI_ENTRY)
    payload = dict(payload or {})
    # 权限级别和联网开关换算成 permission_mode + allowed_tools
    if "level" in payload or "web" in payload:
        level_value = payload.pop("level", None) or claude_level(entry)
        web = bool(payload.pop("web", claude_web(entry)))
        if level_value == "custom":
            tools = [t for t in entry.get("allowed_tools") or [] if t not in catalog.CLAUDE_WEB_TOOLS]
            mode = entry.get("permission_mode") or "default"
        else:
            level = catalog.CLAUDE_LEVEL_BY_VALUE.get(level_value)
            if level is None:
                raise ValueError("权限级别只能是 只读、可编辑、Auto")
            tools, mode = list(level["tools"]), level["mode"]
        payload["allowed_tools"] = tools + (catalog.CLAUDE_WEB_TOOLS if web else [])
        payload["permission_mode"] = mode
    if "model" in payload:
        model = str(payload["model"] or "").strip()
        if model and not re.fullmatch(r"[\w.\-\[\]]+", model):
            raise ValueError("模型名称只能包含字母、数字、点和横线，例如 haiku 或 claude-sonnet-5-5")
    if "effort" in payload and str(payload["effort"] or "").strip() not in CLAUDE_EFFORT_VALUES:
        raise ValueError("思考力度只能是 低、中、高、超高、最高")
    if "permission_mode" in payload and str(payload["permission_mode"] or "").strip() not in CLAUDE_PERMISSION_VALUES:
        raise ValueError("权限模式不支持这个值")
    if "workdir" in payload and not str(payload["workdir"] or "").strip():
        raise ValueError("工作目录不能为空")
    if "allowed_tools" in payload:
        tools = payload["allowed_tools"]
        items = tools if isinstance(tools, list) else re.split(r"[,，\s]+", str(tools))
        items = [str(t).strip() for t in items if str(t).strip()]
        bad = [t for t in items if not TOOL_NAME.match(t)]
        if bad:
            raise ValueError(f"工具名不对：{', '.join(bad)}")
        payload["allowed_tools"] = items
    for name in ("turn_timeout_seconds", "stall_timeout_seconds", "idle_timeout_seconds",
                 "resume_window_minutes", "filler_delay_seconds"):
        if name in payload:
            _check_positive_number(name, payload[name])
    return config_store.update_entry("LLM", CLAUDE_CLI_ENTRY, payload, merged, path)


def save_vendor(vendor_id: str, payload: dict, merged: dict, path: Path = None) -> list:
    """保存某个 API 厂商的接口地址、模型和 Key。条目不存在时按 OpenAI 兼容类型新建。"""
    vendor = catalog.VENDOR_BY_ID.get(vendor_id)
    if vendor is None:
        raise KeyError(vendor_id)
    entry = _entry(merged, "LLM", vendor["entry"])
    if entry and entry.get("type") != "openai":
        raise ValueError(f"配置里的 {vendor['entry']} 不是 OpenAI 兼容类型，请在配置文件里改名或删掉它")
    payload = payload or {}
    base_url = str(payload.get("base_url") or "").strip()
    model = str(payload.get("model") or "").strip()
    api_key = str(payload.get("api_key") or "").strip()
    if not URL.match(base_url):
        raise ValueError("接口地址需要以 http:// 或 https:// 开头")
    if not model:
        raise ValueError("请填写模型名称")

    url_field = _url_field(entry)
    changes = {}
    if base_url != _text(entry.get(url_field)):
        changes[url_field] = base_url
    if model != _text(entry.get("model_name")):
        changes["model_name"] = model
    if api_key:
        if any(c.isspace() for c in api_key):
            raise ValueError("API Key 里不能有空格或换行")
        if api_key != entry.get("api_key"):
            changes["api_key"] = api_key
    if not entry:
        changes = {"type": "openai", **changes}
        changes.setdefault("api_key", "")
    if not changes:
        return []
    with config_store.edit_custom(path) as data:
        target = data.setdefault("LLM", CommentedMap()).setdefault(vendor["entry"], CommentedMap())
        for field, value in changes.items():
            target[field] = value
    return [f for f in changes if f != "type"]


def vendor_test_config(vendor_id: str, payload: dict, merged: dict) -> dict:
    """测试连接用的 LLM 配置：页面上还没保存的值优先，Key 留空时用已保存的。"""
    vendor = catalog.VENDOR_BY_ID.get(vendor_id)
    if vendor is None:
        raise KeyError(vendor_id)
    entry = _entry(merged, "LLM", vendor["entry"])
    payload = payload or {}
    base_url = str(payload.get("base_url") or "").strip() or _text(entry.get(_url_field(entry))) or vendor["base_url"]
    model = str(payload.get("model") or "").strip() or _text(entry.get("model_name"))
    api_key = str(payload.get("api_key") or "").strip() or _text(entry.get("api_key"))
    if not URL.match(base_url):
        raise ValueError("接口地址需要以 http:// 或 https:// 开头")
    if not model:
        raise ValueError("请填写模型名称")
    if not api_key and vendor_id != "custom":
        raise ValueError("请先填写 API Key")
    config = {k: v for k, v in entry.items() if k not in ("url", "base_url")}
    config.update({"type": "openai", "base_url": base_url, "model_name": model,
                   "api_key": api_key or "not-needed", "max_tokens": 20, "timeout": 20})
    return config


def desired_intent(merged: dict, llm_name: str, path: Path = None):
    """切换大模型后意图识别应该是什么：Claude CLI 只能用 nointent；
    API 模型恢复用户上次在 API 模式下的选择，没有选过时 Claude 用 nointent、其他用 function_call。"""
    if config_store.entry_type(merged, "LLM", llm_name) == "claude_cli":
        return "nointent"
    pref = (config_store.load_custom(path).get("admin") or {}).get("api_intent")
    if pref in catalog.INTENT_MODES:
        return pref
    return "nointent" if llm_name == catalog.VENDOR_BY_ID["claude"]["entry"] else "function_call"


# ---------- 语音识别 ----------

def asr_model_check(asr_entry: dict):
    """本地 ASR 的模型文件检查；不是本地模型时返回 None。"""
    model_files = {"sherpa_onnx_local": ("", ["model.int8.onnx", "tokens.txt"])}.get(asr_entry.get("type"))
    if not model_files:
        return None
    model_dir = Path(get_project_dir()) / (asr_entry.get("model_dir") or model_files[0])
    model = model_dir / model_files[1][0]
    size = model.stat().st_size if model.exists() else 0
    absent = [f for f in model_files[1] if not (model_dir / f).exists()]
    ok = size > 100 * 1024 * 1024 and not absent
    if size and not absent:
        detail = f"{model.name} {size // (1024 * 1024)}MB"
    elif absent:
        detail = f"缺少 {model_dir / absent[0]}"
    else:
        detail = f"{model} 文件不完整"
    return {"ok": ok, "detail": detail}


ASR_LABELS = {
    "sherpa_onnx_local": "本地识别 · SenseVoice（sherpa-onnx），不需要联网",
}


def describe_asr(merged: dict) -> dict:
    name = _selected(merged, "ASR")
    entry = _entry(merged, "ASR", name)
    info = [{"label": "模型目录", "value": str(entry.get("model_dir") or "")}] if entry.get("model_dir") else []
    if entry.get("model_type"):
        info.append({"label": "模型类型", "value": str(entry["model_type"])})
    return {
        "selected": name,
        "label": ASR_LABELS.get(entry.get("type"), "云端或其他识别服务"),
        "info": info,
        "model": asr_model_check(entry),
    }


# ---------- 语音合成 ----------

def describe_tts(merged: dict) -> dict:
    name = _selected(merged, "TTS")
    entry = _entry(merged, "TTS", name)
    return {
        "selected": name,
        "type": entry.get("type", name),
        "voice": str(entry.get("voice") or ""),
        "voices": catalog.EDGE_VOICES,
    }


def save_tts_voice(voice: str, merged: dict, path: Path = None) -> list:
    name = _selected(merged, "TTS")
    if _entry(merged, "TTS", name).get("type") != "edge":
        raise ValueError("当前语音合成不是 EdgeTTS，后台只支持设置 EdgeTTS 的音色")
    if voice not in catalog.EDGE_VOICE_VALUES:
        raise ValueError("不支持这个音色")
    return config_store.update_entry("TTS", name, {"voice": voice}, merged, path)


# ---------- 意图识别 ----------

def plugin_configured(merged: dict, config_key: str) -> bool:
    """插件需要的 Key 是否已填写（上游默认值是"你的…"或"mk-xxx"这类占位文本）。"""
    key = ((merged.get("plugins") or {}).get(config_key) or {}).get("api_key")
    return not config_store.is_placeholder(key) and not str(key).strip().lower().endswith("-xxx")


def describe_intent(merged: dict) -> dict:
    name = _selected(merged, "Intent")
    mode = config_store.entry_type(merged, "Intent", name)
    functions = [str(f) for f in _entry(merged, "Intent", "function_call").get("functions") or []]
    return {
        "selected": name,
        "mode": mode if mode in catalog.INTENT_MODES else "other",
        "locked": llm_kind(merged) == "cli",
        "plugins": [{**p, "enabled": p["name"] in functions,
                     "needs_config": bool(p.get("config")) and not plugin_configured(merged, p["config"])}
                    for p in catalog.PLUGINS],
        "always_on": catalog.ALWAYS_ON_PLUGINS,
        "extra": [f for f in functions if f not in catalog.PLUGIN_NAMES],
    }


def intent_entry_name(merged: dict, mode: str) -> str:
    """意图识别模式对应的条目名（上游里条目名和类型同名，用户改过名时按类型找）。"""
    intents = merged.get("Intent") or {}
    if isinstance(intents.get(mode), dict) and intents[mode].get("type", mode) == mode:
        return mode
    for name, entry in intents.items():
        if isinstance(entry, dict) and entry.get("type") == mode:
            return name
    raise KeyError(mode)


def save_intent_plugins(functions: list, merged: dict, path: Path = None) -> list:
    """保存 function_call 的插件列表。后台不认识的插件（用户手动加的）原样保留。"""
    if not isinstance(functions, list):
        raise ValueError("插件列表格式不对")
    unknown = [f for f in functions if f not in catalog.PLUGIN_NAMES]
    if unknown:
        raise ValueError(f"不认识的插件：{', '.join(map(str, unknown))}")
    name = intent_entry_name(merged, "function_call")
    current = [str(f) for f in _entry(merged, "Intent", name).get("functions") or []]
    wanted = [p["name"] for p in catalog.PLUGINS if p["name"] in functions]
    wanted += [f for f in current if f not in catalog.PLUGIN_NAMES]
    if wanted == current:
        return []
    with config_store.edit_custom(path) as data:
        target = data.setdefault("Intent", CommentedMap()).setdefault(name, CommentedMap())
        target["functions"] = CommentedSeq(wanted)
    return ["functions"]


def remember_api_intent(mode: str, path: Path = None) -> None:
    """记住 API 模式下用户选的意图识别，切回 API 模型时恢复。"""
    if (config_store.load_custom(path).get("admin") or {}).get("api_intent") == mode:
        return
    with config_store.edit_custom(path) as data:
        data.setdefault("admin", CommentedMap())["api_intent"] = mode


# ---------- 呼名 ----------

ALIAS_BAD = re.compile(r"[\s,，.。!！?？、~～;；:：\"'“”‘’]")


def call_targets(merged: dict) -> list:
    """可以配称呼的模型：本机 Claude Code，以及已经填好的 API 厂商。"""
    from core.agent import call_names

    result = []
    if config_store.entry_type(merged, "LLM", CLAUDE_CLI_ENTRY) == "claude_cli":
        result.append({"entry": CLAUDE_CLI_ENTRY, "label": "Claude（本机 Claude Code）",
                       "suggest": list(call_names.DEFAULT_MODELS[CLAUDE_CLI_ENTRY])})
    for vendor in catalog.LLM_VENDORS:
        if describe_vendor(vendor, merged)["configured"]:
            label = vendor["label"].lower()
            result.append({"entry": vendor["entry"], "label": f"{vendor['label']}（API）",
                           "suggest": [label] if not ALIAS_BAD.search(label) and len(label) >= 2 else []})
    return result


def describe_call_names(merged: dict) -> dict:
    from core.agent import call_names, llm_router

    cfg = merged.get("call_names") or {}
    raw = cfg.get("models") or call_names.DEFAULT_MODELS
    targets = {t["entry"]: t for t in call_targets(merged)}
    models = []
    for key, value in raw.items():
        entry = llm_router.entry_name(str(key))
        if any(m["entry"] == entry for m in models):
            continue
        t = targets.get(entry)
        models.append({"entry": entry, "label": t["label"] if t else entry, "aliases": call_names._aliases(value),
                       "ready": t is not None, "suggest": t["suggest"] if t else []})
    return {
        "enabled": bool(cfg.get("enabled", True)),
        "reply": str(cfg.get("reply") or call_names.DEFAULT_REPLY),
        "models": models,
        "targets": list(targets.values()),
        "default": _selected(merged, "LLM"),
        "prefixes": ["嗨", "嘿", "hi", "hey"],
    }


def _clean_aliases(raw, label: str, taken: dict) -> list:
    from core.agent import call_names

    if not isinstance(raw, list):
        raise ValueError("称呼格式不对")
    aliases = []
    for item in raw:
        alias = str(item or "").strip().lower()
        if not alias or alias in aliases:
            continue
        if ALIAS_BAD.search(alias):
            raise ValueError(f"称呼“{alias}”里不能有空格或标点")
        if not 2 <= len(alias) <= 20:
            raise ValueError(f"称呼“{alias}”要在 2 到 20 个字之间")
        if alias in call_names._PREFIXES:
            raise ValueError(f"“{alias}”是开头的招呼语，不能当称呼")
        if alias in taken:
            raise ValueError(f"称呼“{alias}”同时给了 {taken[alias]} 和 {label}，只能给一个模型")
        taken[alias] = label
        aliases.append(alias)
    if not aliases:
        raise ValueError(f"{label} 至少要有一个称呼，不用的话请删掉这个模型")
    return aliases


def save_call_names(payload: dict, merged: dict, path: Path = None) -> list:
    """保存呼名设置，返回修改了的项。取值不对时抛出 ValueError。

    payload["models"] 是 [{"entry": 条目名, "aliases": [...]}]。新加的模型必须已经配置好；
    原来就有、后来没配置好的模型可以保留（叫到时设备会提示还没配置好）。
    """
    from core.agent import call_names

    current = describe_call_names(merged)
    enabled = bool(payload.get("enabled", current["enabled"]))
    reply = str(payload.get("reply", current["reply"]) or "").strip() or call_names.DEFAULT_REPLY
    if len(reply) > 40:
        raise ValueError("回复太长了，请控制在 40 个字以内")
    raw = payload.get("models")
    if raw is None:
        raw = [{"entry": m["entry"], "aliases": m["aliases"]} for m in current["models"]]
    if not isinstance(raw, list):
        raise ValueError("模型列表格式不对")
    known = {m["entry"]: m["label"] for m in current["models"]}
    known.update({t["entry"]: t["label"] for t in current["targets"]})
    allowed = {t["entry"] for t in current["targets"]} | {m["entry"] for m in current["models"]}
    models, taken = {}, {}
    for item in raw:
        entry = str((item or {}).get("entry") or "") if isinstance(item, dict) else ""
        if entry not in allowed:
            raise ValueError(f"模型“{entry}”还没配置好，请先到“大模型”页填好再来")
        if entry in models:
            raise ValueError(f"{known[entry]} 重复了")
        models[entry] = _clean_aliases(item.get("aliases"), known[entry], taken)
    if enabled and not models:
        raise ValueError("至少要给一个模型配称呼")

    old_models = {m["entry"]: m["aliases"] for m in current["models"]}
    changed = [k for k, new, old in (("enabled", enabled, current["enabled"]), ("reply", reply, current["reply"]),
                                     ("models", list(models.items()), list(old_models.items()))) if new != old]
    if not changed:
        return []
    with config_store.edit_custom(path) as data:
        cfg = data.setdefault("call_names", CommentedMap())
        cfg["enabled"] = enabled
        cfg["reply"] = reply
        cfg["models"] = CommentedMap((k, CommentedSeq(v)) for k, v in models.items())
    return changed

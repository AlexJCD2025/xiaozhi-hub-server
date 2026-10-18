"""管理后台"工作原理"页：按当前配置描述一句话从设备到播放的完整流程，并拆解发给大模型的提示词。

只读：不修改配置，也不影响运行中的服务。流程里的参数都从配置读取，切换模块后页面跟着变。
提示词按原项目的拼装方式（core/utils/prompt_manager.py、core/utils/dialogue.py）重新渲染一份预览，
模板变量用标记包起来，前端据此高亮显示"这里填进了什么"。
"""

import os
from datetime import datetime

from jinja2 import Template

from config.config_loader import get_project_dir
from core.admin import catalog, config_store, connections

# 变量标记：\x01变量名\x02值\x03，前端换成高亮
MARK_START, MARK_SEP, MARK_END = "\x01", "\x02", "\x03"

VAR_LABELS = {
    "base_prompt": "人设",
    "current_time": "当前时间",
    "today_date": "日期",
    "today_weekday": "星期",
    "lunar_date": "农历",
    "local_address": "位置",
    "weather_info": "天气",
    "emojiList": "表情列表",
    "dynamic_context": "动态上下文",
    "language": "回复语言",
}

DIRECT_ANSWER_FEWSHOT = [
    {"role": "user", "content": "给我讲个故事吧"},
    {"role": "assistant", "content": '调用 direct_answer：{"response": "好呀，你想听什么类型的呀？童话、冒险还是搞笑的？选一个我给你开讲~"}'},
    {"role": "tool", "content": "已直接回复"},
    {"role": "user", "content": "拜拜"},
    {"role": "assistant", "content": '调用 handle_exit_intent：{"say_goodbye": "再见，下次再聊~"}'},
    {"role": "tool", "content": "退出意图已处理"},
    {"role": "assistant", "content": "再见，下次再聊~"},
]


def _mark(name: str, value) -> str:
    return f"{MARK_START}{name}{MARK_SEP}{value}{MARK_END}"


def _entry(merged: dict, section: str) -> tuple:
    name = (merged.get("selected_module") or {}).get(section)
    entry = (merged.get(section) or {}).get(name) or {}
    return name, entry if isinstance(entry, dict) else {}


def _online_conn():
    """任意一台在线设备的连接（用来取它实际使用的提示词和位置）。"""
    for item in connections.snapshot():
        conn = connections.get(item["session_id"])
        if conn is not None:
            return conn
    return None


def _system_prompt_of(conn) -> str:
    dialogue = getattr(getattr(conn, "dialogue", None), "dialogue", None) or []
    for msg in dialogue:
        if getattr(msg, "role", None) == "system":
            return str(getattr(msg, "content", "") or "")
    return ""


# ---------- 流程 ----------

def _voice_name(voice: str) -> str:
    info = next((v for v in catalog.EDGE_VOICES if v["value"] == voice), None)
    return f"{info['name']}（{info['group']}）" if info else voice


def _llm_label(name: str, entry: dict) -> str:
    if entry.get("type") == "claude_cli":
        return "Claude Code CLI"
    vendor = catalog.VENDOR_BY_ENTRY.get(name)
    return vendor["label"] if vendor else name


def describe_flow(merged: dict) -> dict:
    vad_name, vad = _entry(merged, "VAD")
    asr_name, asr = _entry(merged, "ASR")
    llm_name, llm = _entry(merged, "LLM")
    tts_name, tts = _entry(merged, "TTS")
    intent_name, intent = _entry(merged, "Intent")
    intent_type = intent.get("type", intent_name)
    is_cli = llm.get("type") == "claude_cli"
    prebuffer = int(merged.get("tts_audio_prebuffer_count") or 5)
    silence = vad.get("min_silence_duration_ms", 1000)

    stages = [
        {
            "id": "listen", "title": "设备收音", "module": vad_name, "icon": "🎙",
            "summary": f"说完停顿 {silence}ms 算一句",
            "params": [
                {"label": "传输", "value": "设备把麦克风声音编码成 Opus，每 60ms 一包，经 WebSocket 发给服务端"},
                {"label": "解码", "value": "服务端解码成 16kHz 单声道"},
                {"label": "语音检测（VAD）", "value": f"{vad_name}：声音概率 ≥ {vad.get('threshold', 0.5)} 算说话，"
                                                     f"≤ {vad.get('threshold_low', 0.3)} 算安静，安静超过 {silence}ms 判定这句说完"},
                {"label": "太短不处理", "value": "不到 0.9 秒的声音直接忽略"},
                {"label": "唤醒后", "value": "唤醒后 2 秒内不收音，避免把唤醒词本身识别进去"},
            ],
        },
        {
            "id": "asr", "title": "语音识别", "module": asr_name, "icon": "📝",
            "summary": "本地识别，不联网" if asr.get("type") in ("sherpa_onnx_local", "fun_local") else "云端识别",
            "params": [
                {"label": "模型", "value": f"{asr.get('type', asr_name)} · {asr.get('model_dir') or '云端服务'}"},
                {"label": "结果", "value": "识别出的文字去掉首尾标点后，先发给设备显示（stt 消息），再进入下一步"},
                {"label": "空结果", "value": "去掉标点后没有文字（例如只有噪音）就不往下走"},
            ],
        },
        {
            "id": "intent", "title": "意图识别", "module": "不用插件" if intent_type == "nointent" else
            "用插件" if intent_type == "function_call" else intent_name, "icon": "🧭",
            "summary": "先检查退出指令和唤醒词",
            "params": [
                {"label": "第 1 步：退出指令", "value": f"整句话是 {'、'.join(merged.get('exit_commands') or [])} 时直接断开，不经过大模型"},
                {"label": "第 2 步：唤醒词", "value": "整句话是唤醒词时，播放缓存的回复，不经过大模型"},
                {"label": "第 3 步：意图", "value":
                    "不用插件：直接交给大模型" if intent_type == "nointent" else
                    "用插件：把插件当作工具清单交给大模型，由它决定要不要调用；一轮最多连续调用 5 次工具"
                    if intent_type == "function_call" else f"{intent_name}：先用一个模型判断要不要调插件"},
            ],
        },
        {
            "id": "llm", "title": "大模型", "module": _llm_label(llm_name, llm), "icon": "✦",
            "summary": f"{llm.get('model') or '默认模型'} · 思考力度 {llm.get('effort') or '默认'}" if is_cli
            else str(llm.get("model_name") or ""),
            "params": _llm_params(llm, is_cli),
        },
        {
            "id": "emotion", "title": "表情", "module": "开头的 emoji", "icon": "🙂",
            "summary": "单独发给设备换脸",
            "params": [
                {"label": "提取", "value": "大模型回复的第一段里找第一个 emoji，对应成情绪名，例如 😂 → funny；找不到时用 🙂 happy"},
                {"label": "发送", "value": "发一条 llm 消息给设备：{\"type\": \"llm\", \"text\": \"😂\", \"emotion\": \"funny\"}，设备据此换表情"},
                {"label": "每轮一次", "value": "每轮只发一次" + ("；先说了填充语时，会在正式回复开头再找一次表情并重发" if is_cli else "")},
                {"label": "朗读时", "value": "emoji 不会被念出来，切句后会全部去掉"},
            ],
        },
        {
            "id": "tts", "title": "语音合成", "module": tts_name, "icon": "🔊",
            "summary": f"音色：{_voice_name(str(tts.get('voice') or ''))}" if tts.get("type") == "edge" else tts.get("type", ""),
            "params": [
                {"label": "切句", "value": "边收大模型的回复边切句：第一句遇到逗号就切（让设备尽早开口），后面的句子遇到句号、问号、感叹号、分号才切"},
                {"label": "清理", "value": "去掉 emoji、Markdown 符号（加粗、列表、代码块、表格等）和换行"},
                {"label": "合成", "value": f"{tts_name} 逐句合成语音，转成 24kHz Opus，每 60ms 一包"},
            ],
        },
        {
            "id": "play", "title": "设备播放", "module": f"预缓冲 {prebuffer} 帧", "icon": "📢",
            "summary": f"约 {prebuffer * 60 / 1000:.1f} 秒缓冲",
            "params": [
                {"label": "预缓冲", "value": f"每句开头先一口气发 {prebuffer} 包（约 {prebuffer * 60 / 1000:.1f} 秒），之后按 60ms 一包匀速发送，避免 Wi-Fi 抖动导致断续"},
                {"label": "字幕", "value": "每句开始前发一条 sentence_start 消息，设备屏幕显示这句话"},
                {"label": "结束", "value": "等设备把缓冲里的声音播完，再发 tts stop，设备回到收音状态"},
                {"label": "打断", "value": "说话时点屏幕或说唤醒词，服务端清空待发的声音并立即发 tts stop；"
                                        f"设备里已缓冲的约 {prebuffer * 60 / 1000:.1f} 秒会播完才停"},
            ],
        },
    ]

    branches = [
        {"title": "唤醒词", "text": "说唤醒词（例如“嘿，你好呀”）时不经过大模型，直接播放缓存的回复，例如“您请说，我正听着。”"},
        {"title": "退出指令", "text": f"整句话是“{'”“'.join(merged.get('exit_commands') or [])}”时立即断开连接，没有回复"},
        {"title": "长时间不说话", "text": f"{merged.get('close_connection_no_voice_time', 120)} 秒没人说话，大模型说一句告别语后断开"},
    ]
    if is_cli:
        branches += [
            {"title": "填充语", "text": f"Claude 超过 {llm.get('filler_delay_seconds', 2)} 秒还没开口时，先说“{llm.get('first_reply_filler') or '（未设置）'}”"},
            {"title": "接续上次对话", "text": f"{llm.get('resume_window_minutes', 120)} 分钟内重新连接会自动接着聊；超过后说"
                                        f"“{'”“'.join(llm.get('resume_keywords') or [])}”才会接回旧会话"},
            {"title": "出错时", "text": "Claude 没登录、额度用完、超时等情况，会直接念一句中文说明，例如“Claude 现在用量到上限了，稍后再试吧。”"},
        ]

    messages = [
        {"type": "stt", "text": "识别出的文字，设备屏幕显示"},
        {"type": "tts · start", "text": "服务端开始说话"},
        {"type": "llm", "text": "表情（emoji 和情绪名）"},
        {"type": "tts · sentence_start", "text": "每句话的字幕，后面紧跟这句的语音包"},
        {"type": "（Opus 语音包）", "text": "每包 60ms"},
        {"type": "tts · stop", "text": "说完了，设备回到收音"},
    ]
    return {"stages": stages, "branches": branches, "messages": messages}


def _llm_params(llm: dict, is_cli: bool) -> list:
    if is_cli:
        return [
            {"label": "每轮发送", "value": "只发这一轮识别出的文字；之前的对话保存在 Claude Code 的会话里，不重复发送"},
            {"label": "提示词", "value": "Claude Code 进程启动时一次性传入（见下方“提示词拆解”），进程存活期间不变"},
            {"label": "进程", "value": f"每台设备一个 Claude Code 进程；空闲 {llm.get('idle_timeout_seconds', 300)} 秒或设备断开时关闭"},
            {"label": "工作目录", "value": str(llm.get("workdir") or "")},
            {"label": "允许的工具", "value": "、".join(llm.get("allowed_tools") or []) or "无"},
            {"label": "超时", "value": f"单轮最长 {llm.get('turn_timeout_seconds', 120)} 秒；连续 {llm.get('stall_timeout_seconds', 30)} 秒没有任何动静就中断"},
            {"label": "回复", "value": "边生成边往下传（流式），不用等整段写完"},
        ]
    return [
        {"label": "每轮发送", "value": "系统提示词 + 全部历史对话 + 这一轮的话（OpenAI 协议，流式返回）"},
        {"label": "接口", "value": str(llm.get("base_url") or llm.get("url") or "")},
        {"label": "最大输出", "value": str(llm.get("max_tokens") or "默认")},
        {"label": "关闭思考", "value": "对 DeepSeek、通义千问、智谱、Kimi、豆包、Claude 官方接口自动关闭深度思考，减少首字等待"},
    ]


# ---------- 提示词拆解 ----------

def _template_name(merged: dict) -> str:
    return str(merged.get("prompt_template") or "hub-base-prompt.txt")


def _template_text(merged: dict) -> str:
    path = os.path.join(get_project_dir(), _template_name(merged))
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def _context_values(merged: dict, conn) -> dict:
    from core.utils.current_time import get_current_date, get_current_lunar_date, get_current_weekday
    from core.utils.prompt_manager import EMOJI_List

    _, tts = _entry(merged, "TTS")
    location = weather = ""
    if conn is not None:
        try:
            from core.utils.cache.manager import CacheType, cache_manager

            location = cache_manager.get(CacheType.LOCATION, getattr(conn, "client_ip", "")) or ""
            weather = cache_manager.get(CacheType.WEATHER, location) or "" if location else ""
        except Exception:
            pass
    return {
        "current_time": datetime.now().strftime("%H:%M"),
        "today_date": get_current_date(),
        "today_weekday": get_current_weekday(),
        "lunar_date": get_current_lunar_date(),
        "local_address": location or "（设备连接时按 IP 查询城市；局域网 IP 查不到时为空）",
        "weather_info": weather or "（设备连接时按城市查询未来几天天气）",
        "emojiList": EMOJI_List,
        "dynamic_context": "",
        "language": tts.get("language") or "中文",
    }


def render_base_prompt(merged: dict, conn=None) -> str:
    """按原项目的方式渲染基础模板，变量值用标记包起来。"""
    template = _template_text(merged)
    persona = str(merged.get("prompt") or "")
    if not template:
        return _mark("base_prompt", persona)
    values = _context_values(merged, conn)
    marked = {k: _mark(k, v) for k, v in values.items()}
    return Template(template).render(base_prompt=_mark("base_prompt", persona), emoji_enabled=True,
                                     device_id="", client_ip="", **marked)


def describe_prompt(merged: dict) -> dict:
    llm_name, llm = _entry(merged, "LLM")
    _, intent = _entry(merged, "Intent")
    is_cli = llm.get("type") == "claude_cli"
    conn = _online_conn()
    base = render_base_prompt(merged, conn)

    segments = []
    if is_cli:
        segments.append({
            "id": "claude_preset", "title": "Claude Code 自带的系统提示", "source": "Claude Code", "when": "进程启动时",
            "text": "", "collapsed": True,
            "note": "Claude Code 内置的说明，介绍它自己有哪些工具、工作目录在哪、怎么回答编程问题等。"
                    "这段由 Claude Code 程序自己生成，不在我们的配置里，这里无法显示原文。我们的内容追加在它后面。",
        })
    segments.append({
        "id": "base", "title": "基础模板（含人设）", "source": "我们", "when":
            "设备连接时生成；Claude 进程启动时固定" if is_cli else "设备连接时生成；当前时间每轮更新",
        "text": base, "collapsed": False,
        "note": f"来自 {_template_name(merged)}。高亮的部分是填进去的值：人设来自配置里的 prompt，"
                "日期、农历在设备连接时生成，当前时间" +
                ("在 Claude 进程启动时取一次。" if is_cli else "每轮发送前更新。"),
    })
    if is_cli:
        from core.providers.llm.claude_cli.claude_cli import VOICE_RULES

        segments.append({
            "id": "voice_rules", "title": "语音规则", "source": "我们", "when": "进程启动时",
            "text": VOICE_RULES.strip(), "collapsed": False,
            "note": "我们为 Claude Code 加的规则：开头一个 emoji、简短口语、不用 Markdown。写在 claude_cli.py 的 VOICE_RULES。",
        })
        segments.append({
            "id": "turn", "title": "每轮发送的内容", "source": "原项目", "when": "每轮",
            "text": "（这一轮识别出的文字，例如：明天上海会下雨吗）", "collapsed": False,
            "note": "只发这一轮的话，之前的对话保存在 Claude Code 会话里。第一次识别到说话人时，会发 "
                    "{\"speaker\": \"名字\", \"content\": \"文字\"} 这样的 JSON（声纹识别目前没开）。",
        })
    else:
        if intent.get("type") == "function_call":
            segments.append({
                "id": "fewshot", "title": "工具调用示例", "source": "原项目", "when": "每轮",
                "text": "\n".join(f"[{m['role']}] {m['content']}" for m in DIRECT_ANSWER_FEWSHOT), "collapsed": True,
                "note": "插在系统提示词后面的几条示范对话，教模型什么时候直接回答、什么时候调用工具。不会显示给用户。",
            })
            plugins = [p["label"] for p in catalog.PLUGINS if p["name"] in (intent.get("functions") or [])]
            segments.append({
                "id": "tools", "title": "工具清单", "source": "原项目", "when": "每轮",
                "text": "、".join(["直接回答（direct_answer）", *[p["label"] for p in catalog.ALWAYS_ON_PLUGINS], *plugins]),
                "collapsed": False,
                "note": "随请求一起发给模型的工具（OpenAI 协议的 tools 参数），每个工具带名字、用途和参数说明。",
            })
        segments.append({
            "id": "history", "title": "历史对话 + 这一轮的话", "source": "原项目", "when": "每轮",
            "text": "[user] 上一轮说的话\n[assistant] 上一轮的回复\n……\n[user] 这一轮识别出的文字", "collapsed": False,
            "note": "每轮都把这次连接以来的全部对话一起发送，没有截断；设备断开后清空（没有开启记忆）。",
        })

    warnings = []
    if is_cli:
        warnings.append("提示词在 Claude Code 进程启动时一次性传入，进程存活期间（空闲 5 分钟才关闭）里面的日期、时间不会更新。")

    actual = _system_prompt_of(conn) if conn is not None else ""
    return {
        "llm": _llm_label(llm_name, llm),
        "segments": segments,
        "warnings": warnings,
        "actual": {"device_id": getattr(conn, "device_id", None), "text": actual} if actual else None,
        "marks": {"start": MARK_START, "sep": MARK_SEP, "end": MARK_END},
        "var_labels": VAR_LABELS,
    }


def describe(merged: dict) -> dict:
    return {"flow": describe_flow(merged), "prompt": describe_prompt(merged)}

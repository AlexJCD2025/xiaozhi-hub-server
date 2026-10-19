"""管理后台"工作原理"页：按当前配置描述一句话从设备到播放的完整流程，并拆解发给大模型的提示词。

只读：不修改配置，也不影响运行中的服务。流程里的参数都从配置读取，切换模块后页面跟着变。
提示词按原项目的拼装方式（core/utils/prompt_manager.py、core/utils/dialogue.py）重新渲染一份预览，
模板变量用标记包起来，前端据此高亮显示"这里填进了什么"。
"""

import os
from datetime import datetime

from jinja2 import Template

from config.config_loader import get_project_dir
from core.admin import catalog, config_store, connections, pages

# 变量标记：\x01变量名\x02值\x03，前端换成高亮
MARK_START, MARK_SEP, MARK_END = "\x01", "\x02", "\x03"

VAR_LABELS = {
    "base_prompt": "人设",
    "current_time": "当前时间",
    "today_date": "日期",
    "today_weekday": "星期",
    "lunar_date": "农历",
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
    """任意一台在线设备的连接（用来取它实际使用的提示词）。"""
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
    calls = pages.describe_call_names(merged)
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
            "summary": "本地识别，不联网" if asr.get("type") == "sherpa_onnx_local" else "云端识别",
            "params": [
                {"label": "模型", "value": f"{asr.get('type', asr_name)} · {asr.get('model_dir') or '云端服务'}"},
                {"label": "结果", "value": "识别出的文字去掉首尾标点后，先发给设备显示（stt 消息），再进入下一步"},
                {"label": "空结果", "value": "去掉标点后没有文字（例如只有噪音）就不往下走"},
            ],
        },
        {
            "id": "intent", "title": "意图识别", "module": "不用插件" if intent_type == "nointent" else
            "用插件" if intent_type == "function_call" else intent_name, "icon": "🧭",
            "summary": "先处理呼名、退出指令和唤醒词" if calls["enabled"] else "先检查退出指令和唤醒词",
            "params": ([
                {"label": "呼名", "value": f"句首是“嗨 + 称呼”时去掉称呼，把这台设备切到对应的模型并记住；"
                                         f"只说称呼时直接回“{calls['reply']}”，不经过大模型"},
            ] if calls["enabled"] else []) + [
                {"label": "退出指令", "value": f"整句话是 {'、'.join(merged.get('exit_commands') or [])} 时直接断开，不经过大模型"},
                {"label": "唤醒词", "value": "整句话是唤醒词时，播放缓存的回复，不经过大模型"},
                {"label": "意图", "value":
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

    branches = ([
        {"title": "只说称呼", "text": f"只说“嗨 + 称呼”时，服务端切换模型后直接回“{calls['reply']}”，不经过大模型，也不启动 Claude Code 进程"},
    ] if calls["enabled"] else []) + [
        {"title": "唤醒词", "text": "说唤醒词（例如“嘿，你好呀”）时不经过大模型，直接播放缓存的回复，例如“您请说，我正听着。”"},
        {"title": "退出指令", "text": f"整句话是“{'”“'.join(merged.get('exit_commands') or [])}”时立即断开连接，没有回复"},
        {"title": "长时间不说话", "text": "；".join(_idle_close(merged))},
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
    return {"stages": stages, "branches": branches, "messages": messages,
            "timeline": describe_timeline(merged, calls)}


def _duration(seconds) -> str:
    try:
        seconds = float(seconds)
    except (TypeError, ValueError):
        return str(seconds)
    if seconds >= 60 and seconds % 60 == 0:
        minutes = int(seconds // 60)
        return f"{minutes // 60} 小时" if minutes % 60 == 0 else f"{minutes} 分钟"
    return f"{int(seconds)} 秒"


def _idle_close(merged: dict) -> list:
    """两道"没人说话就断开"：第一道说告别语，第二道兜底直接断开。两道看的是同一个"最后活动时间"。"""
    first = int(merged.get("close_connection_no_voice_time", 120) or 120)
    end_prompt = merged.get("end_prompt") or {}
    if end_prompt.get("enable", True) is False:
        action = "直接断开"
    elif end_prompt.get("reply"):
        action = f"说一句“{end_prompt['reply']}”后断开"
    else:
        action = "让大模型说一句告别语后断开"
    return [
        f"设备在收音、但 {_duration(first)}没人说话：{action}",
        f"{_duration(first + 60)}没有任何动静（设备没发声音和文字、服务端也没在说话）：兜底直接断开，没有告别语",
    ]


def describe_timeline(merged: dict, calls: dict) -> list:
    """从唤醒到断开，一次对话的完整经过。每一步标出是设备还是服务端在做，时长等参数从配置读取。"""
    llms = merged.get("LLM") or {}
    default_name = (merged.get("selected_module") or {}).get("LLM")
    default_label = _llm_label(default_name, llms.get(default_name) or {})
    called = [m["entry"] for m in calls["models"]] if calls["enabled"] else []
    cli_names = [n for n in [default_name, *called]
                 if isinstance(llms.get(n), dict) and llms[n].get("type") == "claude_cli"]
    has_api = any(isinstance(llms.get(n), dict) and llms[n].get("type") != "claude_cli" for n in [default_name, *called])
    cli = llms[cli_names[0]] if cli_names else {}
    window = cli.get("resume_window_minutes", 120)
    idle = cli.get("idle_timeout_seconds", 300)
    keywords = "”“".join(cli.get("resume_keywords") or ["接着上次聊", "继续上次的话题", "继续刚才的话题", "接着刚才聊"])
    reply = calls["reply"]

    wake = [
        "说“Hi, StackChan”：唤醒词由设备自己识别（固件里），不经过服务端",
        "设备连上本地服务，并把唤醒词当作一句话发过来（StackChan 固定发“嘿，你好呀”）；"
        "服务端认出是唤醒词，播放缓存的问候，不经过大模型",
        "点一下屏幕也能唤醒：设备直接开始收音，没有问候",
    ]
    wake.append(f"服务端把这台设备切到它上次用的模型（第一次连上的设备用默认模型 {default_label}）；这时还不启动 Claude Code 进程"
                if calls["enabled"] else f"所有话都交给默认模型 {default_label}；这时还不启动 Claude Code 进程")
    steps = [{"title": "唤醒", "who": "设备 → 服务端", "items": wake}]

    if calls["enabled"]:
        names = "；".join(f"{m['label']}：{'、'.join(m['aliases'])}" for m in calls["models"])
        steps.append({"title": "叫模型（可选）", "who": "服务端", "items": [
            f"只说“嗨 + 称呼”：切到这个模型并记住，回一声“{reply}”，不启动 Claude Code 进程",
            "“嗨 + 称呼 + 内容”：去掉称呼，把内容交给这个模型",
            "不说称呼：交给这台设备上次用的模型",
            f"现在的称呼 —— {names}" if names else "还没有配置称呼",
        ]})

    first = []
    if cli_names:
        first += [
            "交给 Claude 时，这一刻才启动 Claude Code 进程，同时决定接上旧会话还是新开",
            f"说的是“{keywords}”：接回上次的会话，不管隔了多久",
            f"普通的话：距上次对话不超过 {_duration(float(window) * 60)} 就自动接上，否则新开会话",
            "接续指令后面还带着内容时（如“接着上次聊那个 bug”），接上后把整句交给 Claude",
        ]
    if has_api:
        first.append("交给 API 模型时：对话只在这次连接内有效，断开后清空，接续指令会当普通的话交给模型")
    steps.append({"title": "第一句话", "who": "服务端", "items": first or ["交给当前模型"]})

    during = ["后面每句话直接交给当前模型" + ("；Claude Code 进程已在运行，不再判断接续" if cli_names else "")]
    if cli_names:
        progress = cli.get("progress_interval_seconds", 30)
        during.append(f"Claude 干长任务时设备陪着等，最长 {_duration(cli.get('turn_timeout_seconds', 600))}，期间连接不会断开"
                      + (f"；每 {_duration(progress)}没开口就说一句“{cli.get('progress_reply') or '还在处理，请稍等'}”" if progress else ""))
    if calls["enabled"]:
        during.append("说“嗨 + 别的称呼”随时换模型；每个模型只记得跟自己的对话，切回来接着原来的聊")
    during.append("打断：设备说话时点一下屏幕上的脸，或说唤醒词")
    steps.append({"title": "对话中", "who": "服务端", "items": during})

    end = _idle_close(merged)
    if cli_names:
        end.append(f"Claude Code 进程空闲 {_duration(idle)}或设备断开时关闭；切到别的模型后不马上关，等空闲超时再关，方便切回来")
        end.append("进程因空闲关闭后再说话：重新启动进程，按“第一句话”的规则判断接续")
    if calls["enabled"]:
        end.append("下次唤醒：还是上次用的模型" + ("，接续规则同上" if cli_names else ""))
    steps.append({"title": "结束", "who": "服务端", "items": end})
    return steps


def _llm_params(llm: dict, is_cli: bool) -> list:
    if is_cli:
        return [
            {"label": "每轮发送", "value": "只发这一轮识别出的文字；之前的对话保存在 Claude Code 的会话里，不重复发送"},
            {"label": "提示词", "value": "Claude Code 进程启动时一次性传入（见下方“提示词拆解”），进程存活期间不变"},
            {"label": "进程", "value": f"每台设备一个 Claude Code 进程；空闲 {llm.get('idle_timeout_seconds', 300)} 秒或设备断开时关闭"},
            {"label": "工作目录", "value": str(llm.get("workdir") or "")},
            {"label": "允许的工具", "value": "、".join(llm.get("allowed_tools") or []) or "无"},
            {"label": "超时", "value": f"单轮最长 {llm.get('turn_timeout_seconds', 600)} 秒；连续 {llm.get('stall_timeout_seconds', 30)} 秒没有任何动静就中断（执行工具时不算）"},
            {"label": "长任务", "value": "干活期间连接不会因为没人说话断开" + (
                f"；每 {llm.get('progress_interval_seconds', 30)} 秒没开口就说一句“{llm.get('progress_reply') or '还在处理，请稍等'}”"
                if llm.get("progress_interval_seconds", 30) else "")},
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


def _context_values(merged: dict) -> dict:
    from core.utils.current_time import get_current_date, get_current_lunar_date, get_current_weekday
    from core.utils.prompt_manager import EMOJI_List

    _, tts = _entry(merged, "TTS")
    return {
        "current_time": datetime.now().strftime("%H:%M"),
        "today_date": get_current_date(),
        "today_weekday": get_current_weekday(),
        "lunar_date": get_current_lunar_date(),
        "emojiList": EMOJI_List,
        "dynamic_context": "",
        "language": tts.get("language") or "中文",
    }


def render_base_prompt(merged: dict) -> str:
    """按服务端的方式渲染基础模板，变量值用标记包起来。"""
    template = _template_text(merged)
    persona = str(merged.get("prompt") or "")
    if not template:
        return _mark("base_prompt", persona)
    values = _context_values(merged)
    marked = {k: _mark(k, v) for k, v in values.items()}
    return Template(template).render(base_prompt=_mark("base_prompt", persona), emoji_enabled=True,
                                     device_id="", client_ip="", **marked)


def describe_prompt(merged: dict) -> dict:
    llm_name, llm = _entry(merged, "LLM")
    _, intent = _entry(merged, "Intent")
    is_cli = llm.get("type") == "claude_cli"
    conn = _online_conn()
    base = render_base_prompt(merged)

    segments = []
    if is_cli:
        segments.append({
            "id": "claude_preset", "title": "Claude Code 自带的系统提示", "source": "Claude Code", "when": "进程启动时",
            "text": "", "collapsed": True,
            "note": "Claude Code 内置的说明，介绍它自己有哪些工具、工作目录在哪、怎么回答编程问题等。"
                    "这段由 Claude Code 程序自己生成，不在我们的配置里，这里无法显示原文。我们的内容追加在它后面。",
        })
    segments.append({
        "id": "base", "title": "基础模板（含人设）", "source": "服务端", "when":
            "设备连接时生成；Claude 进程启动时固定" if is_cli else "设备连接时生成；当前时间每轮更新",
        "text": base, "collapsed": False,
        "note": f"来自 {_template_name(merged)}。高亮的部分是填进去的值：人设来自配置里的 prompt，"
                "日期、农历在设备连接时生成，当前时间" +
                ("在 Claude 进程启动时取一次。" if is_cli else "每轮发送前更新。"),
    })
    if is_cli:
        from core.providers.llm.claude_cli.claude_cli import VOICE_RULES

        segments.append({
            "id": "voice_rules", "title": "语音规则", "source": "服务端", "when": "进程启动时",
            "text": VOICE_RULES.strip(), "collapsed": False,
            "note": "我们为 Claude Code 加的规则：开头一个 emoji、简短口语、不用 Markdown。写在 claude_cli.py 的 VOICE_RULES。",
        })
        segments.append({
            "id": "turn", "title": "每轮发送的内容", "source": "服务端", "when": "每轮",
            "text": "（这一轮识别出的文字，例如：帮我看看工作目录里有哪些文件）", "collapsed": False,
            "note": "只发这一轮的话，之前的对话保存在 Claude Code 会话里。第一次识别到说话人时，会发 "
                    "{\"speaker\": \"名字\", \"content\": \"文字\"} 这样的 JSON（声纹识别目前没开）。",
        })
    else:
        if intent.get("type") == "function_call":
            segments.append({
                "id": "fewshot", "title": "工具调用示例", "source": "服务端", "when": "每轮",
                "text": "\n".join(f"[{m['role']}] {m['content']}" for m in DIRECT_ANSWER_FEWSHOT), "collapsed": True,
                "note": "插在系统提示词后面的几条示范对话，教模型什么时候直接回答、什么时候调用工具。不会显示给用户。",
            })
            plugins = [p["label"] for p in catalog.PLUGINS if p["name"] in (intent.get("functions") or [])]
            segments.append({
                "id": "tools", "title": "工具清单", "source": "服务端", "when": "每轮",
                "text": "、".join(["直接回答（direct_answer）", *[p["label"] for p in catalog.ALWAYS_ON_PLUGINS], *plugins]),
                "collapsed": False,
                "note": "随请求一起发给模型的工具（OpenAI 协议的 tools 参数），每个工具带名字、用途和参数说明。",
            })
        segments.append({
            "id": "history", "title": "历史对话 + 这一轮的话", "source": "服务端", "when": "每轮",
            "text": "[user] 上一轮说的话\n[assistant] 上一轮的回复\n……\n[user] 这一轮识别出的文字", "collapsed": False,
            "note": "每轮都把这次连接以来的全部对话一起发送，没有截断；设备断开后清空（没有开启记忆）。",
        })

    warnings = []
    if is_cli:
        idle = int(llm.get("idle_timeout_seconds") or 300)
        idle_text = f"{idle // 60} 分钟" if idle % 60 == 0 else f"{idle} 秒"
        warnings.append(f"提示词在 Claude Code 进程启动时一次性传入，进程存活期间（空闲 {idle_text}才关闭）里面的日期、时间不会更新。")

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

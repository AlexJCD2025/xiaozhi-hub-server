"""呼名：句首"嗨 + 称呼"（如"嗨 Claude"）指定由哪个模型回答。

称呼在交给模型前去掉；整句话只有称呼时，回一句固定的话，不发给模型，也不启动 Claude Code 进程，
这样下一句"接着上次聊"仍然是这个模型的第一句话，照常接续。

别名来自实测（工作空间 asr-test/call-names）：语音识别把 Claude 全部识别成 "cloud"，
把"嗨"识别成 嗨/嘿/hi/hey/high/hiigh。第一期只支持 Claude，Grok、Codex 识别不稳定，暂不支持。
"""

import asyncio
import re
import time
import uuid
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler

TAG = __name__

DEFAULT_MODELS = {"claude": ["claude", "cloud", "clod", "clot", "克劳德"]}
DEFAULT_REPLY = "我在，请说"

_PREFIXES = ["hiigh", "high", "hey", "hai", "hi", "嗨", "嘿"]
_SEP = r"[\s,，.。!！?？、~～]*"


def _alias_pattern(alias: str) -> str:
    # 称呼后面不能紧跟（或隔着空格跟）英文字母：避免"cloudflare"、"Claude Code 怎么用"被当成称呼。
    # 中文句子里称呼后面通常是标点或中文；"Hi Claude what's up"这种不带逗号的英文句子会原样交给模型
    return re.escape(alias) + r"(?!\s*[a-z])"


def _aliases(value) -> list:
    """配置里的别名应是列表；写成单个字符串时当作一个别名，不能拆成单个字符。"""
    if isinstance(value, str):
        value = [value]
    return [str(a).strip().lower() for a in (value or []) if str(a).strip()]


def _build(models: dict) -> list:
    prefix = "(?:" + "|".join(_PREFIXES) + ")"
    result = []
    for model, value in models.items():
        aliases = _aliases(value)
        if aliases:
            alternatives = "|".join(_alias_pattern(a) for a in sorted(aliases, key=len, reverse=True))
            result.append((model, re.compile(r"^" + _SEP + prefix + _SEP + "(?:" + alternatives + ")" + _SEP,
                                             re.IGNORECASE)))
    return result


def match_call_name(text: str, models: dict = None):
    """句首是称呼时返回 (模型, 去掉称呼后的文字)，文字为空表示整句话只有称呼；不是称呼返回 None。

    连说两遍称呼（实测出现过 "Hi, cloud. Hi, cloud."）也一并去掉。
    """
    patterns = _build(models or DEFAULT_MODELS)
    found = None
    rest = text or ""
    while True:
        for model, pattern in patterns:
            m = pattern.match(rest)
            if m:
                found, rest = model, rest[m.end():]
                break
        else:
            break
    if found is None:
        return None
    return found, rest.strip()


async def handle_call_name(conn: "ConnectionHandler", text: str):
    """在意图识别和大模型之前处理称呼。

    返回 None 表示这句话已经处理完（只有称呼，已回复）；否则返回要交给模型的文字（没有称呼时原样返回）。
    """
    cfg = conn.config.get("call_names") or {}
    if not cfg.get("enabled", True):
        return text
    hit = match_call_name(text, cfg.get("models") or DEFAULT_MODELS)
    if hit is None:
        return text
    model, rest = hit
    if rest:
        conn.logger.bind(tag=TAG).info(f"呼名 {model}：去掉称呼后交给模型：{rest}")
        return rest

    from core.handle.intentHandler import speak_txt
    from core.handle.sendAudioHandle import send_stt_message
    from core.utils.dialogue import Message

    # 刚连上时 TTS 可能还在初始化（和唤醒词回复一样，最多等 3 秒）
    deadline = time.monotonic() + 3
    while not conn.tts and time.monotonic() < deadline:
        await asyncio.sleep(0.1)
    if not conn.tts:
        conn.logger.bind(tag=TAG).warning("呼名：TTS 尚未就绪，无法回复，按普通对话处理")
        return text

    reply = cfg.get("reply") or DEFAULT_REPLY
    conn.logger.bind(tag=TAG).info(f"呼名 {model}：只有称呼，直接回复：{reply}")
    await send_stt_message(conn, text)
    conn.client_abort = False
    conn.sentence_id = str(uuid.uuid4().hex)
    conn.dialogue.put(Message(role="user", content=text))
    speak_txt(conn, reply)
    return None

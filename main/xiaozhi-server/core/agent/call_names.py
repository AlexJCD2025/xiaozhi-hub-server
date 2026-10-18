"""呼名：句首"嗨 + 称呼"（如"嗨 Claude"）指定由哪个模型回答。

称呼对应 LLM 配置里的条目（如 ClaudeCLI、DeepSeekLLM），叫到后这台设备就切到这个模型并记住，
之后直接说话也交给它（见 llm_router）。称呼在交给模型前去掉；整句话只有称呼时，回一句固定的话，
不发给模型，也不启动 Claude Code 进程，这样下一句"接着上次聊"仍然是这个模型的第一句话，照常接续。

默认别名来自实测（工作空间 asr-test/call-names）：语音识别把 Claude 全部识别成 "cloud"，
把"嗨"识别成 嗨/嘿/hi/hey/high/hiigh。别名也可以在管理后台管理：开启"录入"后对着设备说几遍，
把语音识别实际听到的结果存成别名（Learner）。
"""

import asyncio
import re
import threading
import time
import uuid
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler

TAG = __name__

DEFAULT_MODELS = {"ClaudeCLI": ["claude", "cloud", "clod", "clot", "克劳德"]}
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


def extract_alias(text: str) -> str:
    """从一次录入的识别结果里取出称呼：去掉开头的"嗨/hi"等和标点，取到下一个停顿为止。

    例如 "嗨cloud。" → "cloud"，"Hi, cloud. Hi, cloud." → "cloud"，"克劳德" → "克劳德"。
    """
    rest = re.sub(r"^" + _SEP + "(?:" + "|".join(_PREFIXES) + ")" + _SEP, "", (text or "").strip(), count=1,
                  flags=re.IGNORECASE)
    rest = re.sub(r"^" + _SEP, "", rest)
    return re.split(_SEP.replace("*", "+"), rest, maxsplit=1)[0].strip().lower()


class Learner:
    """录入模式：管理后台开启后，接下来设备说的几句话不交给模型，只记录语音识别的结果，作为候选别名。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._state = None

    def start(self, session_id=None, count: int = 3, timeout: float = 90, target: str = None) -> None:
        """target：给哪个模型录入（只是记下来给后台页面用）。"""
        with self._lock:
            self._state = {"session_id": session_id, "count": count, "until": time.time() + timeout, "samples": [],
                           "target": target}

    def stop(self) -> None:
        with self._lock:
            if self._state:
                self._state["until"] = 0

    def status(self) -> dict:
        with self._lock:
            st = self._state
            if not st:
                return {"active": False, "samples": [], "count": 0, "session_id": None, "seconds_left": 0,
                        "target": None}
            left = max(0, int(st["until"] - time.time()))
            active = left > 0 and len(st["samples"]) < st["count"]
            return {"active": active, "samples": list(st["samples"]), "count": st["count"],
                    "session_id": st["session_id"], "seconds_left": left if active else 0,
                    "target": st.get("target")}

    def capture(self, session_id: str, text: str):
        """录入中且是目标设备时记下这句话，返回 (第几遍, 一共几遍)；否则返回 None。"""
        with self._lock:
            st = self._state
            if not st or time.time() > st["until"] or len(st["samples"]) >= st["count"]:
                return None
            if st["session_id"] and st["session_id"] != session_id:
                return None
            st["samples"].append({"text": text, "alias": extract_alias(text)})
            return len(st["samples"]), st["count"]


learner = Learner()


async def _speak(conn: "ConnectionHandler", heard: str, reply: str) -> bool:
    """不经过大模型，直接回一句话。TTS 还没就绪时返回 False。"""
    from core.handle.intentHandler import speak_txt
    from core.handle.sendAudioHandle import send_stt_message
    from core.utils.dialogue import Message

    # 刚连上时 TTS 可能还在初始化（和唤醒词回复一样，最多等 3 秒）
    deadline = time.monotonic() + 3
    while not conn.tts and time.monotonic() < deadline:
        await asyncio.sleep(0.1)
    if not conn.tts:
        return False
    await send_stt_message(conn, heard)
    conn.client_abort = False
    conn.sentence_id = str(uuid.uuid4().hex)
    conn.dialogue.put(Message(role="user", content=heard))
    speak_txt(conn, reply)
    return True


def _is_wakeup_word(conn: "ConnectionHandler", text: str) -> bool:
    from core.utils.util import remove_punctuation_and_length

    _, filtered = remove_punctuation_and_length(text)
    return filtered in (conn.config.get("wakeup_words") or [])


async def handle_call_name(conn: "ConnectionHandler", text: str):
    """在意图识别和大模型之前处理称呼（以及管理后台开启的录入）。

    返回 None 表示这句话已经处理完（录入了一遍，或只有称呼，已回复）；否则返回要交给模型的文字。
    """
    if not text.lstrip().startswith("{") and not _is_wakeup_word(conn, text):
        got = learner.capture(getattr(conn, "session_id", None), text)
        if got:
            n, total = got
            conn.logger.bind(tag=TAG).info(f"呼名录入 第 {n}/{total} 遍：{text}")
            await _speak(conn, text, f"收到第 {n} 遍" if n < total else "录好了，请到管理后台确认")
            return None

    cfg = conn.config.get("call_names") or {}
    if not cfg.get("enabled", True):
        return text
    hit = match_call_name(text, cfg.get("models") or DEFAULT_MODELS)
    if hit is None:
        return text
    from core.agent import llm_router

    model, rest = hit
    model = llm_router.entry_name(model)
    try:
        ok = await llm_router.switch_async(conn, model)
    except Exception as e:
        conn.logger.bind(tag=TAG).error(f"呼名 {model}：切换模型失败: {e}")
        ok = False
    if not ok:
        conn.logger.bind(tag=TAG).warning(f"呼名 {model}：这个模型没有配置好或加载失败，不切换")
        if await _speak(conn, text, f"{llm_router.model_label(model)} 还没配置好，请到管理后台检查"):
            return None
        return rest or text
    if rest:
        conn.logger.bind(tag=TAG).info(f"呼名 {model}：去掉称呼后交给模型：{rest}")
        return rest

    reply = cfg.get("reply") or DEFAULT_REPLY
    if not await _speak(conn, text, reply):
        conn.logger.bind(tag=TAG).warning("呼名：TTS 尚未就绪，无法回复，按普通对话处理")
        return text
    conn.logger.bind(tag=TAG).info(f"呼名 {model}：只有称呼，直接回复：{reply}")
    return None

"""待汇报箱：在外面干活的 Agent（目前只有 Grok Bot）做完后要念给用户的话。

设备被打断或等超时后，Grok Bot 在云端继续做；做完的结果先放进这里，再找设备空闲的时候念：
- 设备在线且空闲（没在说话、没在听你说、不在一轮对话里）：马上念；
- 设备不在线：留着，下次连上后等问候播完再念；
- 用户说"嗨 孙悟空"切回去、或给它派新活：先念它的待汇报。

只放内存，服务重启就丢（用户决定）；24 小时没念掉的也丢。
"""

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from config.logger import setup_logging
from core.agent.grokbot_client import _sleep

TAG = __name__
logger = setup_logging()

EXPIRE_SECONDS = 24 * 3600
MAX_PER_DEVICE = 20
DELIVER_TIMEOUT = 600  # 设备一直不空闲时最多等这么久，之后留到下次机会
AFTER_CONNECT_DELAY = 3  # 设备刚连上时先等问候播完


@dataclass
class Report:
    bot_id: str
    text: str
    at: float = field(default_factory=time.time)


_lock = threading.Lock()
_box: Dict[str, List[Report]] = {}  # 设备 -> 待汇报
_delivering = set()  # 正在等设备空闲的设备


def device_key(conn) -> str:
    return getattr(conn, "device_id", None) or conn.session_id


def _prune(items: List[Report]) -> List[Report]:
    now = time.time()
    return [r for r in items if now - r.at < EXPIRE_SECONDS][-MAX_PER_DEVICE:]


def add(key: str, bot_id: str, text: str) -> None:
    """放一条待汇报，设备在线就安排念。可以从任何线程调用。"""
    text = (text or "").strip()
    if not text:
        return
    with _lock:
        _box[key] = _prune(_box.get(key, [])) + [Report(bot_id, text)]
    logger.bind(tag=TAG).info(f"设备 {key} 新增待汇报：{text[:60]}")
    _try_deliver(key)


def pending(key: str, bot_id: str = None) -> bool:
    with _lock:
        items = _prune(_box.get(key, []))
        _box[key] = items
    return any(bot_id is None or r.bot_id == bot_id for r in items)


def take(key: str, bot_id: str = None) -> List[Report]:
    """取走（并删除）这台设备的待汇报；指定 bot_id 时只取这个 Bot 的。"""
    with _lock:
        items = _prune(_box.get(key, []))
        picked = [r for r in items if bot_id is None or r.bot_id == bot_id]
        _box[key] = [r for r in items if r not in picked]
    return picked


def take_text(key: str, bot_id: str = None) -> str:
    return "".join(r.text for r in take(key, bot_id))


def clear() -> None:
    with _lock:
        _box.clear()
        _delivering.clear()


# ---------- 找机会念 ----------

def _find_conn(key: str):
    from core.admin import connections

    for item in connections.snapshot():
        conn = connections.get(item["session_id"])
        if conn is not None and device_key(conn) == key:
            return conn
    return None


def _try_deliver(key: str) -> None:
    conn = _find_conn(key)
    loop = getattr(conn, "loop", None) if conn is not None else None
    if conn is None or loop is None:
        return
    import asyncio

    asyncio.run_coroutine_threadsafe(deliver_when_idle(conn), loop)


def is_idle(conn) -> bool:
    """设备没在说话（含等回复）、没在听人说，TTS 也就绪了。"""
    return (not getattr(conn, "client_is_speaking", False) and not getattr(conn, "client_have_voice", False)
            and getattr(conn, "tts", None) is not None)


def still_online(conn) -> bool:
    from core.admin import connections

    return connections.get(getattr(conn, "session_id", None)) is conn


async def deliver_when_idle(conn, initial_delay: float = 0, timeout: float = DELIVER_TIMEOUT) -> bool:
    """等设备空闲后把待汇报念出来。同一台设备同时只有一个在等。返回是否念了。"""
    key = device_key(conn)
    with _lock:
        if key in _delivering:
            return False
        _delivering.add(key)
    try:
        await _sleep(initial_delay)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not still_online(conn) or not pending(key):
                return False
            if is_idle(conn):
                text = take_text(key)
                if not text:
                    return False
                speak(conn, text)
                return True
            await _sleep(0.5)
        logger.bind(tag=TAG).info(f"设备 {key} 一直不空闲，待汇报留到下次")
        return False
    finally:
        with _lock:
            _delivering.discard(key)


def speak(conn, text: str) -> None:
    """服务端主动说一句（和呼名回复"我在"走同一条路）。"""
    from core.handle.intentHandler import speak_txt

    conn.client_abort = False
    conn.sentence_id = str(uuid.uuid4().hex)
    conn.logger.bind(tag=TAG).info(f"播报待汇报：{text[:80]}")
    speak_txt(conn, text)


def on_conn_open(conn) -> None:
    """设备连上时：有待汇报就等问候播完再念。"""
    key = device_key(conn)
    loop = getattr(conn, "loop", None)
    if loop is None or not pending(key):
        return
    import asyncio

    asyncio.run_coroutine_threadsafe(deliver_when_idle(conn, initial_delay=AFTER_CONNECT_DELAY), loop)

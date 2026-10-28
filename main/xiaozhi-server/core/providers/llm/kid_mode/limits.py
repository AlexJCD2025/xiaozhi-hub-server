"""每天可用次数和安静时段。默认都关闭。时间用 Asia/Shanghai。"""

import json
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")
_LOCK = threading.Lock()
_QUIET = re.compile(r"^(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})$")


def shanghai_now(clock=None) -> datetime:
    if clock is not None:
        return clock()
    return datetime.now(SHANGHAI)


def quiet_active(spec: str, now: datetime) -> bool:
    """``21:30-07:00`` 这样的跨夜时段也算。空字符串或写错了都当作没开启。"""
    match = _QUIET.match((spec or "").strip())
    if not match:
        return False
    start_h, start_m, end_h, end_m = (int(part) for part in match.groups())
    if not (0 <= start_h <= 23 and 0 <= end_h <= 23 and 0 <= start_m <= 59 and 0 <= end_m <= 59):
        return False
    start = start_h * 60 + start_m
    end = end_h * 60 + end_m
    if start == end:
        return False
    current = now.hour * 60 + now.minute
    if start < end:
        return start <= current < end
    return current >= start or current < end


def _device_key(device: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.:-]", "_", device or "device")
    return cleaned[:80] or "device"


def allow_turn(path: Path, device: str, day: str, limit: int) -> bool:
    """limit <= 0 表示不限制。到上限返回 False，不增加次数。"""
    if limit <= 0:
        return True
    key = _device_key(device)
    with _LOCK:
        data = {}
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8")) or {}
            except (OSError, json.JSONDecodeError):
                data = {}
        if not isinstance(data, dict):
            data = {}
        bucket = data.get(day) if isinstance(data.get(day), dict) else {}
        count = int(bucket.get(key, 0) or 0)
        if count >= limit:
            data[day] = bucket
            _write(path, {day: bucket})
            return False
        bucket[key] = count + 1
        _write(path, {day: bucket})
        return True


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def quiet_reply(text: str) -> str:
    from core.providers.llm.kid_mode.safety import prefers_english

    if prefers_english(text):
        return "It's quiet time now. Let's talk tomorrow. Good night."
    return "现在是休息时间啦。明天再聊，晚安。"


def limit_reply(text: str) -> str:
    from core.providers.llm.kid_mode.safety import prefers_english

    if prefers_english(text):
        return "We've talked a lot today. Let's continue tomorrow."
    return "今天聊得够多啦，明天再继续。去休息一下吧。"

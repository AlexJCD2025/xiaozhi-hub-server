"""语音控制指令的关键字匹配。"""

import re

DEFAULT_RESUME_KEYWORDS = ["接着上次聊", "继续上次的话题", "继续刚才的话题", "接着刚才聊"]

_STRIP = re.compile(r"[\s，。！？、,.!?；;：:~～\"'“”‘’…]+")


def normalize(text: str) -> str:
    return _STRIP.sub("", text or "")


def wants_resume(text: str, keywords=None) -> bool:
    """去掉空白和标点后，只要包含任一关键字就算命中。"""
    t = normalize(text)
    return any(normalize(k) and normalize(k) in t for k in (keywords or DEFAULT_RESUME_KEYWORDS))

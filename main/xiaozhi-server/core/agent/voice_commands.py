"""语音控制指令的关键字匹配。"""

import re

DEFAULT_RESUME_KEYWORDS = ["接着上次聊", "继续上次的话题", "继续刚才的话题", "接着刚才聊"]

_STRIP = re.compile(r"[\s，。！？、,.!?；;：:~～\"'“”‘’…]+")


def normalize(text: str) -> str:
    return _STRIP.sub("", text or "")


NEGATIONS = ("不要", "不用", "别", "不想", "不必", "甭")


def wants_resume(text: str, keywords=None) -> bool:
    """去掉空白和标点后，包含任一关键字就算命中；关键字前面紧跟否定词（如"别接着上次聊"）则不算。"""
    t = normalize(text)
    for k in keywords or DEFAULT_RESUME_KEYWORDS:
        k = normalize(k)
        if not k:
            continue
        start = t.find(k)
        while start != -1:
            if not any(t[:start].endswith(n) for n in NEGATIONS):
                return True
            start = t.find(k, start + 1)
    return False

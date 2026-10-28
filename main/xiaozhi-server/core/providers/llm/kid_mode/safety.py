"""儿童模式的输入和输出检查。

命中之后只返回事先写好的短句，不复述孩子的原话，也不提供任何做法。
关键词故意写得比较具体，避免「学校作业」「杀死细菌」这类日常说法被拦住。
"""

import re
from dataclasses import dataclass
from typing import Optional, Sequence


@dataclass(frozen=True)
class SafetyHit:
    category: str
    reply_zh: str
    reply_en: str

    def reply_for(self, text: str) -> str:
        return self.reply_en if prefers_english(text) else self.reply_zh


def prefers_english(text: str) -> bool:
    latin = len(re.findall(r"[A-Za-z]", text or ""))
    cjk = len(re.findall(r"[\u4e00-\u9fff]", text or ""))
    if cjk == 0 and latin > 0:
        return True
    return latin > cjk and latin >= 8


_RULES = (
    ("self_harm", (
        r"自杀", r"自残", r"割腕", r"不想活", r"想死", r"结束(自己的)?生命", r"活着没意思",
        r"\b(kill myself|suicide|self[- ]harm|cut myself|want to die)\b",
    ), SafetyHit(
        "self_harm",
        "我很在意你。请把这件事告诉爸爸妈妈，或者告诉一个你信任的大人。我不能讨论伤害自己的做法。",
        "I care about you. Please tell a parent or another adult you trust. I can't talk about ways to get hurt.",
    )),
    ("sexual", (
        r"色情", r"裸体", r"裸聊", r"裸照", r"做爱", r"性交", r"黄片", r"自慰", r"手淫", r"约炮", r"援交",
        r"\b(porn|nude|nudes|blowjob|handjob|hentai)\b", r"\bsex(ual)?\b",
    ), SafetyHit(
        "sexual",
        "这个话题我们先不聊。长大以后可以问爸爸妈妈。我们换一个学习和生活里的问题吧。",
        "Let's not talk about that. You can ask a parent when you're older. Want to talk about school or a hobby instead?",
    )),
    ("violence", (
        r"血腥", r"分尸", r"虐杀", r"开膛", r"砍头", r"怎么杀人", r"谋杀", r"杀死(?!细菌|病毒|进程)",
        r"\b(gore|dismember|how to kill)\b",
    ), SafetyHit(
        "violence",
        "太可怕的画面我们不聊。我们说点别的吧。",
        "Let's skip the scary stuff and talk about something else.",
    )),
    ("drugs", (
        r"毒品", r"大麻", r"冰毒", r"海洛因", r"摇头丸", r"可卡因", r"吸毒",
        r"喝酒", r"啤酒", r"白酒", r"红酒", r"喝醉", r"香烟", r"抽烟", r"吸烟", r"电子烟",
        r"酒精(?!消毒)",
        r"\b(weed|cocaine|heroin|meth|get drunk|vape|cigarette)\b", r"\balcohol\b", r"\bbeer\b",
    ), SafetyHit(
        "drugs",
        "烟、酒和那些东西对身体不好，未成年人不能碰。如果有人让你试，请告诉爸爸妈妈。",
        "Smoking, alcohol, and drugs are not for kids. If someone offers them, tell a parent.",
    )),
    ("gambling", (
        r"赌博", r"赌钱", r"博彩", r"网赌", r"老虎机", r"下注",
        r"\b(gambling|casino)\b", r"\bbet money\b",
    ), SafetyHit(
        "gambling",
        "赌博会让人把钱输掉，我们不聊怎么赌。想玩的话，可以说说正当的游戏。",
        "Gambling loses money, so I won't explain how to bet. We can talk about a regular game instead.",
    )),
    ("hate", (
        r"种族清洗", r"种族灭绝", r"仇恨言论", r"支那人",
        r"\b(nigger|nigga|kike|spic|chink)\b",
    ), SafetyHit(
        "hate",
        "我们不说伤害别人的话。每个人都值得被好好对待。换个话题吧。",
        "We don't say things that hurt people. Everyone deserves to be treated kindly. Let's change the subject.",
    )),
    ("dangerous", (
        r"制作炸弹", r"做炸弹", r"炸弹怎么做", r"炸药配方", r"毒药", r"投毒", r"自制手枪", r"怎么制枪",
        r"\b(how to make a bomb|pipe bomb|make poison)\b",
    ), SafetyHit(
        "dangerous",
        "这个做法太危险了，我不能教。请不要自己尝试，告诉爸爸妈妈。",
        "That is too dangerous for me to explain. Please don't try it, and tell a parent.",
    )),
    ("personal_info", (
        r"密码", r"\bpassword\b", r"\bpasswd\b",
        r"家庭住址", r"我家地址", r"我家住", r"我住在", r"门牌",
        r"身份证", r"银行卡", r"手机号", r"电话号码是",
        r"我的学校", r"学校名字", r"哪所学校", r"哪个学校", r"学校地址",
    ), SafetyHit(
        "personal_info",
        "地址、学校和密码都不要告诉我，也不要告诉网上的人。这些只跟爸爸妈妈说。",
        "Don't tell me your address, school, or passwords, and don't tell people online. Only a parent should know those.",
    )),
    ("bypass", (
        r"忽略(之前|上面|以上)的(规则|提示|限制|指令)",
        r"你现在(没有|无)限制",
        r"\bignore (all |any )?(previous|prior) instructions\b",
        r"\bjailbreak\b",
    ), SafetyHit(
        "bypass",
        "我还是那个陪你聊天、帮你学习的朋友。我们聊点别的吧。",
        "I'm still the friend who chats and helps you learn. Let's talk about something else.",
    )),
)

_COMPILED = tuple(
    (hit, tuple(re.compile(pattern, re.I) for pattern in patterns))
    for _name, patterns, hit in _RULES
)

_CUSTOM = SafetyHit(
    "custom",
    "这个话题我们先不聊，换一个学习和生活里的问题吧。",
    "Let's not talk about that. Want to switch to something about school or a hobby?",
)


def _hit_patterns(text: str) -> Optional[SafetyHit]:
    for hit, patterns in _COMPILED:
        if any(pattern.search(text) for pattern in patterns):
            return hit
    return None


def _hit_phrases(text: str, phrases: Sequence[str]) -> Optional[SafetyHit]:
    lowered = text.lower()
    for phrase in phrases:
        if phrase.lower() in lowered:
            return _CUSTOM
    return None


def check_text(text: str, extra_phrases: Sequence[str] = ()) -> Optional[SafetyHit]:
    """输入和输出用同一套规则。没命中返回 None。"""
    raw = text or ""
    if not raw.strip():
        return None
    return _hit_patterns(raw) or _hit_phrases(raw, extra_phrases)


def moderation_category(payload: dict) -> Optional[str]:
    """把 OpenAI 兼容的审核结果映射到我们的类别。没标红返回 None。"""
    results = payload.get("results") if isinstance(payload, dict) else None
    if not results:
        return None
    first = results[0] if isinstance(results, list) else None
    if not isinstance(first, dict) or not first.get("flagged"):
        return None
    categories = first.get("categories") or {}
    if not isinstance(categories, dict):
        return "custom"

    def on(*names: str) -> bool:
        return any(categories.get(name) for name in names)

    if on("self-harm", "self-harm/intent", "self-harm/instructions"):
        return "self_harm"
    if on("sexual", "sexual/minors"):
        return "sexual"
    if on("violence", "violence/graphic"):
        return "violence"
    if on("hate", "hate/threatening", "harassment", "harassment/threatening"):
        return "hate"
    if on("illicit", "illicit/violent"):
        return "dangerous"
    return "custom"


def reply_for_category(category: str, text: str) -> str:
    for _name, _patterns, hit in _RULES:
        if hit.category == category:
            return hit.reply_for(text)
    return _CUSTOM.reply_for(text)


_MD = (
    (re.compile(r"```.*?```", re.S), " "),
    (re.compile(r"`([^`]*)`"), r"\1"),
    (re.compile(r"!\[[^\]]*\]\([^)]*\)"), " "),
    (re.compile(r"\[([^\]]+)\]\([^)]*\)"), r"\1"),
    (re.compile(r"^[#>*\-]+\s*", re.M), ""),
    (re.compile(r"[_*~]{1,3}"), ""),
    (re.compile(r"\s+"), " "),
)


_TOOL_BLOCK = re.compile(r"<tool_call>.*?</tool_call>", re.S | re.I)


def spoken_text(text: str, limit: int = 220) -> str:
    """收成适合朗读的短句。"""
    raw = text or ""
    for pattern, repl in _MD:
        raw = pattern.sub(repl, raw)
    raw = raw.strip()
    if len(raw) <= limit:
        return raw
    cut = raw[:limit]
    for mark in ("。", "！", "？", ".", "!", "?"):
        pos = cut.rfind(mark)
        if pos >= limit // 2:
            return cut[: pos + 1]
    return cut.rstrip(" ，,、") + "。"


def without_tools(text: str) -> str:
    """去掉模型假装调用工具的片段。儿童模式没有工具。"""
    cleaned = _TOOL_BLOCK.sub(" ", text or "")
    cleaned = re.sub(r"</?tool_call>", " ", cleaned, flags=re.I)
    return spoken_text(cleaned)

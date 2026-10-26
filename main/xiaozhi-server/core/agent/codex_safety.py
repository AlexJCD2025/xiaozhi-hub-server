"""Codex 计划阶段的安全判断。

计划阶段跑在只读沙箱里。只有「真的要写入 / 删除 / 付款 / 部署 / 发消息」才需要设备确认。
只读命令（rg、sed 不带 -i、cat、ls、git status 等）单独出现时不确认。

以前的误报：把命令行或命令输出里出现的词（比如 rg 的搜索词、git status 里的 deleted）
当成危险操作。这里只看命令本身是不是只读，危险意图只从 Codex 的计划文字里判断，不看命令输出。
"""

import os
import re
import shlex
from dataclasses import dataclass, field
from typing import List, Optional, Sequence

# 设备待汇报箱里用的 id。和 Grok Bot 的 bot_id 分开，避免念串。
CODEX_REPORT_ID = "codex"

READ_ONLY_BINS = {
    "rg", "grep", "egrep", "fgrep", "ag", "ack",
    "cat", "bat", "head", "tail", "less", "more", "nl", "wc", "sort", "uniq", "cut", "tr",
    "diff", "comm", "cmp", "cksum", "md5", "md5sum", "shasum", "sha256sum", "file", "stat",
    "ls", "dir", "tree", "find", "fd", "locate", "pwd", "whoami", "id", "date", "which",
    "uname", "basename", "dirname", "realpath", "readlink", "od", "hexdump", "true", "false",
    "echo", "printf", "printenv", "env", "type", "command", "du", "df", "wc",
}

SHELLS = {"bash", "zsh", "sh", "dash", "fish"}
WRAPPERS = {"command", "nice", "stdbuf", "time", "nohup"}

GIT_READ_ONLY = {
    "status", "log", "diff", "show", "blame", "rev-parse", "ls-files", "describe", "shortlog",
    "name-rev", "cat-file", "rev-list", "symbolic-ref", "check-ignore", "merge-base",
    "count-objects", "for-each-ref", "show-ref", "version", "help", "grep", "ls-remote",
    "config", "branch", "remote", "tag", "stash",
}

# 计划文字里的意图。只读命令的参数里出现这些词不算。
_TEXT_RULES = [
    ("删除", re.compile(r"(删除|删掉|移除|清空).{0,12}(文件|目录|文件夹|仓库|分支|数据|记录|日志|缓存)")),
    ("删除", re.compile(r"(打算|准备|计划|将会|将要|需要|要去).{0,8}(删除|删掉|移除|清空)")),
    ("删除", re.compile(r"(?<![不没未])要(删除|删掉|移除)")),
    ("删除", re.compile(r"\brm\s+-[rf]", re.I)),
    ("支付", re.compile(r"(付款|支付|转账|扣款|扣费)")),
    ("支付", re.compile(r"\b(pay|payment|checkout)\b", re.I)),
    ("部署", re.compile(r"(部署|发布到线上|上线)")),
    ("部署", re.compile(r"\bdeploy\b", re.I)),
    ("发送", re.compile(r"(发邮件|发送邮件|发一封邮件|发消息|发送消息|发短信)")),
    ("发送", re.compile(r"\bsend\s+(an?\s+)?(email|e-mail|message)\b", re.I)),
    ("写入", re.compile(r"(打算|准备|计划|将会|将要|需要|我要|我会).{0,12}(修改|写入|改写|覆盖|新建|创建).{0,12}(文件|代码|配置|脚本)")),
]
_NEGATION = re.compile(r"(不要|不用|不会|无需|没有|并未|别|不|未|没|无|勿)")

_SECRET = re.compile(
    r"(sk-[A-Za-z0-9_\-]{8,}|ghp_[A-Za-z0-9]{8,}|github_pat_[A-Za-z0-9_]{8,}|"
    r"(?i:(?:api[_-]?key|access[_-]?token|refresh[_-]?token|secret|password|passwd|authorization|bearer)"
    r"\s*[:=]\s*\S+|bearer\s+[A-Za-z0-9._\-]{8,}))",
)

_MISSING_SESSION = re.compile(
    r"(no such (session|thread)|thread\b.{0,40}\bnot found|session\b.{0,40}\bnot found|"
    r"failed to resume|unknown (session|thread)|could not resume)",
    re.I,
)
_AUTH = re.compile(
    r"(not logged in|login required|sign in|chatgpt|unauthorized|\b401\b|auth(entication)? (failed|required))",
    re.I,
)

YES_REPLIES = {"确认", "确认吧", "确定", "确定吧", "同意", "同意吧", "执行", "执行吧", "批准", "可以执行", "好的执行"}
NO_REPLIES = {"取消", "取消吧", "停止", "停止吧", "停下", "停下吧", "不用了", "不要了", "算了", "别做了"}
RESUME_EXACT = {"继续", "继续吧", "接着做", "接着干", "接着上次"}


@dataclass
class SafetyDecision:
    needs_confirmation: bool
    reasons: tuple = ()
    readonly_commands: tuple = ()
    write_commands: tuple = field(default_factory=tuple)

    def __post_init__(self):
        self.readonly_commands = tuple(self.readonly_commands)
        self.write_commands = tuple(self.write_commands)
        self.reasons = tuple(self.reasons)


def redact(text: str, limit: int = 300) -> str:
    """日志和播报前去掉看起来像密钥的片段。"""
    cleaned = _SECRET.sub("[已隐藏]", text or "")
    if len(cleaned) > limit:
        return cleaned[:limit] + "…"
    return cleaned


def summarize_for_speech(text: str, limit: int = 160) -> str:
    """把 Codex 的最终回复收成适合朗读的几句，去掉 Markdown。"""
    raw = text or ""
    raw = re.sub(r"```.*?```", " ", raw, flags=re.S)
    raw = re.sub(r"`([^`]*)`", r"\1", raw)
    raw = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", raw)
    raw = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", raw)
    raw = re.sub(r"^[#>*\-]+\s*", "", raw, flags=re.M)
    raw = re.sub(r"[_*~]{1,3}", "", raw)
    raw = re.sub(r"\s+", " ", raw).strip()
    raw = redact(raw, limit=2000)
    if not raw:
        return ""
    parts = [p.strip() for p in re.split(r"(?<=[。！？!?])", raw) if p.strip()]
    chosen = []
    total = 0
    for part in parts[:3]:
        if chosen and total + len(part) > limit:
            break
        chosen.append(part)
        total += len(part)
    out = "".join(chosen) if chosen else raw[:limit]
    if len(out) > limit:
        out = out[:limit].rstrip(" ，,、") + "。"
    if out and out[-1] not in "。！？!?":
        out += "。"
    return out


def _strip_quotes(segment: str) -> str:
    return re.sub(r"""'[^']*'|"[^"]*"|`[^`]*`""", " ", segment)


def has_file_redirect(segment: str) -> bool:
    """有没有把输出写进文件。`2>&1`、`>&2` 这种描述符重定向不算。"""
    stripped = _strip_quotes(segment)
    # 只剥掉「>&数字」或「>&-」。`> file`、`>> file`、`>& file` 都还留着 >。
    stripped = re.sub(r"\d*>&[-0-9]+", " ", stripped)
    return ">" in stripped


def _split_segments(script: str) -> List[str]:
    """按管道和连接符切开，引号里的不动。"""
    parts = []
    buf = []
    quote = ""
    i = 0
    while i < len(script):
        ch = script[i]
        if quote:
            buf.append(ch)
            if ch == quote and (i == 0 or script[i - 1] != "\\"):
                quote = ""
            i += 1
            continue
        if ch in "'\"":
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if script.startswith("&&", i) or script.startswith("||", i):
            parts.append("".join(buf))
            buf = []
            i += 2
            continue
        if ch in "|;":
            parts.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    parts.append("".join(buf))
    return [p.strip() for p in parts if p.strip()]


def _unwrap_wrappers(argv: Sequence[str]) -> List[str]:
    argv = list(argv)
    while argv:
        base = os.path.basename(argv[0])
        if base == "env":
            idx = 1
            while idx < len(argv) and "=" in argv[idx] and not argv[idx].startswith("-"):
                idx += 1
            argv = argv[idx:]
            continue
        if base == "stdbuf":
            idx = 1
            while idx < len(argv) and argv[idx].startswith("-"):
                idx += 2 if argv[idx] in ("-i", "-o", "-e") else 1
            argv = argv[idx:]
            continue
        if base in ("nice", "nohup", "time"):
            argv = [a for i, a in enumerate(argv[1:], start=1) if not (i == 1 and a.startswith("-"))]
            if base == "nice" and argv and argv[0].startswith("-"):
                argv = argv[1:]
            continue
        if base == "command" and len(argv) > 1:
            argv = argv[1:]
            continue
        break
    return argv


def _extract_shell_script(argv: Sequence[str]) -> Optional[str]:
    for i, arg in enumerate(argv):
        if arg in ("-c", "-lc") or arg.endswith("c") and arg.startswith("-") and "c" in arg and len(arg) <= 4:
            if i + 1 < len(argv):
                return argv[i + 1]
            return ""
    return None


def _git_readonly(args: Sequence[str]) -> bool:
    cleaned = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "-C":
            i += 2
            continue
        if arg == "-c" or arg.startswith("-c") and arg != "-c":
            i += 1 if arg.startswith("-c") and len(arg) > 2 else 2
            continue
        if arg in ("-C", "--git-dir", "--work-tree") or arg.startswith("--git-dir") or arg.startswith("--work-tree"):
            i += 1 if "=" in arg else 2
            continue
        cleaned.append(arg)
        i += 1
    positional = [a for a in cleaned if not a.startswith("-")]
    if not positional:
        return True
    sub = positional[0]
    rest = positional[1:]
    flags = [a for a in cleaned if a.startswith("-")]
    if sub not in GIT_READ_ONLY:
        return False
    if sub == "config":
        if any(a in ("--unset", "--unset-all", "--edit", "--remove-section", "--add", "--replace-all") or a.startswith("--set") for a in flags + rest):
            return False
        return True
    if sub == "branch":
        if any(a in ("-d", "-D", "-m", "-M") or a.startswith("--delete") or a.startswith("--move") for a in flags):
            return False
        return True
    if sub == "remote":
        return not rest or rest[0] not in ("add", "remove", "rm", "rename", "set-url", "set-branches", "set-head")
    if sub == "tag":
        return not any(a in ("-d", "-a", "-s", "-u") for a in flags)
    if sub == "stash":
        return not rest or rest[0] in ("list", "show")
    return True


def _sed_readonly(args: Sequence[str]) -> bool:
    for arg in args:
        if arg == "-i" or arg.startswith("-i") or arg == "--in-place" or arg.startswith("--in-place"):
            return False
    script = " ".join(args)
    if re.search(r"(^|[\s;])[wW]\s+\S", script):
        return False
    return True


def _argv_readonly(argv: Sequence[str], depth: int) -> bool:
    if depth > 4 or not argv:
        return depth <= 4 and not argv
    argv = _unwrap_wrappers(argv)
    if not argv:
        return True
    base = os.path.basename(argv[0])
    if base in SHELLS:
        script = _extract_shell_script(argv)
        if script is None:
            return False
        return command_is_readonly(script, depth + 1)
    if base == "sed":
        return _sed_readonly(argv[1:])
    if base == "git":
        return _git_readonly(argv[1:])
    if base == "jq":
        return "-i" not in argv[1:] and "--in-place" not in argv[1:]
    if base in READ_ONLY_BINS:
        return True
    return False


def command_is_readonly(command: str, depth: int = 0) -> bool:
    """整条命令（含管道）是不是只读。解析不了就当成要确认。"""
    text = (command or "").strip()
    if not text or depth > 4:
        return not text and depth <= 4
    try:
        for segment in _split_segments(text):
            if has_file_redirect(segment):
                return False
            try:
                argv = shlex.split(segment, posix=True)
            except ValueError:
                return False
            if not _argv_readonly(argv, depth):
                return False
        return True
    except Exception:
        return False


def command_reasons(command: str) -> List[str]:
    """只读命令返回空列表。写命令给出要告诉用户的原因。"""
    if command_is_readonly(command):
        return []
    low = command or ""
    reasons = []
    if re.search(r"(^|[^\w])(rm|rmdir|unlink)(\s|$)|git\s+clean|git\s+reset\s+--hard", low, re.I):
        reasons.append("删除")
    if re.search(r"\bdeploy\b|npm\s+publish|git\s+push", low, re.I):
        reasons.append("部署")
    if re.search(r"\b(mail|sendmail)\b|git\s+send-email", low, re.I):
        reasons.append("发送")
    if not reasons:
        reasons.append("写入")
    return reasons


def _negated(text: str, start: int) -> bool:
    window = text[max(0, start - 6):start]
    return bool(_NEGATION.search(window))


def text_risks(text: str) -> List[str]:
    """从计划文字里找危险意图。否定（不要删除、没有要删除的文件）不算。"""
    found = []
    body = text or ""
    for label, pattern in _TEXT_RULES:
        for match in pattern.finditer(body):
            if _negated(body, match.start()):
                continue
            if label not in found:
                found.append(label)
            break
    return found


def assess_plan(commands: Sequence[str], agent_text: str = "", file_changes: int = 0) -> SafetyDecision:
    """综合命令和计划文字。只读命令不会单独触发确认，也不扫描命令输出。"""
    reasons = []
    readonly = []
    writes = []
    for command in commands:
        if command_is_readonly(command):
            readonly.append(command)
            continue
        writes.append(command)
        for reason in command_reasons(command):
            if reason not in reasons:
                reasons.append(reason)
    if file_changes and "写入" not in reasons:
        reasons.append("写入")
    for reason in text_risks(agent_text):
        if reason not in reasons:
            reasons.append(reason)
    return SafetyDecision(
        needs_confirmation=bool(reasons),
        reasons=tuple(reasons),
        readonly_commands=tuple(readonly),
        write_commands=tuple(writes),
    )


def confirmation_speech(decision: SafetyDecision, summary: str) -> str:
    why = "、".join(decision.reasons) or "写入"
    brief = summarize_for_speech(summary, limit=50)
    if brief:
        body = f"要{why}。{brief}点一下屏幕确认，或者说确认。"
    else:
        body = f"要{why}。点一下屏幕确认，或者说确认。"
    return body if body.startswith("😶") else "😶" + body


def normalize_reply(text: str) -> str:
    return re.sub(r"[\s，。！？、,.!?；;：:~～\"'“”‘’…]+", "", text or "")


def classify_confirm_reply(text: str) -> str:
    """待确认时，用户这句话是同意、拒绝，还是别的内容。"""
    n = normalize_reply(text)
    if n in YES_REPLIES:
        return "yes"
    if n in NO_REPLIES:
        return "no"
    return "other"


def match_stop(text: str) -> bool:
    """整句就是停止或取消。长句子里带这两个词不当成停止，避免把正常请求杀掉。"""
    return normalize_reply(text) in NO_REPLIES


def match_continue_phrase(text: str) -> bool:
    """整句就是「继续」一类。长句里的「继续」仍走原来的关键字匹配，避免误伤。"""
    return normalize_reply(text) in RESUME_EXACT


def looks_like_missing_session(detail: str) -> bool:
    return bool(_MISSING_SESSION.search(detail or ""))


def looks_like_auth(detail: str) -> bool:
    return bool(_AUTH.search(detail or ""))

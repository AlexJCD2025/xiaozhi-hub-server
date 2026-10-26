"""解析 `codex exec --json` 打出来的 JSONL。

只认 Codex CLI 0.153 这一代的事件（thread.started、item.started / item.completed）。
解析失败的行丢掉，不把原始行写进日志以外的地方。
"""

import json
from dataclasses import dataclass
from typing import Optional


@dataclass
class CodexEvent:
    kind: str  # thread / command_started / command_finished / message / file_change / error / turn_failed / turn_completed / turn_started / other
    thread_id: str = ""
    command: str = ""
    text: str = ""
    message: str = ""
    exit_code: Optional[int] = None
    raw_type: str = ""


def _command_text(value) -> str:
    if isinstance(value, list):
        return " ".join(str(part) for part in value)
    return str(value or "")


def parse_line(line: str) -> Optional[CodexEvent]:
    """解析一行 JSON。不是 JSON、或不是对象时返回 None。"""
    raw = (line or "").strip()
    if not raw:
        return None
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    # 旧一些的包装：{"msg": {"type": ...}}。0.153 的 --json 直接把 type 放在顶层。
    if "type" not in obj and isinstance(obj.get("msg"), dict):
        obj = obj["msg"]
    typ = str(obj.get("type") or "")
    if typ == "thread.started":
        thread_id = obj.get("thread_id") or obj.get("threadId") or ""
        return CodexEvent("thread", thread_id=str(thread_id or ""))
    if typ in ("item.started", "item.completed", "item.updated"):
        item = obj.get("item") if isinstance(obj.get("item"), dict) else {}
        itype = str(item.get("type") or "")
        if itype == "command_execution":
            command = _command_text(item.get("command"))
            status = str(item.get("status") or "")
            if typ == "item.started" or status == "in_progress":
                return CodexEvent("command_started", command=command, raw_type=itype)
            code = item.get("exit_code")
            try:
                code = int(code) if code is not None else None
            except (TypeError, ValueError):
                code = None
            return CodexEvent("command_finished", command=command, exit_code=code, raw_type=itype)
        if itype == "agent_message" and typ == "item.completed":
            return CodexEvent("message", text=str(item.get("text") or ""), raw_type=itype)
        if itype == "file_change":
            return CodexEvent("file_change", raw_type=itype)
        return CodexEvent("other", raw_type=itype or typ)
    if typ == "turn.failed":
        err = obj.get("error")
        message = err.get("message") if isinstance(err, dict) else str(err or obj.get("message") or "")
        return CodexEvent("turn_failed", message=str(message or ""))
    if typ == "error":
        return CodexEvent("error", message=str(obj.get("message") or ""))
    if typ == "turn.completed":
        return CodexEvent("turn_completed")
    if typ == "turn.started":
        return CodexEvent("turn_started")
    return CodexEvent("other", raw_type=typ)

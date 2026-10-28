"""给家长看的本地对话记录。滚动保存，不写密钥。"""

import json
import re
from pathlib import Path


def redact(text: str, limit: int = 2000) -> str:
    cleaned = re.sub(r"sk-[A-Za-z0-9_\-]{8,}|ghp_[A-Za-z0-9]{8,}", "[已隐藏]", text or "")
    cleaned = re.sub(
        r"(?i)((?:api[_-]?key|password|passwd|密码)\s*[:：是为]?\s*)\S+",
        r"\1[已隐藏]",
        cleaned,
    )
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if len(cleaned) > limit:
        return cleaned[:limit] + "…"
    return cleaned


def append_record(directory: Path, record: dict, max_bytes: int, backups: int) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "chat.jsonl"
    line = json.dumps(record, ensure_ascii=False) + "\n"
    encoded = line.encode("utf-8")
    if path.exists() and path.stat().st_size + len(encoded) > max_bytes:
        _rotate(path, backups)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line)


def _rotate(path: Path, backups: int) -> None:
    oldest = path.with_name(f"{path.name}.{backups}")
    if oldest.exists():
        oldest.unlink()
    for index in range(backups - 1, 0, -1):
        src = path.with_name(f"{path.name}.{index}")
        if src.exists():
            src.rename(path.with_name(f"{path.name}.{index + 1}"))
    if path.exists():
        path.rename(path.with_name(f"{path.name}.1"))

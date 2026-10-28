"""儿童模式的配置。密钥优先放在环境变量里，不要写进会提交的文件。"""

import os
from dataclasses import dataclass
from typing import Mapping, Optional, Sequence


def as_bool(value, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _blank(value) -> bool:
    if value is None:
        return True
    text = str(value).strip()
    return text == "" or "你" in text or text.endswith("-xxx") or text in ("not-needed", "sk-xxx")


def _resolve_text(value, env_name: str, env: Mapping[str, str]) -> str:
    if value is None:
        return env.get(env_name, "").strip()
    text = str(value).strip()
    if text.startswith("${") and text.endswith("}") and len(text) > 3:
        return env.get(text[2:-1], "").strip()
    lowered = text.lower()
    if lowered.startswith("env:") and len(text) > 4:
        return env.get(text[4:].strip(), "").strip()
    if _blank(text):
        return env.get(env_name, "").strip()
    return text


@dataclass(frozen=True)
class KidSettings:
    base_url: str
    model: str
    api_key: str
    max_tokens: int
    temperature: float
    isolate: bool
    safety_enabled: bool
    moderation: bool
    extra_block_phrases: tuple
    extra_persona: str
    max_turns_per_day: int
    quiet_hours: str
    conversation_log: bool
    log_max_bytes: int
    log_backups: int
    history_limit: int

    @property
    def ready(self) -> bool:
        return bool(self.base_url and self.model and self.api_key)


def _int(value, default: int, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def _phrases(value) -> tuple:
    if isinstance(value, str):
        value = [value]
    found = []
    for item in value or []:
        text = str(item or "").strip()
        if len(text) < 2 or len(text) > 40:
            continue
        if text not in found:
            found.append(text)
        if len(found) >= 50:
            break
    return tuple(found)


def resolve_kid_config(entry: Optional[dict], env: Optional[Mapping[str, str]] = None) -> KidSettings:
    """把 YAML 条目和环境变量合成一份配置。YAML 里的真实值优先于环境变量。"""
    entry = entry or {}
    env = env if env is not None else os.environ
    try:
        temperature = float(entry.get("temperature", 0.6))
    except (TypeError, ValueError):
        temperature = 0.6
    temperature = max(0.0, min(1.5, temperature))
    return KidSettings(
        base_url=_resolve_text(entry.get("base_url", entry.get("url")), "KID_MODE_BASE_URL", env).rstrip("/"),
        model=_resolve_text(entry.get("model_name", entry.get("model")), "KID_MODE_MODEL", env),
        api_key=_resolve_text(entry.get("api_key"), "KID_MODE_API_KEY", env),
        max_tokens=_int(entry.get("max_tokens"), 180, 32, 800),
        temperature=temperature,
        isolate=as_bool(entry.get("isolate", True), default=True),
        safety_enabled=as_bool(entry.get("safety_enabled", True), default=True),
        moderation=as_bool(entry.get("moderation", False), default=False),
        extra_block_phrases=_phrases(entry.get("extra_block_phrases")),
        extra_persona=str(entry.get("extra_persona") or "").strip()[:800],
        max_turns_per_day=_int(entry.get("max_turns_per_day"), 0, 0, 10000),
        quiet_hours=str(entry.get("quiet_hours") or "").strip(),
        conversation_log=as_bool(entry.get("conversation_log", True), default=True),
        log_max_bytes=_int(entry.get("log_max_bytes"), 1_000_000, 16_384, 20_000_000),
        log_backups=_int(entry.get("log_backups"), 5, 1, 20),
        history_limit=_int(entry.get("history_limit"), 16, 2, 40),
    )


def kid_isolates(config: Optional[dict], name: Optional[str]) -> bool:
    """当前模型是儿童模式且开启隔离时，不允许呼名切到 Codex 或其他 agent。"""
    if not name:
        return False
    entry = ((config or {}).get("LLM") or {}).get(name)
    if not isinstance(entry, dict) or entry.get("type") != "kid_mode":
        return False
    return as_bool(entry.get("isolate", True), default=True)

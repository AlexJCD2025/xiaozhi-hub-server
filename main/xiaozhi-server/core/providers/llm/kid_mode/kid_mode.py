"""儿童模式：只调用一个 OpenAI 兼容的聊天接口。

没有工具、没有命令行、不能读文件，也不能切到 Codex 或其他 agent（隔离默认开启）。
人设和安全规则写在请求里；发送前和收到回复后再做一次关键词检查。
"""

from datetime import datetime
from pathlib import Path
from typing import List, Optional
from urllib.parse import urlparse

from config.config_loader import get_project_dir
from config.logger import setup_logging
from core.providers.llm.base import LLMProviderBase
from core.providers.llm.kid_mode.journal import append_record, redact
from core.providers.llm.kid_mode.limits import allow_turn, limit_reply, quiet_active, quiet_reply, shanghai_now
from core.providers.llm.kid_mode.persona import system_prompt
from core.providers.llm.kid_mode.safety import (
    check_text,
    moderation_category,
    prefers_english,
    reply_for_category,
    without_tools,
)
from core.providers.llm.kid_mode.settings import KidSettings, resolve_kid_config

TAG = __name__
logger = setup_logging()


def _not_ready_reply(text: str) -> str:
    if prefers_english(text):
        return "Kid mode is not set up yet. Please ask a parent to check the API settings."
    return "儿童模式还没准备好。请让爸爸妈妈检查接口和密钥。"


def _network_reply(text: str) -> str:
    if prefers_english(text):
        return "The network hiccuped. Let's try again in a moment."
    return "网络有点问题，等一下再说吧。"


def _auth_reply(text: str) -> str:
    if prefers_english(text):
        return "The API key was not accepted. Please ask a parent to check it."
    return "儿童模式的密钥不对，请让爸爸妈妈检查。"


class LLMProvider(LLMProviderBase):
    def __init__(self, config, complete=None, clock=None, moderate=None, data_dir=None):
        self.settings: KidSettings = resolve_kid_config(config or {})
        self._complete_fn = complete
        self._clock = clock
        self._moderate = moderate
        self._client = None
        root = Path(data_dir) if data_dir else Path(get_project_dir()) / "data" / "kid_mode"
        self._log_dir = root
        self._usage_path = root / "usage.json"
        host = urlparse(self.settings.base_url).netloc if self.settings.base_url else "未配置"
        logger.bind(tag=TAG).info(
            f"儿童模式：{host}，模型 {self.settings.model or '未配置'}，"
            f"隔离={self.settings.isolate}，安全检查={self.settings.safety_enabled}，"
            f"每天次数={self.settings.max_turns_per_day or '不限'}，安静时段={self.settings.quiet_hours or '关'}"
        )

    def _now(self) -> datetime:
        return shanghai_now(self._clock)

    def _last_user(self, dialogue) -> str:
        for msg in reversed(dialogue or []):
            if msg.get("role") == "user" and isinstance(msg.get("content"), str):
                return msg["content"].strip()
        return ""

    def _messages(self, dialogue) -> List[dict]:
        history = []
        for msg in dialogue or []:
            role = msg.get("role")
            content = msg.get("content")
            if role not in ("user", "assistant") or not isinstance(content, str) or not content.strip():
                continue
            if "<tool_call>" in content.lower():
                continue
            history.append({"role": role, "content": content.strip()})
        history = history[-self.settings.history_limit :]
        return [{"role": "system", "content": system_prompt(self.settings.extra_persona)}, *history]

    def _guard(self, text: str) -> Optional[str]:
        if not self.settings.safety_enabled:
            return None
        hit = check_text(text, self.settings.extra_block_phrases)
        if hit:
            return hit.reply_for(text)
        category = self._moderation_category(text)
        if category:
            return reply_for_category(category, text)
        return None

    def _moderation_category(self, text: str) -> Optional[str]:
        if not self.settings.moderation or not text:
            return None
        try:
            if self._moderate is not None:
                payload = self._moderate(text)
            else:
                payload = self._openai_moderate(text)
        except Exception:
            logger.bind(tag=TAG).warning("审核接口不可用，只使用关键词检查")
            return None
        if isinstance(payload, str):
            return payload or None
        return moderation_category(payload or {})

    def _openai_moderate(self, text: str) -> dict:
        client = self._client_or_create()
        result = client.moderations.create(model="omni-moderation-latest", input=text[:8000])
        dumped = result.model_dump() if hasattr(result, "model_dump") else {}
        return dumped

    def _client_or_create(self):
        if self._client is None:
            import openai

            self._client = openai.OpenAI(api_key=self.settings.api_key, base_url=self.settings.base_url, timeout=60)
        return self._client

    def _complete(self, messages: List[dict]) -> str:
        if self._complete_fn is not None:
            return self._complete_fn(messages)
        import openai

        client = self._client_or_create()
        try:
            result = client.chat.completions.create(
                model=self.settings.model,
                messages=messages,
                max_tokens=self.settings.max_tokens,
                temperature=self.settings.temperature,
                stream=False,
            )
        except openai.AuthenticationError as exc:
            raise PermissionError("auth") from exc
        choice = result.choices[0].message.content if result.choices else ""
        return choice or ""

    def _log(self, session_id: str, user: str, assistant: str, blocked: Optional[str]) -> None:
        if not self.settings.conversation_log:
            return
        record = {
            "ts": self._now().isoformat(timespec="seconds"),
            "session": redact(session_id or "", limit=80),
            "user": redact(user),
            "assistant": redact(assistant),
            "blocked": blocked,
        }
        try:
            append_record(self._log_dir, record, self.settings.log_max_bytes, self.settings.log_backups)
        except OSError:
            logger.bind(tag=TAG).warning("儿童模式对话记录写不进去")

    def response(self, session_id, dialogue, **kwargs):
        user = self._last_user(dialogue)
        if not self.settings.ready:
            yield _not_ready_reply(user)
            return

        now = self._now()
        if quiet_active(self.settings.quiet_hours, now):
            reply = quiet_reply(user)
            self._log(session_id, user, reply, "quiet_hours")
            yield reply
            return
        if not allow_turn(self._usage_path, session_id or "device", now.date().isoformat(), self.settings.max_turns_per_day):
            reply = limit_reply(user)
            self._log(session_id, user, reply, "daily_limit")
            yield reply
            return

        blocked = self._guard(user)
        if blocked:
            self._log(session_id, user, blocked, "input")
            yield blocked
            return

        messages = self._messages(dialogue)
        try:
            raw = self._complete(messages)
        except PermissionError:
            reply = _auth_reply(user)
            self._log(session_id, user, reply, "auth")
            yield reply
            return
        except Exception:
            logger.bind(tag=TAG).warning("儿童模式请求失败")
            reply = _network_reply(user)
            self._log(session_id, user, reply, "error")
            yield reply
            return

        output_block = self._guard(raw)
        if output_block:
            reply = output_block
        else:
            reply = without_tools(raw)
            if not reply:
                reply = (
                    "I can't use the computer. Let's talk about something else."
                    if prefers_english(user)
                    else "我不能操作电脑。我们聊点别的吧。"
                )
        self._log(session_id, user, reply, "output" if output_block or "<tool_call>" in (raw or "").lower() else None)
        yield reply

    def response_with_functions(self, session_id, dialogue, functions=None, **kwargs):
        """插件调用到这里时也不传工具。儿童模式没有函数。"""
        for text in self.response(session_id, dialogue, **kwargs):
            yield text, None

"""grok_bot：让小智设备给一个 Grok Bot（xAI 与 Cursor 的云端 AI 同事）派活，并把它的回复念出来。

一个配置条目对应一个 Bot（bot_id、bot_name），管理后台的"Grok Bot"页勾选 Bot 后自动生成条目，
呼名页再给它配称呼。同一个账号的所有 Bot 共用一条事件流（见 grokbot_client.get_hub）。

连接管理、空闲关闭、填充语、进度提示、超时都继承 claude_cli 的 provider，只换掉会话的创建方式：
- 没有本机进程，"启动会话"只是确认云端电脑醒着并订阅事件流；
- 对话记录在云端，不需要接续逻辑；
- 所有 grok_bot 实例共用一个后台事件循环（事件流中枢必须和会话在同一个循环里）。

凭证由管理后台登录后写到 data/grokbot_credentials.json，这里只读。
"""

import asyncio
import os
import threading
import time
from pathlib import Path
from typing import Optional

from config.config_loader import get_project_dir
from config.logger import setup_logging
from core.agent.agent_session import AgentSessionError, ErrorKind
from core.agent.grokbot_client import Credentials, GrokBotError, default_credentials_path, get_hub
from core.agent.grokbot_session import GrokBotSession, GrokBotSessionConfig, voice_for
from core.agent.session_store import SessionStore
from core.providers.llm.claude_cli.claude_cli import ConnState, LLMProvider as AgentProvider, _num

TAG = __name__
logger = setup_logging()

DEFAULT_FILLER = "交给 Grok Bot 了"

_shared = {"loop": None, "thread": None}
_shared_lock = threading.Lock()


def shared_loop() -> asyncio.AbstractEventLoop:
    """所有 grok_bot 实例共用的后台事件循环（永不停止）。"""
    with _shared_lock:
        loop = _shared["loop"]
        if loop is not None and not loop.is_closed():
            return loop
        loop = asyncio.new_event_loop()

        def run():
            asyncio.set_event_loop(loop)
            loop.run_forever()

        thread = threading.Thread(target=run, name="grok-bot-loop", daemon=True)
        thread.start()
        _shared["loop"], _shared["thread"] = loop, thread
        return loop


class LLMProvider(AgentProvider):
    AGENT_LABEL = "Grok Bot"

    def __init__(self, config: dict):
        self.bot_id = str(config.get("bot_id") or "").strip()
        self.bot_name = str(config.get("bot_name") or "").strip() or self.bot_id[:8]
        self.model_name = f"grok_bot:{self.bot_name}"
        raw_path = str(config.get("credentials_path") or "").strip()
        self.cred_path = Path(os.path.expanduser(raw_path)) if raw_path else default_credentials_path()
        self.backend = str(config.get("backend_url") or "").strip() or None  # 只在测试时指向假服务
        self.session_config = GrokBotSessionConfig(
            bot_id=self.bot_id,
            bot_name=self.bot_name,
            echo_timeout=_num(config, "echo_timeout_seconds", 10),
            poll_interval=_num(config, "poll_interval_seconds", 12),
        )
        self._init_common(config, filler_default=DEFAULT_FILLER)
        self.store = SessionStore(Path(get_project_dir()) / "data" / "agent_sessions.json", "grok_bot")
        self._start_loop()
        self._self_check()

    # ---------- 共用的事件循环 ----------

    def _start_loop(self) -> None:
        self._loop = shared_loop()
        self._thread = _shared["thread"]

    def _maybe_stop(self) -> None:
        """共用的循环不随实例退役而停止；事件流由中枢按"有没有订阅"自己开关。"""
        return

    # ---------- 自检 ----------

    def _self_check(self) -> None:
        if not self.bot_id:
            logger.bind(tag=TAG).error("Grok Bot 条目没有 bot_id，请到管理后台的 Grok Bot 页重新勾选")
            return
        status = Credentials(self.cred_path).status()
        if not status["logged_in"]:
            logger.bind(tag=TAG).warning(
                f"Grok Bot「{self.bot_name}」：{'登录已失效' if status['needs_login'] else '还没登录'}，请到管理后台登录")
            return
        logger.bind(tag=TAG).info(
            f"Grok Bot「{self.bot_name}」：已登录，token 到期 "
            f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(status['expires_at']))}")

    def _voice_error(self, kind: ErrorKind) -> str:
        return voice_for(kind)

    # ---------- 会话 ----------

    async def _ensure_agent(self, st: ConnState, text: str, out, system_prompt: str) -> bool:
        """确保有可用的会话。没有接续指令一说：对话在云端，永远是接着聊。"""
        if st.agent and st.agent.alive:
            return False
        if not self.bot_id:
            raise AgentSessionError(ErrorKind.UNKNOWN, "没有配置 bot_id")
        hub = get_hub(self.cred_path, logger=logger.bind(tag=TAG), backend=self.backend)
        agent = GrokBotSession(hub, self.session_config, logger=logger.bind(tag=TAG))
        t0 = time.time()
        await agent.start()
        if st.closed:
            await agent.close()  # 启动期间连接已经关闭
            raise AgentSessionError(ErrorKind.PROCESS_EXITED, "连接已关闭")
        st.agent = agent
        logger.bind(tag=TAG).info(f"Grok Bot「{self.bot_name}」会话已就绪（{time.time() - t0:.1f}s），设备 {st.key}")
        return False

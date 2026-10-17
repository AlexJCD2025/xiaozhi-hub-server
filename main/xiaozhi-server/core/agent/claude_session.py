"""基于 claude-agent-sdk 的 Claude Code 会话：一个常驻 CLI 进程，多轮对话、可打断、可接续。

注意：SDK 固定为 0.1.81。更新版本依赖 mcp>=1.23，而原项目锁定 mcp==1.22.0，
升级 mcp 到 2.x 会让原项目的 MCP 客户端导入失败。
"""

import asyncio
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import AsyncIterator, List, Optional

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    CLINotFoundError,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    StreamEvent,
    TextBlock,
)
from claude_agent_sdk import ClaudeSDKError

from core.agent.agent_session import (
    AgentEvent,
    AgentSession,
    AgentSessionError,
    Done,
    Error,
    ErrorKind,
    TextDelta,
)

SENTENCE_END = set("。！？!?；;…~～.\n")

ASSISTANT_ERROR_KINDS = {
    "authentication_failed": ErrorKind.AUTH,
    "billing_error": ErrorKind.BILLING,
    "rate_limit": ErrorKind.RATE_LIMIT,
    "server_error": ErrorKind.SERVER,
}


@dataclass
class ClaudeSessionConfig:
    cli_path: Optional[str] = None
    model: Optional[str] = None
    workdir: Optional[str] = None
    allowed_tools: List[str] = field(default_factory=lambda: ["Read", "Grep", "Glob"])
    permission_mode: str = "default"
    system_prompt_append: str = ""


class ClaudeSession(AgentSession):
    def __init__(self, config: ClaudeSessionConfig, resume_id: Optional[str] = None, logger=None):
        self.config = config
        self.resume_id = resume_id
        self.logger = logger
        self._session_id = resume_id or str(uuid.uuid4())
        self._client: Optional[ClaudeSDKClient] = None
        self._interrupting = False
        self._stderr_tail = deque(maxlen=20)

    @property
    def session_id(self) -> Optional[str]:
        return self._session_id

    @property
    def stderr_tail(self) -> str:
        return "\n".join(self._stderr_tail)

    # ---------- 生命周期 ----------

    def _options(self) -> ClaudeAgentOptions:
        allowed = list(self.config.allowed_tools)

        async def can_use_tool(name, tool_input, context):
            # 第二道保险：工具列表之外的一律拒绝。语音场景没有人工审批。
            if name in allowed:
                return PermissionResultAllow()
            return PermissionResultDeny(message="语音模式下不允许使用这个工具")

        kwargs = dict(
            cli_path=self.config.cli_path,
            cwd=self.config.workdir,
            model=self.config.model or None,
            tools=allowed,
            allowed_tools=allowed,
            permission_mode=self.config.permission_mode,
            can_use_tool=can_use_tool,
            include_partial_messages=True,
            setting_sources=[],  # 不加载用户的 hooks、插件等设置，行为可预期
            system_prompt={
                "type": "preset",
                "preset": "claude_code",
                "append": self.config.system_prompt_append,
            },
            stderr=self._stderr_tail.append,
        )
        if self.resume_id:
            kwargs["resume"] = self.resume_id
        else:
            kwargs["session_id"] = self._session_id
        return ClaudeAgentOptions(**kwargs)

    async def start(self) -> None:
        client = ClaudeSDKClient(options=self._options())
        try:
            await client.connect()
        except CLINotFoundError as e:
            raise AgentSessionError(ErrorKind.NOT_INSTALLED, str(e))
        except ClaudeSDKError as e:
            detail = f"{e}\n{self.stderr_tail}".strip()
            if self.resume_id:
                raise AgentSessionError(ErrorKind.SESSION_MISSING, detail)
            raise AgentSessionError(ErrorKind.PROCESS_EXITED, detail)
        self._client = client

    async def close(self) -> None:
        client, self._client = self._client, None
        if client is None:
            return
        try:
            await asyncio.wait_for(client.disconnect(), timeout=5)
        except Exception as e:
            if self.logger:
                self.logger.warning(f"关闭 Claude Code 进程时出错: {e}")

    async def interrupt(self) -> None:
        if self._client is None:
            return
        self._interrupting = True
        try:
            await self._client.interrupt()
        except Exception as e:
            if self.logger:
                self.logger.warning(f"中断 Claude Code 失败: {e}")

    @property
    def alive(self) -> bool:
        return self._client is not None

    # ---------- 一轮对话 ----------

    async def send(self, text: str) -> AsyncIterator[AgentEvent]:
        if self._client is None:
            yield Error(ErrorKind.PROCESS_EXITED, "session not started")
            return
        self._interrupting = False
        streamed = False  # 当前这条 assistant 消息是否已经通过逐字流输出过
        last_char = ""
        try:
            await self._client.query(text)
            async for msg in self._client.receive_response():
                if isinstance(msg, StreamEvent):
                    if msg.parent_tool_use_id is not None:
                        continue  # 子代理的输出不读
                    ev = msg.event or {}
                    delta = ev.get("delta") or {}
                    if ev.get("type") == "content_block_delta" and delta.get("type") == "text_delta":
                        piece = delta.get("text") or ""
                        if piece:
                            if not streamed and last_char and last_char not in SENTENCE_END:
                                yield TextDelta("。")  # 两段话之间补一个句号，方便 TTS 断句
                            streamed = True
                            last_char = piece[-1]
                            yield TextDelta(piece)
                elif isinstance(msg, AssistantMessage):
                    if msg.parent_tool_use_id is not None:
                        continue
                    if msg.error or msg.model == "<synthetic>":
                        # 额度、鉴权等错误会伪装成一条 assistant 消息，不能把原文念出来
                        detail = " ".join(b.text for b in msg.content if isinstance(b, TextBlock))
                        yield Error(ASSISTANT_ERROR_KINDS.get(msg.error or "", ErrorKind.UNKNOWN), detail)
                        streamed = False
                        continue
                    if not streamed:
                        # 没有收到逐字流时，才使用整条消息的文字，避免同一段话念两遍
                        for block in msg.content:
                            if isinstance(block, TextBlock) and block.text:
                                if last_char and last_char not in SENTENCE_END:
                                    yield TextDelta("。")
                                last_char = block.text[-1]
                                yield TextDelta(block.text)
                    streamed = False
                elif isinstance(msg, ResultMessage):
                    if msg.session_id:
                        self._session_id = msg.session_id
                    if msg.is_error and not self._interrupting:
                        yield Error(ErrorKind.UNKNOWN, f"{msg.subtype}: {msg.errors or msg.result or ''}")
                    else:
                        yield Done(session_id=self._session_id, interrupted=self._interrupting)
                    return
            yield Done(session_id=self._session_id, interrupted=self._interrupting)
        except ClaudeSDKError as e:
            self._client = None
            yield Error(ErrorKind.PROCESS_EXITED, f"{e}\n{self.stderr_tail}".strip())
        finally:
            self._interrupting = False

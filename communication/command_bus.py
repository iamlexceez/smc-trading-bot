"""Central command routing, authorization, auditing, and serialization."""

from __future__ import annotations

import asyncio
import logging
import shlex
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable
from uuid import uuid4

from storage import db

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CommandRequest:
    platform: str
    user_id: str
    channel_id: str
    text: str
    command_id: str = field(default_factory=lambda: str(uuid4()))
    received_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def command(self) -> str:
        tokens = shlex.split(self.text.strip()) if self.text.strip() else []
        return (tokens[0].lstrip("/").lower() if tokens else "")

    @property
    def arguments(self) -> list[str]:
        tokens = shlex.split(self.text.strip()) if self.text.strip() else []
        return tokens[1:]


@dataclass(frozen=True, slots=True)
class CommandResponse:
    text: str
    ok: bool = True
    requires_confirmation: bool = False
    confirmation_token: str | None = None


CommandHandler = Callable[[CommandRequest], Awaitable[CommandResponse | str]]
Authorizer = Callable[[CommandRequest, bool], Awaitable[bool] | bool]


class CommandBus:
    """One serialized command path for every user interface."""

    def __init__(self, *, db_module: Any = db, db_path: str | None = None, authorizer: Authorizer | None = None):
        self.db = db_module
        self.db_path = db_path
        self.authorizer = authorizer or (lambda request, dangerous: True)
        self._handlers: dict[str, tuple[CommandHandler, bool]] = {}
        self._state_lock = asyncio.Lock()
        self._pending_confirmations: dict[str, tuple[str, float]] = {}

    def register(self, name: str, handler: CommandHandler, *, dangerous: bool = False) -> None:
        normalized = name.lstrip("/").lower().strip()
        if not normalized:
            raise ValueError("command name cannot be empty")
        self._handlers[normalized] = (handler, dangerous)

    async def dispatch(self, request: CommandRequest) -> CommandResponse:
        command = request.command
        pending = self._pending_confirmations.get(request.user_id)
        if command == "confirm" and pending and request.arguments and request.arguments[0] == pending[0]:
            token, pending_command, _deadline = pending
            request = CommandRequest(
                platform=request.platform,
                user_id=request.user_id,
                channel_id=request.channel_id,
                text=f"/{pending_command} --confirm {token}",
                command_id=request.command_id,
                received_at=request.received_at,
            )
            command = request.command
        handler_entry = self._handlers.get(command)
        if handler_entry is None:
            response = CommandResponse(f"Unknown command: `/{command or 'empty'}`", ok=False)
            await self._audit(request, "NOT_REGISTERED", response.text, "REJECTED")
            return response

        handler, dangerous = handler_entry
        authorized = self.authorizer(request, dangerous)
        if asyncio.iscoroutine(authorized):
            authorized = await authorized
        if not authorized:
            response = CommandResponse("⛔ Not authorized for this command.", ok=False)
            await self._audit(request, "DENIED", response.text, "REJECTED")
            return response

        if dangerous and not self._is_confirmed(request):
            token = str(uuid4())
            self._pending_confirmations[request.user_id] = (token, command, asyncio.get_running_loop().time() + 120.0)
            response = CommandResponse(
                f"⚠️ Confirmation required for `/{command}`. Reply with `/confirm {token}` within 120 seconds.",
                ok=False,
                requires_confirmation=True,
                confirmation_token=token,
            )
            await self._audit(request, "AUTHORIZED_CONFIRMATION_REQUIRED", response.text, "PENDING")
            return response

        # All state-changing commands share one lock, preventing Telegram and
        # Slack from mutating objective/pause state concurrently.
        lock = self._state_lock if dangerous else _NoopAsyncLock()
        async with lock:
            try:
                result = await handler(request)
                response = result if isinstance(result, CommandResponse) else CommandResponse(str(result))
                await self._audit(request, "AUTHORIZED", response.text, "DELIVERED")
                return response
            except Exception as exc:
                response = CommandResponse(f"Command failed: `{type(exc).__name__}`", ok=False)
                await self._audit(request, "AUTHORIZED", str(exc), "FAILED")
                raise

    def _is_confirmed(self, request: CommandRequest) -> bool:
        pending = self._pending_confirmations.get(request.user_id)
        if not pending:
            return False
        token, pending_command, deadline = pending
        if pending_command != request.command and request.command != "confirm":
            return False
        if asyncio.get_running_loop().time() > deadline:
            self._pending_confirmations.pop(request.user_id, None)
            return False
        if request.command == "confirm" and request.arguments:
            confirmed = request.arguments[0] == token
        else:
            try:
                marker = next(index for index, value in enumerate(request.arguments) if value.lower() in {"confirm", "--confirm"})
                confirmed = marker + 1 < len(request.arguments) and request.arguments[marker + 1] == token
            except StopIteration:
                confirmed = False
        if confirmed:
            self._pending_confirmations.pop(request.user_id, None)
        return confirmed

    async def _audit(self, request: CommandRequest, authorization: str, result: str, status: str) -> None:
        if not self.db or not hasattr(self.db, "record_command_audit"):
            return
        kwargs = {"db_path": self.db_path} if self.db_path else {}
        try:
            await self.db.record_command_audit(
                command_id=request.command_id,
                user_id=request.user_id,
                platform=request.platform,
                channel_id=request.channel_id,
                command=request.command,
                arguments=" ".join(request.arguments),
                authorization_result=authorization,
                execution_result=result,
                response_status=status,
                **kwargs,
            )
        except Exception:
            logger.exception("Command audit persistence failed")


class _NoopAsyncLock:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

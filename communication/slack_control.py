"""Optional Slack Socket Mode command adapter.

Socket Mode keeps the Windows VPS private: Slack delivers events over an
outbound WebSocket, while command authorization remains inside CommandBus.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import Any

from .command_bus import CommandBus, CommandRequest

logger = logging.getLogger(__name__)


class SlackSocketControl:
    def __init__(self, command_bus: CommandBus):
        self.command_bus = command_bus
        self.client: Any | None = None
        self._task: asyncio.Task | None = None

    @property
    def configured(self) -> bool:
        return bool(os.getenv("SLACK_APP_TOKEN", "").strip() and os.getenv("SLACK_BOT_TOKEN", "").strip())

    async def start(self) -> bool:
        if not self.configured:
            logger.info("Slack control disabled: SLACK_APP_TOKEN or SLACK_BOT_TOKEN is not configured")
            return False
        try:
            from slack_sdk.socket_mode.aiohttp import SocketModeClient
            from slack_sdk.socket_mode.request import SocketModeRequest
            from slack_sdk.socket_mode.response import SocketModeResponse
            from slack_sdk.web.async_client import AsyncWebClient

            self.client = SocketModeClient(
                app_token=os.environ["SLACK_APP_TOKEN"],
                web_client=AsyncWebClient(token=os.environ["SLACK_BOT_TOKEN"]),
            )

            async def process(client: Any, request: SocketModeRequest) -> None:
                await client.send_socket_mode_response(SocketModeResponse(envelope_id=request.envelope_id))
                payload = request.payload or {}
                if request.type == "slash_commands":
                    await self._handle_payload(client, payload)
                    return
                if request.type != "events_api":
                    return
                event = payload.get("event") or {}
                if event.get("type") != "message" or event.get("subtype"):
                    return
                await self._handle_payload(client, event)

            self.client.socket_mode_request_listeners.append(process)
            self._task = asyncio.create_task(self.client.connect(), name="slack_socket_mode_connect")
            logger.info("Slack Socket Mode control adapter started")
            return True
        except ImportError:
            logger.error("Slack control unavailable: install slack-sdk on the VPS")
            return False
        except Exception:
            logger.exception("Slack Socket Mode startup failed; Telegram and trading remain independent")
            return False

    async def stop(self) -> None:
        if self.client is None:
            return
        try:
            disconnect = getattr(self.client, "disconnect", None)
            if disconnect:
                result = disconnect()
                if asyncio.iscoroutine(result):
                    await result
        except Exception:
            logger.exception("Slack Socket Mode shutdown failed")
        finally:
            self.client = None

    async def _handle_payload(self, client: Any, payload: dict[str, Any]) -> None:
        user_id = str(payload.get("user_id") or (payload.get("user") or {}).get("id") or "")
        channel_id = str(payload.get("channel_id") or payload.get("channel") or "")
        text = str(payload.get("text") or "").strip()
        if not user_id or not channel_id or not text:
            return
        # In channel mentions, remove the bot mention before parsing /commands.
        text = re.sub(r"<@[^>]+>", "", text).strip()
        request = CommandRequest(
            platform="slack",
            user_id=user_id,
            channel_id=channel_id,
            text=text,
        )
        response = await self.command_bus.dispatch(request)
        try:
            await client.web_client.chat_postMessage(channel=channel_id, text=response.text)
        except Exception:
            logger.exception("Could not send Slack command response")

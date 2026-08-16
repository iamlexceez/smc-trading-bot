"""Unified Telegram/Slack communication layer."""

from .events import DeliveryChannel, EventSeverity, NotificationEvent
from .command_bus import CommandBus, CommandRequest, CommandResponse

__all__ = [
    "CommandBus",
    "CommandRequest",
    "CommandResponse",
    "DeliveryChannel",
    "EventSeverity",
    "NotificationEvent",
]

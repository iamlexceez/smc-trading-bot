"""Unified Telegram/Slack communication layer."""

from .events import DeliveryChannel, EventSeverity, NotificationEvent

__all__ = ["DeliveryChannel", "EventSeverity", "NotificationEvent"]

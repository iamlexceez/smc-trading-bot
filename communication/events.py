"""Shared event model and notification routing policy.

The trading engine publishes immutable events; channel adapters only deliver
those events. No channel is allowed to own trading state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any
from uuid import uuid4


class EventSeverity(StrEnum):
    CRITICAL = "CRITICAL"
    IMPORTANT = "IMPORTANT"
    PERIODIC = "PERIODIC"
    SILENT = "SILENT"


class DeliveryChannel(StrEnum):
    TELEGRAM = "telegram"
    SLACK = "slack"


# Events are deliberately allow-listed. Unknown event types default to the
# conservative important route rather than silently disappearing.
ROUTING_MATRIX: dict[str, tuple[DeliveryChannel, ...]] = {
    "trade_opened": (DeliveryChannel.TELEGRAM, DeliveryChannel.SLACK),
    "trade_closed": (DeliveryChannel.TELEGRAM, DeliveryChannel.SLACK),
    "protection_failure": (DeliveryChannel.TELEGRAM, DeliveryChannel.SLACK),
    "emergency_stop": (DeliveryChannel.TELEGRAM, DeliveryChannel.SLACK),
    "critical_broker_failure": (DeliveryChannel.TELEGRAM, DeliveryChannel.SLACK),
    "critical_execution_failure": (DeliveryChannel.TELEGRAM, DeliveryChannel.SLACK),
    "objective_completed": (DeliveryChannel.TELEGRAM, DeliveryChannel.SLACK),
    "objective_failed": (DeliveryChannel.TELEGRAM, DeliveryChannel.SLACK),
    "objective_session_started": (DeliveryChannel.TELEGRAM, DeliveryChannel.SLACK),
    "core_universe_changed": (DeliveryChannel.TELEGRAM, DeliveryChannel.SLACK),
    "best_executable_opportunity": (DeliveryChannel.TELEGRAM, DeliveryChannel.SLACK),
    "profit_protection": (DeliveryChannel.TELEGRAM, DeliveryChannel.SLACK),
    "broker_status": (DeliveryChannel.SLACK,),
    "health_update": (DeliveryChannel.SLACK,),
    "learning_summary": (DeliveryChannel.TELEGRAM, DeliveryChannel.SLACK),
    "research_summary": (DeliveryChannel.SLACK,),
    "diagnostic_error": (DeliveryChannel.SLACK,),
}


@dataclass(frozen=True, slots=True)
class NotificationEvent:
    """Immutable event emitted by the trading core."""

    event_type: str
    message: str
    severity: EventSeverity = EventSeverity.IMPORTANT
    dedupe_key: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    channels: tuple[DeliveryChannel, ...] | None = None
    event_id: str = field(default_factory=lambda: str(uuid4()))
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    photo: bytes | None = field(default=None, repr=False, compare=False)

    @property
    def resolved_dedupe_key(self) -> str:
        return self.dedupe_key or f"{self.event_type}:{self.message}"

    @property
    def target_channels(self) -> tuple[DeliveryChannel, ...]:
        if self.channels is not None:
            return self.channels
        if self.severity is EventSeverity.SILENT:
            return ()
        if self.event_type in ROUTING_MATRIX:
            return ROUTING_MATRIX[self.event_type]
        if self.severity is EventSeverity.PERIODIC:
            return (DeliveryChannel.SLACK,)
        return (DeliveryChannel.TELEGRAM, DeliveryChannel.SLACK)

    @property
    def persistent(self) -> bool:
        return self.severity is EventSeverity.CRITICAL

    def serialized_payload(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "severity": self.severity.value,
            "message": self.message,
            "dedupe_key": self.resolved_dedupe_key,
            "payload": self.payload,
            "channels": [channel.value for channel in self.target_channels],
            "created_at": self.created_at.isoformat(),
        }

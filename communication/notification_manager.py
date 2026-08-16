"""Unified notification manager and isolated channel adapters."""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from time import monotonic
from typing import Any, Protocol

from telegram.error import RetryAfter

from .events import DeliveryChannel, EventSeverity, NotificationEvent

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    channel: DeliveryChannel
    delivered: bool
    error: str | None = None
    retry_after: float | None = None


class ChannelAdapter(Protocol):
    channel: DeliveryChannel

    async def send(self, event: NotificationEvent) -> DeliveryResult:
        ...


class TelegramAdapter:
    channel = DeliveryChannel.TELEGRAM

    def __init__(self, bot_app: Any, chat_id: int | None):
        self.bot_app = bot_app
        self.chat_id = chat_id
        self._lock = asyncio.Lock()
        self._next_allowed_at = 0.0
        self._backoff_until = 0.0

    async def send(self, event: NotificationEvent) -> DeliveryResult:
        if not self.bot_app or not self.chat_id:
            return DeliveryResult(self.channel, False, "telegram_not_configured")
        async with self._lock:
            now = monotonic()
            if now < self._backoff_until:
                return DeliveryResult(self.channel, False, "telegram_backoff", self._backoff_until - now)
            wait_for = max(0.0, self._next_allowed_at - now)
            if wait_for:
                await asyncio.sleep(wait_for)
            try:
                if event.photo:
                    await self.bot_app.bot.send_photo(self.chat_id, event.photo, caption=event.message)
                else:
                    await self.bot_app.bot.send_message(self.chat_id, event.message)
                self._next_allowed_at = monotonic() + 1.05
                return DeliveryResult(self.channel, True)
            except RetryAfter as exc:
                retry_after = float(getattr(exc, "retry_after", 60.0))
                self._backoff_until = monotonic() + retry_after
                logger.warning("Telegram rate limit engaged for %.0fs", retry_after)
                return DeliveryResult(self.channel, False, "telegram_rate_limited", retry_after)
            except Exception as exc:
                logger.error("Telegram delivery failed without affecting trading: %s", exc)
                return DeliveryResult(self.channel, False, str(exc))


class SlackWebhookAdapter:
    channel = DeliveryChannel.SLACK

    def __init__(self, webhook_url: str | None = None):
        fallback = (webhook_url or os.getenv("SLACK_WEBHOOK_URL", "")).strip()
        self.webhook_url = fallback
        self.webhook_urls = {
            "alerts": os.getenv("SLACK_ALERTS_WEBHOOK_URL", fallback).strip(),
            "operations": os.getenv("SLACK_OPERATIONS_WEBHOOK_URL", fallback).strip(),
            "learning": os.getenv("SLACK_LEARNING_WEBHOOK_URL", fallback).strip(),
            "research": os.getenv("SLACK_RESEARCH_WEBHOOK_URL", fallback).strip(),
            "errors": os.getenv("SLACK_ERRORS_WEBHOOK_URL", fallback).strip(),
        }
        self._lock = asyncio.Lock()
        self._next_allowed_at = 0.0
        self._backoff_until = 0.0

    async def send(self, event: NotificationEvent) -> DeliveryResult:
        webhook_url = self.webhook_urls.get(event.slack_route, self.webhook_url)
        if not webhook_url:
            return DeliveryResult(self.channel, False, "slack_not_configured")
        async with self._lock:
            now = monotonic()
            if now < self._backoff_until:
                return DeliveryResult(self.channel, False, "slack_backoff", self._backoff_until - now)
            wait_for = max(0.0, self._next_allowed_at - now)
            if wait_for:
                await asyncio.sleep(wait_for)
            try:
                import requests

                response = await asyncio.to_thread(
                    requests.post,
                    webhook_url,
                    json={"text": event.message.replace("**", "").replace("`", "")},
                    timeout=10,
                )
                if response.status_code == 429:
                    retry_after = 60.0
                    try:
                        retry_after = float(response.headers.get("Retry-After", retry_after))
                    except (TypeError, ValueError):
                        pass
                    self._backoff_until = monotonic() + retry_after
                    return DeliveryResult(self.channel, False, "slack_rate_limited", retry_after)
                if response.status_code >= 400:
                    return DeliveryResult(self.channel, False, f"slack_http_{response.status_code}")
                self._next_allowed_at = monotonic() + 1.05
                return DeliveryResult(self.channel, True)
            except Exception as exc:
                logger.error("Slack delivery failed without affecting trading: %s", exc)
                return DeliveryResult(self.channel, False, str(exc))


class NotificationManager:
    """Single event delivery boundary for all human-facing notifications."""

    def __init__(
        self,
        *,
        telegram: TelegramAdapter | None = None,
        slack: SlackWebhookAdapter | None = None,
        db_module: Any | None = None,
        db_path: str | None = None,
        dedupe_ttl_seconds: float = 300.0,
    ):
        self.adapters: dict[DeliveryChannel, ChannelAdapter] = {}
        if telegram:
            self.adapters[DeliveryChannel.TELEGRAM] = telegram
        if slack:
            self.adapters[DeliveryChannel.SLACK] = slack
        self.db = db_module
        self.db_path = db_path
        self.dedupe_ttl_seconds = dedupe_ttl_seconds
        self._dedupe: dict[str, float] = {}
        self._publish_lock = asyncio.Lock()

    async def publish(self, event: NotificationEvent) -> list[DeliveryResult]:
        if event.severity is EventSeverity.SILENT or not event.target_channels:
            return []
        async with self._publish_lock:
            now = monotonic()
            previous = self._dedupe.get(event.resolved_dedupe_key)
            if previous is not None and now - previous < self.dedupe_ttl_seconds:
                return [DeliveryResult(channel, False, "deduplicated") for channel in event.target_channels]
            self._dedupe[event.resolved_dedupe_key] = now
            self._prune_dedupe(now)
            if event.persistent and self.db and hasattr(self.db, "record_notification_event"):
                kwargs = {"db_path": self.db_path} if self.db_path else {}
                try:
                    await self.db.record_notification_event(event, **kwargs)
                except Exception:
                    logger.exception("Critical notification persistence failed; trading remains isolated")

        results: list[DeliveryResult] = []
        # Deliver independently. A failure in one platform must not prevent the
        # other platform from receiving the event. Only critical events require
        # persistent delivery records; routine chart activity must not create
        # database rows or depend on schema availability.
        persist_delivery = event.persistent and self.db and hasattr(self.db, "record_notification_delivery")
        for channel in event.target_channels:
            adapter = self.adapters.get(channel)
            if adapter is None:
                result = DeliveryResult(channel, False, "adapter_not_configured")
            else:
                result = await adapter.send(event)
            results.append(result)
            if persist_delivery:
                kwargs = {"db_path": self.db_path} if self.db_path else {}
                try:
                    await self.db.record_notification_delivery(
                        event.event_id,
                        channel.value,
                        result.delivered,
                        result.error,
                        result.retry_after,
                        **kwargs,
                    )
                except Exception:
                    logger.exception("Notification delivery-state persistence failed")
        return results

    def _prune_dedupe(self, now: float) -> None:
        cutoff = now - self.dedupe_ttl_seconds
        self._dedupe = {key: timestamp for key, timestamp in self._dedupe.items() if timestamp >= cutoff}

    async def retry_pending(self, limit: int = 50) -> int:
        """Retry persisted critical events when storage support is available."""
        if not self.db or not hasattr(self.db, "get_pending_notification_events"):
            return 0
        kwargs = {"db_path": self.db_path} if self.db_path else {}
        try:
            pending = await self.db.get_pending_notification_events(limit=limit, **kwargs)
        except Exception:
            logger.exception("Could not load pending critical notifications")
            return 0
        count = 0
        for item in pending:
            event = NotificationEvent(
                event_id=item["event_id"],
                event_type=item["event_type"],
                message=item["message"],
                severity=EventSeverity(item["severity"]),
                dedupe_key=item["dedupe_key"],
                payload=item.get("payload", {}),
                channels=tuple(DeliveryChannel(value) for value in item.get("channels", [])),
            )
            # Do not let the retry path suppress a persisted event solely because
            # the original process already saw its dedupe key.
            self._dedupe.pop(event.resolved_dedupe_key, None)
            await self.publish(event)
            count += 1
        return count

import asyncio
import json
from pathlib import Path

from communication.events import DeliveryChannel, EventSeverity, NotificationEvent
from communication.notification_manager import DeliveryResult, NotificationManager
from storage import db


class FakeAdapter:
    def __init__(self, channel, delivered=True):
        self.channel = channel
        self.delivered = delivered
        self.events = []

    async def send(self, event):
        self.events.append(event.event_id)
        return DeliveryResult(
            self.channel,
            self.delivered,
            None if self.delivered else "simulated_failure",
        )


def test_critical_event_is_persisted_and_routed_to_both_channels(tmp_path: Path):
    path = tmp_path / "notifications.db"

    async def scenario():
        await db.init_db(str(path))
        telegram = FakeAdapter(DeliveryChannel.TELEGRAM)
        slack = FakeAdapter(DeliveryChannel.SLACK)
        manager = NotificationManager(
            telegram=telegram,
            slack=slack,
            db_module=db,
            db_path=str(path),
        )
        event = NotificationEvent(
            event_type="emergency_stop",
            message="Emergency stop engaged",
            severity=EventSeverity.CRITICAL,
            dedupe_key="emergency:1",
        )
        results = await manager.publish(event)
        assert all(result.delivered for result in results)
        assert telegram.events == [event.event_id]
        assert slack.events == [event.event_id]

        async with db.aiosqlite.connect(str(path)) as conn:
            event_row = await (await conn.execute(
                "SELECT severity, event_type FROM notification_events WHERE event_id = ?",
                (event.event_id,),
            )).fetchone()
            delivery_count = (await (await conn.execute(
                "SELECT COUNT(*) FROM notification_deliveries WHERE event_id = ?",
                (event.event_id,),
            )).fetchone())[0]
        assert event_row == ("CRITICAL", "emergency_stop")
        assert delivery_count == 2

    asyncio.run(scenario())


def test_duplicate_event_is_suppressed_without_affecting_other_events():
    async def scenario():
        telegram = FakeAdapter(DeliveryChannel.TELEGRAM)
        slack = FakeAdapter(DeliveryChannel.SLACK)
        manager = NotificationManager(telegram=telegram, slack=slack)
        first = NotificationEvent(
            event_type="trade_opened",
            message="Opened TEST",
            dedupe_key="trade:1",
        )
        second = NotificationEvent(
            event_type="trade_opened",
            message="Opened TEST",
            dedupe_key="trade:1",
        )
        first_results = await manager.publish(first)
        second_results = await manager.publish(second)
        assert all(result.delivered for result in first_results)
        assert all(result.error == "deduplicated" for result in second_results)
        assert len(telegram.events) == 1
        assert len(slack.events) == 1

    asyncio.run(scenario())


def test_slack_failure_does_not_prevent_telegram_delivery():
    async def scenario():
        telegram = FakeAdapter(DeliveryChannel.TELEGRAM)
        slack = FakeAdapter(DeliveryChannel.SLACK, delivered=False)
        manager = NotificationManager(telegram=telegram, slack=slack)
        results = await manager.publish(NotificationEvent(
            event_type="trade_closed",
            message="Closed TEST",
            severity=EventSeverity.IMPORTANT,
        ))
        assert results[0].delivered is True
        assert results[1].delivered is False
        assert telegram.events
        assert slack.events

    asyncio.run(scenario())

import asyncio

from config import TradeSettings
from scheduler import MarketScheduler


class _Response:
    status_code = 200


def _scheduler() -> MarketScheduler:
    return MarketScheduler(
        TradeSettings.defaults(),
        object(),
        object(),
        bot_app=None,
        admin_chat_id=None,
    )


def test_slack_is_noop_when_unconfigured(monkeypatch):
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
    scheduler = _scheduler()
    asyncio.run(scheduler._notify_slack("critical event"))


def test_slack_posts_clean_text_to_configured_webhook(monkeypatch):
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.test/services/secret")
    calls = []

    async def fake_to_thread(func, *args, **kwargs):
        calls.append((func, args, kwargs))
        return _Response()

    monkeypatch.setattr(asyncio, "to_thread", fake_to_thread)
    scheduler = _scheduler()
    asyncio.run(scheduler._notify_slack("**TRADE_OPENED** `TEST`"))

    assert len(calls) == 1
    func, args, kwargs = calls[0]
    assert func.__name__ == "post"
    assert args[0] == "https://hooks.slack.test/services/secret"
    assert kwargs["json"] == {"text": "TRADE_OPENED TEST"}
    assert kwargs["timeout"] == 10

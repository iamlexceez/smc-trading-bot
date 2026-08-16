import asyncio
from types import SimpleNamespace

from analysis.capital_state import AccountCapitalState
from config import TradeSettings
import scheduler as scheduler_module


def test_stale_demo_settings_self_recover_after_verified_broker(monkeypatch):
    async def scenario():
        settings = TradeSettings.defaults()
        settings.trading_mode = "demo"
        settings.auto_trade = False
        settings.is_paused = True
        settings.automation_pause_reason = "LEGACY_STALE"
        engine = object.__new__(scheduler_module.MarketScheduler)
        engine.settings = settings
        engine.risk_manager = SimpleNamespace(settings=settings)

        saved = []

        async def active_objective(_mode):
            return None

        async def save_settings(value, **_kwargs):
            saved.append(value)

        monkeypatch.setattr(scheduler_module.db, "get_active_objective", active_objective)
        monkeypatch.setattr(scheduler_module.db, "save_settings", save_settings)

        changed = await engine._autonomous_demo_recovery({"state": AccountCapitalState.ACCOUNT_VERIFIED})
        assert changed == "auto_trade,is_paused,automation_pause_reason"
        assert settings.auto_trade is True
        assert settings.is_paused is False
        assert settings.automation_pause_reason == ""
        assert len(saved) == 1

    asyncio.run(scenario())


def test_deliberate_emergency_stop_is_never_auto_reversed(monkeypatch):
    async def scenario():
        settings = TradeSettings.defaults()
        settings.trading_mode = "demo"
        settings.auto_trade = False
        settings.is_paused = True
        settings.automation_pause_reason = "EMERGENCY_STOP"
        engine = object.__new__(scheduler_module.MarketScheduler)
        engine.settings = settings
        engine.risk_manager = SimpleNamespace(settings=settings)

        async def active_objective(_mode):
            return None

        async def fail_if_saved(*_args, **_kwargs):
            raise AssertionError("deliberate stop must not be auto-cleared")

        monkeypatch.setattr(scheduler_module.db, "get_active_objective", active_objective)
        monkeypatch.setattr(scheduler_module.db, "save_settings", fail_if_saved)

        changed = await engine._autonomous_demo_recovery({"state": AccountCapitalState.ACCOUNT_VERIFIED})
        assert changed == ""
        assert settings.auto_trade is False
        assert settings.is_paused is True

    asyncio.run(scenario())


def test_reset_waiting_remains_manual(monkeypatch):
    async def scenario():
        settings = TradeSettings.defaults()
        settings.trading_mode = "demo"
        settings.auto_trade = False
        settings.is_paused = True
        settings.automation_pause_reason = ""
        settings.demo_auto_resume_after_reset = False
        engine = object.__new__(scheduler_module.MarketScheduler)
        engine.settings = settings
        engine.risk_manager = SimpleNamespace(settings=settings)

        async def active_objective(_mode):
            return None

        monkeypatch.setattr(scheduler_module.db, "get_active_objective", active_objective)

        changed = await engine._autonomous_demo_recovery({
            "state": AccountCapitalState.ACCOUNT_VERIFIED,
            "reset_detected": True,
        })
        assert changed == ""
        assert settings.auto_trade is False
        assert settings.is_paused is True

    asyncio.run(scenario())

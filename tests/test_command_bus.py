import asyncio
from pathlib import Path

from communication.command_bus import CommandBus, CommandRequest
from storage import db


def test_command_bus_authorizes_and_audits_read_only_commands(tmp_path: Path):
    path = tmp_path / "commands.db"

    async def scenario():
        await db.init_db(str(path))
        calls = []

        async def status(request):
            calls.append((request.platform, request.command, request.arguments))
            return "Trading: PAUSED"

        bus = CommandBus(db_module=db, db_path=str(path), authorizer=lambda request, dangerous: request.user_id == "admin")
        bus.register("status", status)
        response = await bus.dispatch(CommandRequest("slack", "admin", "channel", "/status detailed"))
        assert response.ok
        assert response.text == "Trading: PAUSED"
        assert calls == [("slack", "status", ["detailed"])]

        async with db.aiosqlite.connect(str(path)) as conn:
            row = await (await conn.execute(
                "SELECT platform, command, authorization_result, response_status FROM command_audit"
            )).fetchone()
        assert row == ("slack", "status", "AUTHORIZED", "DELIVERED")

    asyncio.run(scenario())


def test_dangerous_command_requires_confirmation_and_then_serializes():
    async def scenario():
        active = 0
        max_active = 0

        async def pause(request):
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.01)
            active -= 1
            return "Paused"

        bus = CommandBus(authorizer=lambda request, dangerous: True)
        bus.register("pause", pause, dangerous=True)
        pending_one = await bus.dispatch(CommandRequest("telegram", "admin-1", "1", "/pause"))
        pending_two = await bus.dispatch(CommandRequest("slack", "admin-2", "ops", "/pause"))
        assert pending_one.requires_confirmation
        assert pending_two.requires_confirmation
        first, second = await asyncio.gather(
            bus.dispatch(CommandRequest("telegram", "admin-1", "1", f"/pause --confirm {pending_one.confirmation_token}")),
            bus.dispatch(CommandRequest("slack", "admin-2", "ops", f"/pause --confirm {pending_two.confirmation_token}")),
        )
        assert first.ok
        assert second.ok
        assert max_active == 1

        pending_three = await bus.dispatch(CommandRequest("telegram", "admin-3", "1", "/pause"))
        confirmed = await bus.dispatch(CommandRequest("telegram", "admin-3", "1", f"/confirm {pending_three.confirmation_token}"))
        assert confirmed.ok

    asyncio.run(scenario())


def test_unauthorized_command_is_rejected():
    async def scenario():
        async def dangerous(request):
            return "must not run"

        bus = CommandBus(authorizer=lambda request, is_dangerous: request.user_id == "admin")
        bus.register("pause", dangerous, dangerous=True)
        response = await bus.dispatch(CommandRequest("slack", "guest", "ops", "/pause --confirm x"))
        assert response.ok is False
        assert "Not authorized" in response.text

    asyncio.run(scenario())


def test_verified_demo_resume_reenables_auto_trade_and_clears_pause(tmp_path: Path):
    from types import SimpleNamespace
    from communication.control_service import SharedControlService

    async def scenario():
        path = tmp_path / "resume.db"
        await db.init_db(str(path))
        from config import TradeSettings
        settings = TradeSettings.defaults()
        settings.trading_mode = "demo"
        settings.auto_trade = False
        settings.is_paused = True
        settings.automation_pause_reason = "EMERGENCY_STOP"

        class CapitalState:
            async def verify_resume(self):
                return {"resume_verified": True, "state": "ACCOUNT_VERIFIED"}

        service = SharedControlService(settings, SimpleNamespace(capital_state_service=CapitalState()), db_path=str(path))
        result = await service.resume(CommandRequest("telegram", "admin", "1", "/resume"))
        assert "Trading resumed" in result
        assert settings.auto_trade is True
        assert settings.is_paused is False
        assert settings.automation_pause_reason == ""

    asyncio.run(scenario())


def test_blocked_demo_resume_keeps_auto_trade_disabled():
    from types import SimpleNamespace
    from communication.control_service import SharedControlService

    async def scenario():
        from config import TradeSettings
        settings = TradeSettings.defaults()
        settings.trading_mode = "demo"
        settings.auto_trade = False
        settings.is_paused = True
        settings.automation_pause_reason = "ACCOUNT_SAFETY"

        class CapitalState:
            async def verify_resume(self):
                return {"resume_verified": False, "reason": "CAPITAL_EXHAUSTED"}

        service = SharedControlService(settings, SimpleNamespace(capital_state_service=CapitalState()))
        result = await service.resume(CommandRequest("telegram", "admin", "1", "/resume"))
        assert "CAPITAL_EXHAUSTED" in result
        assert settings.auto_trade is False
        assert settings.is_paused is True

    asyncio.run(scenario())


def test_core_command_reports_earned_specialization_not_broker_availability():
    from types import SimpleNamespace
    from communication.control_service import SharedControlService
    from config import TradeSettings

    async def scenario():
        settings = TradeSettings.defaults()
        scheduler = SimpleNamespace(
            market_universe=SimpleNamespace(accepted_records=[SimpleNamespace(symbol="Boom 500 Index")]),
            last_research_governance={
                "instrument_specialization": {
                    "core_symbols": [],
                    "core_selection_explanation": "No instrument currently satisfies the complete Core evidence requirements.",
                    "rankings": [{
                        "instrument": "Boom 500 Index", "role": "RESEARCH", "selected_core": False,
                        "role_reason": "No completed evidence is available.",
                        "specialization": {"adjusted_score": 0.0},
                    }],
                },
            },
        )
        service = SharedControlService(settings, scheduler)
        result = await service.core(CommandRequest("telegram", "admin", "1", "/core"))
        assert "No instrument currently qualifies for CORE" in result
        assert "No completed evidence is available" in result
        assert "BROKER UNIVERSE" not in result

    asyncio.run(scenario())

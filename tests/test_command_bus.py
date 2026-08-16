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

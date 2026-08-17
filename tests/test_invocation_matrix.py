import asyncio
from pathlib import Path

from analysis.invocation_matrix import InvocationTracker
from storage import db


def test_invocation_tracker_proves_called_and_consumed_path():
    tracker = InvocationTracker()
    tracker.mark_invoked(
        "knowledge.context",
        scheduler_entry_point="scheduler.market_scan",
        data_seen=True,
        output_consumed=True,
        persisted=True,
    )
    row = next(item for item in tracker.snapshot() if item["module_name"] == "knowledge.context")
    assert row["imported"] is True
    assert row["called"] is True
    assert row["scheduled"] is True
    assert row["data_seen"] is True
    assert row["output_consumed"] is True
    assert row["persisted"] is True
    assert row["status"] == "COMPLETE"


def test_invocation_matrix_persists_additively(tmp_path: Path):
    async def scenario():
        path = tmp_path / "invocation.db"
        await db.init_db(str(path))
        await db.upsert_module_invocation_evidence({
            "module_name": "knowledge.context", "imported": True, "called": True,
            "scheduled": True, "data_seen": True, "output_consumed": True,
            "persisted": True, "integration_tested": True, "invocation_count": 3,
            "last_scheduler_entry_point": "scheduler.market_scan", "status": "COMPLETE",
            "contract": {"scheduler_entry_point": "scheduler.market_scan"},
        }, db_path=str(path))
        rows = await db.list_module_invocation_evidence(db_path=str(path))
        assert rows[0]["module_name"] == "knowledge.context"
        assert rows[0]["invocation_count"] == 3
        assert rows[0]["snapshot"]["status"] == "COMPLETE"

    asyncio.run(scenario())

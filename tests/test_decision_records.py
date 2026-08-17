import asyncio
from pathlib import Path

from storage import db


def test_structured_decision_record_persists_trade_and_no_trade_fields(tmp_path: Path):
    async def scenario():
        path = tmp_path / "decisions.db"
        await db.init_db(str(path))
        await db.record_decision_record(
            decision_id="decision-1",
            account_mode="demo",
            instrument="Boom 500 Index",
            timeframe="M15",
            regime="TRENDING_BULLISH",
            strategy="liquidity_sweep_continuation",
            strategy_combination=["liquidity_sweep", "displacement"],
            direction="BUY",
            entry=100.0,
            stop_loss=99.0,
            take_profit=102.0,
            rr=2.0,
            risk={"risk_pct": 0.75},
            expected_value=0.4,
            confidence="UNKNOWN",
            evidence={"sample_size": 0, "classification": "INSUFFICIENT"},
            contradictions=["none"],
            portfolio_state={"open_positions": 0},
            execution_state={"state": "PENDING"},
            decision="NO_TRADE",
            reason="Insufficient evidence",
            thesis={"why": "research candidate only"},
            db_path=str(path),
        )
        async with db.aiosqlite.connect(str(path)) as conn:
            conn.row_factory = db.aiosqlite.Row
            row = await (await conn.execute(
                "SELECT instrument, strategy, decision, reason, evidence_json FROM decision_records WHERE decision_id = ?",
                ("decision-1",),
            )).fetchone()
        assert row["instrument"] == "Boom 500 Index"
        assert row["strategy"] == "liquidity_sweep_continuation"
        assert row["decision"] == "NO_TRADE"
        assert row["reason"] == "Insufficient evidence"
        assert '"sample_size": 0' in row["evidence_json"]

    asyncio.run(scenario())

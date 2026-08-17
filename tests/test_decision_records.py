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


def test_feature_and_combination_evidence_upsert(tmp_path: Path):
    async def scenario():
        path = tmp_path / "evidence.db"
        await db.init_db(str(path))
        await db.upsert_feature_importance_evidence(
            account_mode="demo", symbol="Boom 500 Index", strategy_id="s1",
            regime="TRENDING_BULLISH", timeframe="M15", feature_name="displacement",
            importance=0.7, stability=0.8, incremental_value=0.2, sample_size=30,
            evidence_state="SUPPORTED", db_path=str(path),
        )
        await db.record_strategy_combination_evidence(
            account_mode="demo", symbol="Boom 500 Index", regime="TRENDING_BULLISH",
            timeframe="M15", combination_id="a+b", concepts=["a", "b"],
            single_a_expectancy_r=0.2, single_b_expectancy_r=0.1,
            combined_expectancy_r=0.5, incremental_expectancy_r=0.3,
            sample_size=30, state="PROMOTABLE_CANDIDATE", reason="incremental value",
            db_path=str(path),
        )
        async with db.aiosqlite.connect(str(path)) as conn:
            conn.row_factory = db.aiosqlite.Row
            feature = await (await conn.execute(
                "SELECT importance, evidence_state FROM feature_importance_evidence WHERE feature_name = ?",
                ("displacement",),
            )).fetchone()
            combination = await (await conn.execute(
                "SELECT combined_expectancy_r, state FROM strategy_combination_evidence WHERE combination_id = ?",
                ("a+b",),
            )).fetchone()
        assert feature["importance"] == 0.7
        assert feature["evidence_state"] == "SUPPORTED"
        assert combination["combined_expectancy_r"] == 0.5
        assert combination["state"] == "PROMOTABLE_CANDIDATE"

    asyncio.run(scenario())


def test_instrument_specialization_profile_persists_and_reads_back(tmp_path: Path):
    async def scenario():
        path = tmp_path / "instrument_profiles.db"
        await db.init_db(str(path))
        await db.upsert_instrument_specialization_profile(
            account_mode="demo", instrument="Boom 500 Index",
            profile={
                "specialization": {"adjusted_score": 82.5, "raw_score": 91.0, "evidence_factor": 0.91, "sample_size": 200},
                "role": "CORE", "role_reason": "complete evidence",
                "sample_size": 200, "out_of_sample_sample": 100, "forward_sample": 100,
                "metrics": {"expectancy_r": 0.4, "profit_factor": 2.1, "max_drawdown_r": 1.0},
                "best_strategies": ["bos_choch_continuation"], "best_regimes": ["TRENDING"],
                "best_timeframes": ["M15"], "execution_quality": 90.0,
                "account_size_suitability": 85.0,
            }, db_path=str(path),
        )
        profile = await db.get_instrument_specialization_profile("Boom 500 Index", "demo", str(path))
        assert profile["current_status"] == "CORE"
        assert profile["specialization_score"] == 82.5
        assert profile["best_strategies"] == ["bos_choch_continuation"]
        assert profile["profile"]["role"] == "CORE"

    asyncio.run(scenario())

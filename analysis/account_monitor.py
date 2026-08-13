"""Read-only MT5 account monitoring and bot-state reconciliation.

Current balances, margin, orders, prices, and positions come from the executor's
live MT5 snapshot. The local SQLite database is used only to annotate bot-owned
trades and to report discrepancies; this module never places, modifies, or
closes a broker order.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Optional

from storage import db


class AccountReconciliationEngine:
    """Compare broker current state with bot records without intervening."""

    def __init__(self, executor: Any, account_mode: str) -> None:
        self.executor = executor
        self.account_mode = account_mode
        self.last_snapshot_at: Optional[str] = None
        self.last_error: str = ""

    async def snapshot(self, history_days: int = 1) -> dict[str, Any]:
        """Return a fresh broker snapshot or a clearly marked unavailable state."""
        self.last_error = ""
        try:
            getter = getattr(self.executor, "get_live_account_snapshot", None)
            if not getter:
                self.last_error = "Active executor does not support broker-account snapshots"
                return {"current": False, "error": self.last_error}
            data = await getter(history_days=history_days)
        except Exception as exc:
            self.last_error = f"Broker snapshot failed: {type(exc).__name__}: {exc}"
            return {"current": False, "error": self.last_error}
        if not data or not data.get("current"):
            self.last_error = str((data or {}).get("error") or "MT5 returned no current account snapshot")
            return {"current": False, "error": self.last_error}
        self.last_snapshot_at = str(data.get("retrieved_at") or datetime.now(timezone.utc).isoformat())
        return data

    async def reconcile(self, snapshot: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        """Report broker/database mismatches; do not alter either side."""
        snapshot = snapshot or await self.snapshot()
        if not snapshot.get("current"):
            return {"current": False, "error": snapshot.get("error", "Broker state unavailable"), "discrepancies": []}

        broker_positions = {int(position["ticket"]): position for position in snapshot.get("positions", [])}
        local_trades = {int(row["ticket"]): row for row in await db.get_open_trades(self.account_mode) if row.get("ticket") is not None}
        discrepancies: list[dict[str, Any]] = []

        for ticket, position in broker_positions.items():
            local = local_trades.get(ticket)
            if not local:
                discrepancies.append({"type": "untracked_broker_position", "ticket": ticket, "symbol": position.get("symbol"), "detail": "MT5 position has no open bot trade record; monitoring only."})
                continue
            changed = []
            for broker_field, local_field, label in (("sl", "sl_price", "SL"), ("tp", "tp_price", "TP"), ("volume", "lot_size", "volume")):
                broker_value = float(position.get(broker_field) or 0.0)
                local_value = float(local.get(local_field) or 0.0)
                if abs(broker_value - local_value) > 1e-8:
                    changed.append(f"{label} broker={broker_value} local={local_value}")
            if changed:
                discrepancies.append({"type": "broker_modified_position", "ticket": ticket, "symbol": position.get("symbol"), "detail": "; ".join(changed)})

        for ticket, local in local_trades.items():
            if ticket not in broker_positions:
                discrepancies.append({"type": "stale_local_open_trade", "ticket": ticket, "symbol": local.get("symbol"), "detail": "Open bot record is absent from MT5 positions; awaiting/needs history reconciliation."})

        return {
            "current": True,
            "retrieved_at": snapshot.get("retrieved_at"),
            "broker_open_positions": len(broker_positions),
            "bot_open_records": len(local_trades),
            "discrepancies": discrepancies,
        }

    @staticmethod
    def annotate_positions(snapshot: dict[str, Any], bot_records: dict[int, dict], basket_records: dict[int, Optional[dict]]) -> list[dict[str, Any]]:
        """Attach read-only bot provenance to fresh broker positions."""
        result: list[dict[str, Any]] = []
        for raw in snapshot.get("positions", []):
            position = dict(raw)
            ticket = int(position["ticket"])
            local = bot_records.get(ticket)
            basket = basket_records.get(ticket)
            position["ownership"] = "bot" if local else "manual_or_untracked"
            if local:
                position["policy_version"] = local.get("policy_version")
                position["experiment_id"] = local.get("experiment_id")
                position["setup_id"] = local.get("setup_id")
                position["initial_risk"] = float(local.get("initial_risk") or 0.0)
                if position["initial_risk"] > 0:
                    position["r_multiple"] = float(position.get("profit") or 0.0) / position["initial_risk"]
            if basket:
                position["basket_id"] = basket.get("id")
                position["layer_number"] = basket.get("layer_number")
                position["layer_status"] = basket.get("layer_status")
            result.append(position)
        return result


def summarize_history(deals: list[dict[str, Any]]) -> dict[str, Any]:
    """Calculate transparent account-history statistics from broker deal records."""
    realized = [float(item.get("net_profit") or 0.0) for item in deals]
    wins = [value for value in realized if value > 0]
    losses = [value for value in realized if value < 0]
    gross_profit = sum(wins)
    gross_loss = sum(losses)
    net = sum(realized)
    profit_factor = gross_profit / abs(gross_loss) if gross_loss else (float("inf") if gross_profit else 0.0)
    return {
        "trades": len(realized),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": len(wins) / len(realized) * 100 if realized else 0.0,
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "net_profit": net,
        "average_win": gross_profit / len(wins) if wins else 0.0,
        "average_loss": gross_loss / len(losses) if losses else 0.0,
        "profit_factor": profit_factor,
        "largest_win": max(wins, default=0.0),
        "largest_loss": min(losses, default=0.0),
        "expectancy": net / len(realized) if realized else 0.0,
    }


def exposure_summary(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Summarize live MT5 position exposure using broker position fields."""
    positions = snapshot.get("positions", [])
    by_symbol: dict[str, float] = defaultdict(float)
    by_direction: dict[str, float] = defaultdict(float)
    potential_sl = 0.0
    potential_tp = 0.0
    for position in positions:
        volume = float(position.get("volume") or 0.0)
        by_symbol[str(position.get("symbol") or "UNKNOWN")] += volume
        by_direction[str(position.get("direction") or "UNKNOWN")] += volume
        potential_sl += float(position.get("potential_sl") or 0.0)
        potential_tp += float(position.get("potential_tp") or 0.0)
    equity = float((snapshot.get("account") or {}).get("equity") or 0.0)
    used_margin = float((snapshot.get("account") or {}).get("margin") or 0.0)
    return {
        "open_positions": len(positions),
        "by_symbol": dict(sorted(by_symbol.items())),
        "by_direction": dict(sorted(by_direction.items())),
        "potential_sl": potential_sl,
        "potential_tp": potential_tp,
        "used_margin": used_margin,
        "free_margin": float((snapshot.get("account") or {}).get("free_margin") or 0.0),
        "margin_level": float((snapshot.get("account") or {}).get("margin_level") or 0.0),
        "margin_exposure_pct": used_margin / equity * 100 if equity else 0.0,
    }

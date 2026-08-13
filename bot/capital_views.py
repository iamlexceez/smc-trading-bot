"""Plain-text Telegram views for DEMO-only capital reduction."""

from __future__ import annotations

from typing import Optional


def money(value, currency: str = "USD") -> str:
    try:
        return f"{currency} {float(value or 0.0):,.2f}"
    except (TypeError, ValueError):
        return f"{currency} 0.00"


def capital_test_view(*, account: Optional[dict], session: Optional[dict], target: Optional[float], tolerance: float) -> str:
    currency = str((account or {}).get("currency") or "USD")
    if session:
        status = str(session.get("status") or "unknown").upper()
        title = "🎯 CAPITAL TEST READY" if session.get("capital_test_active") else "🔥 CAPITAL REDUCTION MODE"
        lines = [
            title,
            f"Session: #{session.get('id')} | Status: {status}",
            "Purpose: deliberately reduce actual DEMO equity to the selected target; this activity is excluded from strategy learning.",
            "",
            f"Initial equity: {money(session.get('initial_equity'), currency)}",
            f"Target equity: {money(session.get('target_equity'), currency)} ± {money(session.get('tolerance'), currency)}",
            f"Current equity: {money(session.get('current_equity'), currency)}",
            f"Current balance: {money(session.get('current_balance'), currency)}",
            f"Remaining: {money(session.get('remaining'), currency)} | Progress: {float(session.get('progress_pct') or 0.0):.2f}%",
            f"Capital-test mode: {'ACTIVE' if session.get('capital_test_active') else 'PENDING'}",
        ]
        if session.get("error_reason"):
            lines.append(f"Status detail: {session['error_reason']}")
        if session.get("broker_error"):
            lines.append(f"Broker state: unavailable — {session['broker_error']}")
        return "\n".join(lines)

    lines = [
        "🎯 CAPITAL TEST — DEMO-ONLY",
        "No capital-reduction session is active.",
        f"Configured target: {money(target, currency) if target is not None else 'not set'} ± {money(tolerance, currency)}",
    ]
    if account:
        lines.extend([
            f"Actual MT5 broker mode: {str(account.get('broker_account_mode') or 'unknown').upper()}",
            f"Current actual equity: {money(account.get('equity'), currency)}",
            f"Current actual balance: {money(account.get('balance'), currency)}",
        ])
    lines.append("Use /capital_target <equity> [tolerance], then /capital_start for an explicit confirmation prompt.")
    return "\n".join(lines)


def capital_actions_view(session: Optional[dict], currency: str = "USD") -> str:
    if not session:
        return "CAPITAL REDUCTION ACTIVITY\n\nNo session exists."
    lines = [f"CAPITAL REDUCTION ACTIVITY — SESSION #{session.get('id')}"]
    actions = session.get("actions") or []
    if not actions:
        lines.append("No reduction activity has been recorded.")
        return "\n".join(lines)
    for action in actions[:20]:
        details = action.get("details") or {}
        ticket = f" ticket #{action['ticket']}" if action.get("ticket") else ""
        lines.append(
            f"- {action.get('created_at')} | {action.get('action')} | {action.get('status')}{ticket}"
            f" | {action.get('symbol') or ''} {action.get('direction') or ''} vol {action.get('volume') or ''}"
            f" | equity {money(action.get('equity_before'), currency)} → {money(action.get('equity_after'), currency)}"
            f" | {details.get('reason') or ''}"
        )
    return "\n".join(lines)

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
        stored_status = str(session.get("status") or "unknown").upper()
        runtime_status = str(session.get("runtime_state") or stored_status).upper()
        title = "🔥 CAPITAL REDUCTION COMPLETE" if stored_status == "COMPLETED" else ("⚠️ CAPITAL REDUCTION BLOCKED" if runtime_status == "BLOCKED" else ("🎯 CAPITAL TEST READY" if session.get("capital_test_active") else "🔥 CAPITAL REDUCTION"))
        display_state = "ACTIVE" if stored_status == "ACTIVE" and runtime_status == "BLOCKED" else runtime_status
        effective_tolerance = session.get("effective_tolerance", session.get("tolerance"))
        planning = session.get("last_planning") or {}
        selected = planning.get("best_candidate") or {}
        lines = [
            title,
            f"Session: #{session.get('id')} | State: {display_state} | Session status: {stored_status}",
            "Purpose: deliberately reduce actual DEMO equity through broker-valid sequential actions; this activity is excluded from strategy learning.",
            "",
            f"Target: {money(session.get('target_equity'), currency)} | Current: {money(session.get('current_equity'), currency)}",
            f"Remaining: {money(session.get('remaining'), currency)} | Finish tolerance: {money(effective_tolerance, currency)}",
            f"Mode: {str((session.get('metadata') or {}).get('mode') or 'AGGRESSIVE_TAPERED').upper()} | Valid broker actions: {planning.get('valid_candidate_count', 0)} | Active positions: {session.get('active_positions', 'UNKNOWN')}",
            f"Target proximity: {float(planning.get('proximity_ratio') or 0.0) * 100:.1f}% remaining | Action aggression: {float(planning.get('aggression_factor') or 0.0) * 100:.1f}% | Permitted overshoot: {money(planning.get('tapered_overshoot_tolerance'), currency)}",
            f"Selected: {selected.get('symbol') or 'none'} | Volume: {selected.get('volume') if selected.get('volume') is not None else 'n/a'} | Expected reduction: {money(selected.get('expected_loss'), currency) if selected.get('expected_loss') is not None else 'n/a'}",
            f"Reason: {selected.get('reason') or session.get('error_reason') or 'Searching current broker-valid candidates'}",
            f"Next action: {'RETRY BROKER-VALID ACTION' if runtime_status == 'BLOCKED' and stored_status == 'ACTIVE' else display_state} | Progress: {float(session.get('progress_pct') or 0.0):.2f}%",
        ]
        if session.get("error_reason") and not selected:
            lines.extend(["", f"Detail: {session['error_reason']}"])
        if session.get("broker_error"):
            lines.append(f"Broker state: unavailable — {session['broker_error']}")
        return "\n".join(lines)

    lines = [
        "🎯 CAPITAL TEST — DEMO-ONLY",
        "No capital-reduction session is active.",
        f"Configured target: {money(target, currency) if target is not None else 'not set'} ± {money(tolerance, currency)} absolute tolerance",
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



def demo_session_report_view(session: Optional[dict], symbols: list[dict], currency: str = "USD") -> str:
    if not session:
        return "DEMO SESSION REPORT\n\nNo broker-verified DEMO session has been recorded yet."
    factor = session.get("profit_factor")
    pf = "N/A" if factor is None else ("∞" if factor == float("inf") else f"{float(factor):.2f}")
    expectancy = session.get("expectancy_r")
    best = symbols[0]["symbol"] if symbols else "No completed normal strategy trades"
    worst = symbols[-1]["symbol"] if symbols else "No completed normal strategy trades"
    title = "🚨 DEMO SESSION ENDED" if session.get("status") == "exhausted" else "💰 DEMO SESSION REPORT"
    return "\n".join([
        title,
        f"Session: #{session.get('id')} | Status: {str(session.get('status') or 'unknown').upper()}",
        f"Started: {session.get('started_at')} | Ended: {session.get('ended_at') or 'ACTIVE'}",
        f"Starting capital: {money(session.get('start_balance'), currency)} | Starting equity: {money(session.get('start_equity'), currency)}",
        f"Ending capital: {money(session.get('end_balance'), currency)} | Ending equity: {money(session.get('end_equity'), currency)}",
        f"Peak equity: {money(session.get('max_equity'), currency)} | Minimum equity: {money(session.get('min_equity'), currency)}",
        f"Maximum drawdown: {float(session.get('max_drawdown_pct') or 0.0):.2f}%",
        f"Normal strategy trades: {int(session.get('strategy_trades') or 0)} | Wins: {int(session.get('wins') or 0)} | Losses: {int(session.get('losses') or 0)}",
        f"Profit factor: {pf} | Expectancy: {f'{float(expectancy):.2f}R' if expectancy is not None else 'N/A'}",
        f"Best market: {best} | Worst market: {worst}",
        f"Policy versions: {', '.join(session.get('policy_versions') or []) or 'No completed policy-attributed trades'}",
        f"Capital reduction activity: {'YES' if session.get('capital_reduction_activity') else 'NO'} | Capital test: {'ACTIVE' if session.get('capital_test_active') else 'NO'}",
        f"End reason: {session.get('exhaustion_reason') or 'Not exhausted'}",
        "Intentional capital-reduction trades are excluded from normal strategy statistics.",
    ])

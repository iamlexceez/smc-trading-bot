"""Concise Telegram controls for the Deriv DEMO research system."""

from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup


def main_menu() -> InlineKeyboardMarkup:
    """Primary monitoring and research lifecycle controls."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Dashboard", callback_data="dashboard"), InlineKeyboardButton("💰 Account", callback_data="account")],
        [InlineKeyboardButton("📍 Positions", callback_data="positions"), InlineKeyboardButton("📋 Orders", callback_data="orders")],
        [InlineKeyboardButton("⚠️ Exposure", callback_data="exposure"), InlineKeyboardButton("🩺 Account Health", callback_data="health")],
        [InlineKeyboardButton("📜 History", callback_data="history"), InlineKeyboardButton("📈 Performance", callback_data="performance")],
        [InlineKeyboardButton("💹 Markets", callback_data="markets"), InlineKeyboardButton("🧠 Learning", callback_data="learning")],
        [InlineKeyboardButton("🧪 Experiments", callback_data="experiments"), InlineKeyboardButton("🎯 Capital Test", callback_data="capital_test")],
        [InlineKeyboardButton("🏆 Champion", callback_data="champion"), InlineKeyboardButton("⚔️ Challengers", callback_data="challengers")],
        [InlineKeyboardButton("⚙️ Settings / DEMO-LIVE", callback_data="settings"), InlineKeyboardButton("🚨 Emergency Stop", callback_data="emergency")],
    ])


def settings_menu() -> InlineKeyboardMarkup:
    """Only operational controls; trading policy is discovered by research."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Mode: DEMO / LIVE", callback_data="set_mode"), InlineKeyboardButton("Autonomy", callback_data="set_autotrade")],
        [InlineKeyboardButton("Chart Activity Alerts", callback_data="activity_menu"), InlineKeyboardButton("Refresh Markets", callback_data="markets")],
        [InlineKeyboardButton("⬅️ Dashboard", callback_data="dashboard")],
    ])


def autotrade_menu(current: bool) -> InlineKeyboardMarkup:
    status = "ON" if current else "OFF"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"Autonomous Execution: {status}", callback_data="toggle_autotrade")],
        [InlineKeyboardButton("⬅️ Settings", callback_data="settings")],
    ])


def activity_menu(current: str, include_rejections: bool) -> InlineKeyboardMarkup:
    """Choose how much closed-candle research activity reaches Telegram."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{'✅' if current == 'detailed' else '⬜'} Detailed", callback_data="activity_detailed")],
        [InlineKeyboardButton(f"{'✅' if current == 'essential' else '⬜'} Essential only", callback_data="activity_essential")],
        [InlineKeyboardButton(f"{'✅' if current == 'off' else '⬜'} Off", callback_data="activity_off")],
        [InlineKeyboardButton(f"Rejected candidates: {'ON' if include_rejections else 'OFF'}", callback_data="activity_rejections")],
        [InlineKeyboardButton("⬅️ Settings", callback_data="settings")],
    ])


def mode_menu(current_mode: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{'✅' if current_mode == 'demo' else '⬜'} DEMO", callback_data="mode_demo")],
        [InlineKeyboardButton(f"{'✅' if current_mode == 'live' else '⬜'} LIVE — confirmation required", callback_data="mode_live")],
        [InlineKeyboardButton("⬅️ Settings", callback_data="settings")],
    ])


def capital_test_menu(has_active_session: bool = False) -> InlineKeyboardMarkup:
    """Operational controls for the isolated DEMO capital-reduction workflow."""
    rows = [
        [InlineKeyboardButton("Set Target (use /capital_target)", callback_data="capital_set_target")],
        [InlineKeyboardButton("Start Reduction", callback_data="capital_start"), InlineKeyboardButton("View Progress", callback_data="capital_status")],
        [InlineKeyboardButton("Pause", callback_data="capital_pause"), InlineKeyboardButton("Resume", callback_data="capital_resume")],
        [InlineKeyboardButton("Cancel", callback_data="capital_cancel"), InlineKeyboardButton("View Activity", callback_data="capital_activity")],
        [InlineKeyboardButton("Enter Capital-Test Mode", callback_data="capital_enter_test")],
        [InlineKeyboardButton("Account", callback_data="account"), InlineKeyboardButton("⬅️ Dashboard", callback_data="dashboard")],
    ]
    return InlineKeyboardMarkup(rows)


def confirm_keyboard(action: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Confirm", callback_data=f"confirm_{action}"), InlineKeyboardButton("❌ Cancel", callback_data="cancel")]
    ])


def analysis_menu(symbols: list[str]) -> InlineKeyboardMarkup:
    """Broker-verified Deriv market selection for on-demand analysis."""
    rows = [[InlineKeyboardButton(f"📊 {symbol}", callback_data=f"analyze_{symbol}")] for symbol in symbols]
    rows.append([InlineKeyboardButton("⬅️ Dashboard", callback_data="dashboard")])
    return InlineKeyboardMarkup(rows)

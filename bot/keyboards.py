"""Concise Telegram controls for the Deriv autonomous learning system."""

from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup


def main_menu() -> InlineKeyboardMarkup:
    """Primary monitoring, reporting, control, and emergency menu."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Dashboard", callback_data="dashboard"), InlineKeyboardButton("💹 Markets", callback_data="markets")],
        [InlineKeyboardButton("📈 Active Trades", callback_data="positions"), InlineKeyboardButton("🧠 Learning", callback_data="learning")],
        [InlineKeyboardButton("📊 Performance", callback_data="performance"), InlineKeyboardButton("⚙️ Settings", callback_data="settings")],
        [InlineKeyboardButton("🧪 Backtest", callback_data="backtest_help"), InlineKeyboardButton("🛡 Safety", callback_data="safety")],
        [InlineKeyboardButton("🔄 Model", callback_data="model"), InlineKeyboardButton("🚨 Emergency Stop", callback_data="emergency")],
    ])


def settings_menu() -> InlineKeyboardMarkup:
    """Only controls that remain relevant to the autonomous Deriv system."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Mode: DEMO / LIVE", callback_data="set_mode"), InlineKeyboardButton("Autonomy", callback_data="set_autotrade")],
        [InlineKeyboardButton("Safety Limits", callback_data="safety"), InlineKeyboardButton("Risk & Layers", callback_data="baskets")],
        [InlineKeyboardButton("Refresh Markets", callback_data="markets")],
        [InlineKeyboardButton("⬅️ Dashboard", callback_data="dashboard")],
    ])


def autotrade_menu(current: bool) -> InlineKeyboardMarkup:
    status = "ON" if current else "OFF"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"Autonomous Execution: {status}", callback_data="toggle_autotrade")],
        [InlineKeyboardButton("⬅️ Settings", callback_data="settings")],
    ])


def mode_menu(current_mode: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{'✅' if current_mode == 'demo' else '⬜'} DEMO", callback_data="mode_demo")],
        [InlineKeyboardButton(f"{'✅' if current_mode == 'live' else '⬜'} LIVE — confirmation required", callback_data="mode_live")],
        [InlineKeyboardButton("⬅️ Settings", callback_data="settings")],
    ])


def confirm_keyboard(action: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Confirm", callback_data=f"confirm_{action}"), InlineKeyboardButton("❌ Cancel", callback_data="cancel")]
    ])


def analysis_menu(symbols: list[str]) -> InlineKeyboardMarkup:
    """Broker-verified Deriv market selection for on-demand analysis."""
    rows = [[InlineKeyboardButton(f"📊 {symbol}", callback_data=f"analyze_{symbol}")] for symbol in symbols]
    rows.append([InlineKeyboardButton("⬅️ Dashboard", callback_data="dashboard")])
    return InlineKeyboardMarkup(rows)

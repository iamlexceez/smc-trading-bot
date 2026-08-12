"""
Inline keyboards for Telegram bot settings control.
All non-secret settings are adjustable via inline buttons.
"""

from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup


def main_menu() -> InlineKeyboardMarkup:
    """Main menu keyboard."""
    keyboard = [
        [InlineKeyboardButton("📊 Scan Markets", callback_data="scan"),
         InlineKeyboardButton("📈 Positions", callback_data="positions")],
        [InlineKeyboardButton("⚙️ Settings", callback_data="settings"),
         InlineKeyboardButton("📋 Trade History", callback_data="history")],
        [InlineKeyboardButton("💰 Account Info", callback_data="account"),
         InlineKeyboardButton("🛠 Debug MT5", callback_data="debug_mt5")],
        [InlineKeyboardButton("⏸ Pause" , callback_data="pause"),
         InlineKeyboardButton("▶️ Resume", callback_data="resume")],
        [InlineKeyboardButton("🛑 Close All", callback_data="close_all")],
    ]
    return InlineKeyboardMarkup(keyboard)


def settings_menu() -> InlineKeyboardMarkup:
    """Settings adjustment menu."""
    keyboard = [
        [InlineKeyboardButton("Risk %", callback_data="set_risk"),
         InlineKeyboardButton("Min RR", callback_data="set_rr")],
        [InlineKeyboardButton("Score Threshold", callback_data="set_score"),
         InlineKeyboardButton("Max Spread", callback_data="set_spread")],
        [InlineKeyboardButton("🔥 Aggressive", callback_data="toggle_aggressive"),
         InlineKeyboardButton("⏱ Scalping", callback_data="toggle_scalping")],
        [InlineKeyboardButton("🎯 Target", callback_data="set_target"),
         InlineKeyboardButton("💰 Virtual Bal", callback_data="set_virtual_balance")],
        [InlineKeyboardButton("🏆 Expert Mode", callback_data="confirm_expert_mode"),
         InlineKeyboardButton("🎯 Index Focus", callback_data="toggle_index_focus")],
        [InlineKeyboardButton("Symbols", callback_data="set_symbols"),
         InlineKeyboardButton("Timeframes", callback_data="set_timeframes")],
        [InlineKeyboardButton("Mode: Demo/Live", callback_data="set_mode"),
         InlineKeyboardButton("Auto-Trade", callback_data="set_autotrade")],
        [InlineKeyboardButton("⬅️ Back", callback_data="main")],
    ]
    return InlineKeyboardMarkup(keyboard)


def autotrade_menu(current: bool) -> InlineKeyboardMarkup:
    """Toggle auto-trade."""
    status = "ON ✅" if current else "OFF ❌"
    keyboard = [
        [InlineKeyboardButton(f"Auto-Trade: {status}", callback_data="toggle_autotrade")],
        [InlineKeyboardButton("⬅️ Back", callback_data="settings")],
    ]
    return InlineKeyboardMarkup(keyboard)


def mode_menu(current_mode: str) -> InlineKeyboardMarkup:
    """Switch between demo and live mode."""
    keyboard = [
        [InlineKeyboardButton(
            f"{'✅' if current_mode == 'demo' else '⬜'} Demo Mode",
            callback_data="mode_demo"
        )],
        [InlineKeyboardButton(
            f"{'✅' if current_mode == 'live' else '⬜'} Live Mode",
            callback_data="mode_live"
        )],
        [InlineKeyboardButton("⬅️ Back", callback_data="settings")],
    ]
    return InlineKeyboardMarkup(keyboard)


def confirm_keyboard(action: str) -> InlineKeyboardMarkup:
    """Confirmation keyboard for dangerous actions."""
    keyboard = [
        [
            InlineKeyboardButton("✅ Confirm", callback_data=f"confirm_{action}"),
            InlineKeyboardButton("❌ Cancel", callback_data="cancel"),
        ]
    ]
    return InlineKeyboardMarkup(keyboard)


def symbol_select_keyboard(symbols: list[str], active: list[str]) -> InlineKeyboardMarkup:
    """Multi-select keyboard for symbols."""
    keyboard = []
    for sym in symbols:
        active_sym = "✅" if sym in active else "⬜"
        keyboard.append([InlineKeyboardButton(f"{active_sym} {sym}", callback_data=f"sym_{sym}")])
    keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data="settings")])
    return InlineKeyboardMarkup(keyboard)


def timeframe_select_keyboard(timeframes: list[str], active: list[str]) -> InlineKeyboardMarkup:
    """Multi-select keyboard for timeframes."""
    keyboard = []
    row = []
    for tf in timeframes:
        active_tf = "✅" if tf in active else "⬜"
        row.append(InlineKeyboardButton(f"{active_tf} {tf}", callback_data=f"tf_{tf}"))
        if len(row) == 3:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data="settings")])
    return InlineKeyboardMarkup(keyboard)


def analysis_menu(symbols: list[str]) -> InlineKeyboardMarkup:
    """Symbol selection for analysis."""
    keyboard = []
    for sym in symbols:
        keyboard.append([InlineKeyboardButton(f"📊 {sym}", callback_data=f"analyze_{sym}")])
    keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data="main")])
    return InlineKeyboardMarkup(keyboard)

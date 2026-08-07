"""
Trading session filtering — only trade during high-liquidity sessions.

Sessions (UTC):
- Tokyo:    00:00 - 09:00
- London:   08:00 - 17:00
- New York: 13:00 - 22:00
- London/NY Overlap: 13:00 - 17:00 (highest liquidity)

The bot only opens new trades during enabled sessions.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from dataclasses import dataclass


class Session(Enum):
    TOKYO = "tokyo"
    LONDON = "london"
    NEW_YORK = "new_york"
    LONDON_NY_OVERLAP = "london_ny_overlap"


SESSION_HOURS = {
    Session.TOKYO: (0, 9),
    Session.LONDON: (8, 17),
    Session.NEW_YORK: (13, 22),
    Session.LONDON_NY_OVERLAP: (13, 17),
}


@dataclass
class SessionInfo:
    current_session: Session | None
    is_trading_time: bool
    next_session: Session | None
    next_session_time: str
    reason: str


def get_current_session(utc_time: datetime | None = None) -> Session | None:
    """Get the current active trading session."""
    if utc_time is None:
        utc_time = datetime.now(timezone.utc)
    hour = utc_time.hour

    # Check overlap first (highest priority)
    overlap_start, overlap_end = SESSION_HOURS[Session.LONDON_NY_OVERLAP]
    if overlap_start <= hour < overlap_end:
        return Session.LONDON_NY_OVERLAP

    # Check other sessions
    for session in [Session.TOKYO, Session.LONDON, Session.NEW_YORK]:
        start, end = SESSION_HOURS[session]
        if start <= hour < end:
            return session

    return None


def check_trading_session(
    enabled_sessions: list[str],
    utc_time: datetime | None = None,
) -> SessionInfo:
    """
    Check if current time is within an enabled trading session.

    Args:
        enabled_sessions: List of session names to allow trading in.
                         Options: "tokyo", "london", "new_york", "overlap"
        utc_time: Override current time (for testing)

    Returns:
        SessionInfo with current status and next session details.
    """
    if utc_time is None:
        utc_time = datetime.now(timezone.utc)

    current = get_current_session(utc_time)

    # Map session names
    session_map = {
        "tokyo": Session.TOKYO,
        "london": Session.LONDON,
        "new_york": Session.NEW_YORK,
        "overlap": Session.LONDON_NY_OVERLAP,
        "all": None,  # Special case: all sessions
    }

    # "all" means all sessions are enabled
    if "all" in enabled_sessions or not enabled_sessions:
        enabled = list(Session)
    else:
        enabled = [session_map[s] for s in enabled_sessions if s in session_map and session_map[s]]

    # Check if current session is enabled
    if current and current in enabled:
        return SessionInfo(
            current_session=current,
            is_trading_time=True,
            next_session=None,
            next_session_time="",
            reason=f"Trading in {current.value} session",
        )

    # Also check if current is London or NY and overlap is enabled
    if Session.LONDON_NY_OVERLAP in enabled:
        overlap_start, overlap_end = SESSION_HOURS[Session.LONDON_NY_OVERLAP]
        if overlap_start <= utc_time.hour < overlap_end:
            return SessionInfo(
                current_session=Session.LONDON_NY_OVERLAP,
                is_trading_time=True,
                next_session=None,
                next_session_time="",
                reason="Trading in London/NY overlap",
            )

    # Not in a trading session — find next one
    next_session = None
    next_time = ""
    hour = utc_time.hour

    # Find next enabled session
    for session in [Session.LONDON, Session.NEW_YORK, Session.LONDON_NY_OVERLAP, Session.TOKYO]:
        if session in enabled:
            start, end = SESSION_HOURS[session]
            if hour < start:
                next_session = session
                next_time = f"{start:02d}:00 UTC"
                break

    if not next_session:
        # Next session is tomorrow
        for session in [Session.TOKYO, Session.LONDON, Session.NEW_YORK]:
            if session in enabled:
                start, _ = SESSION_HOURS[session]
                next_session = session
                next_time = f"Tomorrow {start:02d}:00 UTC"
                break

    current_name = current.value if current else "No active session"
    return SessionInfo(
        current_session=current,
        is_trading_time=False,
        next_session=next_session,
        next_session_time=next_time,
        reason=f"Outside trading sessions (current: {current_name}). Next: {next_time}",
    )


def format_session_status(enabled_sessions: list[str]) -> str:
    """Format session info for Telegram display."""
    info = check_trading_session(enabled_sessions)
    status = "🟢 ACTIVE" if info.is_trading_time else "🔴 INACTIVE"

    lines = [
        f"🕐 **Trading Sessions** — {status}",
        f"",
        f"Current: {info.current_session.value if info.current_session else 'Closed'}",
        f"Reason: {info.reason}",
        f"",
        f"**Enabled Sessions:**",
    ]

    session_names = {
        "tokyo": "🇯🇵 Tokyo (00:00-09:00 UTC)",
        "london": "🇬🇧 London (08:00-17:00 UTC)",
        "new_york": "🇺🇸 New York (13:00-22:00 UTC)",
        "overlap": "🔀 London/NY Overlap (13:00-17:00 UTC)",
        "all": "🌍 All sessions (24/5)",
    }

    if "all" in enabled_sessions or not enabled_sessions:
        lines.append(session_names["all"])
    else:
        for s in enabled_sessions:
            lines.append(session_names.get(s, s))

    if not info.is_trading_time and info.next_session:
        lines.append(f"\n⏰ Next: {info.next_session.value} at {info.next_session_time}")

    return "\n".join(lines)

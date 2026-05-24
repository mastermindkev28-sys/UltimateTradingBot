"""
news_filter.py — Economic Calendar / News Blackout Filter
==========================================================
Fetches today's high-impact economic events from the Forex Factory
public JSON feed (no API key required) and blocks trading 45 minutes
before and after each qualifying event.

Additionally supports manual event injection (e.g. Fed speaker appearances
not always captured by the automated feed).

If the calendar fetch fails the filter defaults to ALLOW (fail-open) but
logs a prominent warning.  Operators can set STRICT_NEWS_FILTER=true in
.env to fail-safe (block all trading when calendar unavailable).
"""

import logging
import os
from datetime import datetime, timedelta
from typing import List, Optional

import aiohttp
import pytz

import config

logger = logging.getLogger(__name__)

STRICT_FILTER = os.getenv("STRICT_NEWS_FILTER", "false").lower() in ("true", "1", "yes")


# ═══════════════════════════════════════════════════════════════════════════════
# DATA STRUCTURE
# ═══════════════════════════════════════════════════════════════════════════════
class EconomicEvent:
    __slots__ = ("title", "country", "impact", "event_time")

    def __init__(
        self,
        title:      str,
        country:    str,
        impact:     str,        # "High" | "Medium" | "Low" | "Holiday"
        event_time: datetime,   # timezone-aware (ET)
    ) -> None:
        self.title      = title
        self.country    = country
        self.impact     = impact
        self.event_time = event_time

    def __repr__(self) -> str:
        return (
            f"EconomicEvent({self.impact} | {self.country} | "
            f"{self.title} @ {self.event_time.strftime('%H:%M %Z')})"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# NEWS FILTER
# ═══════════════════════════════════════════════════════════════════════════════
class NewsFilter:
    """
    Tracks high-impact news events and enforces blackout windows.

    Usage:
        filter = NewsFilter()
        await filter.fetch_calendar()     # call once at session start
        blocked = filter.is_blackout_window(datetime.now(tz))
    """

    def __init__(self) -> None:
        self.tz:              pytz.BaseTzInfo    = config.TIMEZONE
        self._events:         List[EconomicEvent] = []
        self._last_fetch:     Optional[datetime]  = None
        self._fetch_failed:   bool                = False
        self._manual_events:  List[EconomicEvent] = []  # operator-injected events

        # Parse any manually configured events from env
        self._load_manual_events()

    # ── manual events (from .env MANUAL_NEWS_TIMES) ─────────────────────────
    def _load_manual_events(self) -> None:
        """
        Read manual event times from the environment.
        Format: "HH:MM,HH:MM,..." in ET — treated as High-impact.
        Example: MANUAL_NEWS_TIMES="08:30,14:00"
        """
        raw = os.getenv("MANUAL_NEWS_TIMES", "")
        if not raw.strip():
            return
        today = datetime.now(self.tz).date()
        for part in raw.split(","):
            part = part.strip()
            try:
                h, m = map(int, part.split(":"))
                naive_dt = datetime(today.year, today.month, today.day, h, m)
                aware_dt = self.tz.localize(naive_dt)
                self._manual_events.append(
                    EconomicEvent(
                        title="Manual Event",
                        country="US",
                        impact="High",
                        event_time=aware_dt,
                    )
                )
                logger.info("Manual news event loaded: %s ET", part)
            except Exception as exc:
                logger.warning("Could not parse manual event time '%s': %s", part, exc)

    # ── calendar fetch ──────────────────────────────────────────────────────
    async def fetch_calendar(self) -> None:
        """
        Fetch the Forex Factory weekly calendar JSON and filter for today's
        high-impact US events (Gold reacts mainly to USD data).
        """
        try:
            timeout = aiohttp.ClientTimeout(total=config.NEWS_CALENDAR_TIMEOUT)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(config.NEWS_CALENDAR_URL) as resp:
                    if resp.status != 200:
                        raise RuntimeError(f"HTTP {resp.status}")
                    raw = await resp.json(content_type=None)

            today_str = datetime.now(self.tz).strftime("%m-%d-%Y")
            events: List[EconomicEvent] = []

            for item in raw:
                # Only US events (Gold / USD sensitive)
                if item.get("country", "").upper() not in ("USD", "US"):
                    continue

                impact = item.get("impact", "").lower()
                if impact not in ("high",):
                    # Optionally include "medium" — kept conservative
                    continue

                title = item.get("title", "")
                date_str = item.get("date", "")

                # Only today's events
                if date_str != today_str:
                    continue

                time_str = item.get("time", "")
                event_dt = self._parse_event_time(date_str, time_str)
                if event_dt is None:
                    continue

                # Match against known high-impact keywords
                if not self._is_high_impact(title):
                    continue

                events.append(
                    EconomicEvent(
                        title=title,
                        country=item.get("country", "US"),
                        impact="High",
                        event_time=event_dt,
                    )
                )

            self._events = events + self._manual_events
            self._last_fetch = datetime.now(self.tz)
            self._fetch_failed = False

            if self._events:
                logger.warning(
                    "📰 %d HIGH-IMPACT news event(s) today: %s",
                    len(self._events),
                    [str(e) for e in self._events],
                )
            else:
                logger.info("📰 No high-impact news events found for today.")

        except Exception as exc:
            self._fetch_failed = True
            self._events = self._manual_events  # fall back to manual only
            logger.error(
                "Failed to fetch news calendar: %s — "
                "strict_filter=%s (trading %s)",
                exc,
                STRICT_FILTER,
                "BLOCKED" if STRICT_FILTER else "ALLOWED with caution",
            )

    # ── blackout check ──────────────────────────────────────────────────────
    def is_blackout_window(self, now: datetime) -> bool:
        """
        Return True if trading should be blocked due to a nearby news event.

        If the calendar fetch failed and STRICT_NEWS_FILTER is True,
        this always returns True (block all trading when uncertain).
        """
        if self._fetch_failed and STRICT_FILTER:
            logger.warning("News calendar unavailable — trading blocked (strict mode)")
            return True

        now_et = now.astimezone(self.tz)
        before = timedelta(minutes=config.NEWS_BLACKOUT_BEFORE_MIN)
        after  = timedelta(minutes=config.NEWS_BLACKOUT_AFTER_MIN)

        for event in self._events:
            window_start = event.event_time - before
            window_end   = event.event_time + after

            if window_start <= now_et <= window_end:
                logger.info(
                    "📰 Blackout window: '%s' @ %s (window %s – %s)",
                    event.title,
                    event.event_time.strftime("%H:%M"),
                    window_start.strftime("%H:%M"),
                    window_end.strftime("%H:%M"),
                )
                return True

        return False

    def time_to_next_event(self, now: datetime) -> Optional[timedelta]:
        """Return the timedelta until the next blackout window begins, or None."""
        now_et = now.astimezone(self.tz)
        before = timedelta(minutes=config.NEWS_BLACKOUT_BEFORE_MIN)
        upcoming = []
        for event in self._events:
            window_start = event.event_time - before
            if window_start > now_et:
                upcoming.append(window_start - now_et)
        return min(upcoming) if upcoming else None

    def get_today_events(self) -> List[dict]:
        """Return a serialisable list of today's events for reporting."""
        return [
            {
                "title":  e.title,
                "impact": e.impact,
                "time":   e.event_time.strftime("%H:%M %Z"),
            }
            for e in self._events
        ]

    # ── helpers ─────────────────────────────────────────────────────────────
    def _parse_event_time(self, date_str: str, time_str: str) -> Optional[datetime]:
        """
        Parse Forex Factory date + time strings into a timezone-aware datetime.
        FF format: date='01-15-2025', time='8:30am'  (ET)
        """
        if not time_str or time_str.lower() in ("tentative", "all day", ""):
            # Events without specific times — treat conservatively
            # Use a midday placeholder so the blackout doesn't kill the full day
            time_str = "12:00pm"

        try:
            combined = f"{date_str} {time_str}"
            naive = datetime.strptime(combined, "%m-%d-%Y %I:%M%p")
            return self.tz.localize(naive)
        except ValueError:
            try:
                naive = datetime.strptime(combined, "%m-%d-%Y %I%p")
                return self.tz.localize(naive)
            except Exception:
                logger.debug("Cannot parse event time: date=%s time=%s", date_str, time_str)
                return None

    @staticmethod
    def _is_high_impact(title: str) -> bool:
        """Check if the event title contains any high-impact keyword."""
        title_upper = title.upper()
        for keyword in config.HIGH_IMPACT_KEYWORDS:
            if keyword.upper() in title_upper:
                return True
        return False

    # ── operator utilities ──────────────────────────────────────────────────
    def add_manual_event(self, event_time_et: str, title: str = "Manual Event") -> None:
        """
        Programmatically inject a news blackout (e.g. from a Telegram command).
        event_time_et: "HH:MM" string in ET.
        """
        try:
            today = datetime.now(self.tz).date()
            h, m = map(int, event_time_et.split(":"))
            naive = datetime(today.year, today.month, today.day, h, m)
            aware = self.tz.localize(naive)
            evt = EconomicEvent(
                title=title,
                country="US",
                impact="High",
                event_time=aware,
            )
            self._events.append(evt)
            logger.info("Manual news event added: %s @ %s ET", title, event_time_et)
        except Exception as exc:
            logger.error("add_manual_event failed: %s", exc)

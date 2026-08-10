"""Recurring task scheduling (SPEC §4.4).

The chosen model: **one open instance at a time, the next generated on
completion rather than on schedule.** A bin day left undone for three weeks is
one task due this week, not three overdue ones — with the completed instances
kept as history so "who did the bins last month" stays answerable.

The catch-up rule is what makes that work. When you finally tick off a task that
was due three weeks ago, the next occurrence is the next one *at or after
today*, not three weeks ago plus one interval. Without it, completing a stale
task hands you another stale task.

RRULE parsing is `dateutil`, never hand-rolled — SPEC §4.3 says so for the
calendar and the reasoning is identical here. RFC 5545 is full of traps and this
is not the place to rediscover them.
"""

from __future__ import annotations

import datetime as dt

from dateutil.rrule import rrulestr


class InvalidRecurrenceRule(ValueError):
    """The supplied RRULE is not something dateutil can parse."""


def validate_rule(rule: str, *, dtstart: dt.datetime | None = None) -> str:
    """Check a rule parses, and hand back a normalised form.

    Called at the API boundary so an unparseable rule is rejected on the way in
    rather than raising later, at completion time, when someone is just trying
    to tick a box.
    """
    cleaned = rule.strip()
    if not cleaned:
        raise InvalidRecurrenceRule("Recurrence rule is empty.")

    try:
        rrulestr(cleaned, dtstart=dtstart or dt.datetime(2000, 1, 1, tzinfo=dt.UTC))
    except (ValueError, TypeError) as exc:
        raise InvalidRecurrenceRule(f"Not a valid recurrence rule: {exc}") from exc

    return cleaned


def next_occurrence(
    rule: str,
    *,
    series_start: dt.datetime,
    previous_due: dt.datetime,
    now: dt.datetime,
) -> dt.datetime | None:
    """The next due date after completing an instance that was due `previous_due`.

    `series_start` is the DTSTART of the whole series, not of this instance, and
    that distinction is load-bearing. Anchoring the rule to each instance's own
    due date restarts COUNT every time, so `FREQ=DAILY;COUNT=2` would recur
    forever instead of stopping after two.

    Returns None when the series has run out — COUNT and UNTIL can legitimately
    end one — and the caller should then simply not create a successor.

    The "at or after today" rule lives here: ask for the first occurrence after
    the one just completed, and if that is already in the past, skip forward to
    the first that is not. A daily chore neglected for a fortnight therefore
    comes back due once, not fourteen times over.
    """
    start = _as_aware(series_start)
    previous = _as_aware(previous_due)
    reference = _as_aware(now)

    try:
        rule_set = rrulestr(rule, dtstart=start)
    except (ValueError, TypeError) as exc:  # pragma: no cover - validated on write
        raise InvalidRecurrenceRule(str(exc)) from exc

    candidate = rule_set.after(previous, inc=False)
    if candidate is None:
        return None

    if _as_aware(candidate) >= reference:
        return _as_aware(candidate)

    # Completed late: skip to the first occurrence that has not already passed,
    # so completing a stale task does not immediately produce another one.
    caught_up = rule_set.after(reference, inc=True)
    return _as_aware(caught_up) if caught_up is not None else None


def _as_aware(value: dt.datetime) -> dt.datetime:
    """dateutil returns naive datetimes for naive input; the database is tz-aware."""
    return value if value.tzinfo is not None else value.replace(tzinfo=dt.UTC)


def describe(rule: str) -> str:
    """A short human label for a rule, for lists and audit entries.

    Deliberately crude: it covers the shapes the UI can produce and falls back to
    the raw rule rather than inventing prose for something it does not
    understand. A wrong description is worse than a technical one.
    """
    upper = rule.upper().replace("RRULE:", "")
    parts = dict(piece.split("=", 1) for piece in upper.split(";") if "=" in piece)
    freq = parts.get("FREQ", "")
    interval = int(parts.get("INTERVAL", "1") or 1)

    every = {
        "DAILY": ("Daily", "Every {n} days"),
        "WEEKLY": ("Weekly", "Every {n} weeks"),
        "MONTHLY": ("Monthly", "Every {n} months"),
        "YEARLY": ("Yearly", "Every {n} years"),
    }.get(freq)

    if every is None:
        return rule

    return every[0] if interval == 1 else every[1].format(n=interval)

"""Expanding a recurring event into occurrences (SPEC §4.3).

§4.3 calls per-occurrence editing "the hard part" and says not to hand-roll
recurrence. The rule parsing is `dateutil`; what lives here is everything around
it, which is where the actual traps are:

**Expansion happens in the event's own timezone, not in UTC.** "Every Monday at
09:00" means 09:00 local on each of those Mondays, and a household straddling a
DST change would otherwise watch the school run drift by an hour twice a year.
So we convert the start to local wall-clock time, generate occurrences against
*that*, and re-attach the zone afterwards — which is the only order that
produces 09:00 on both sides of the transition.

**COUNT counts occurrences before exclusions are applied** (RFC 5545 §3.8.5.3).
A series of `COUNT=10` with one excluded date yields nine visible occurrences,
not ten. Yuvomi documents the same thing. Getting this backwards means a
fortnightly series quietly running one cycle long.

**Nonexistent and ambiguous local times.** The hour that DST skips does not
exist, and the hour it repeats happens twice. Both are resolved explicitly
rather than left to whatever the platform does.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dateutil.rrule import rrulestr

#: A recurring event is not licence to generate unbounded work. An unbounded
#: rule queried over a wide window is bounded by this many occurrences.
MAX_OCCURRENCES = 2000

DEFAULT_TZID = "UTC"


class InvalidRecurrenceRule(ValueError):
    """The RRULE is not something dateutil can parse."""


class UnknownTimezone(ValueError):
    """The tzid is not an IANA zone name."""


@dataclass(frozen=True)
class Occurrence:
    """One instance of an event, in UTC.

    `original_start` identifies *which* occurrence this is within the series —
    the RFC 5545 RECURRENCE-ID. It is what an override or an exclusion points
    at, and it stays fixed even when the occurrence is moved.
    """

    starts_at: dt.datetime
    ends_at: dt.datetime
    original_start: dt.datetime


def zone_for(tzid: str) -> ZoneInfo:
    try:
        return ZoneInfo(tzid or DEFAULT_TZID)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise UnknownTimezone(f"Not an IANA timezone: {tzid!r}") from exc


def validate_rule(rule: str) -> str:
    """Reject an unparseable rule at the API boundary, not at render time."""
    cleaned = rule.strip()
    if not cleaned:
        raise InvalidRecurrenceRule("Recurrence rule is empty.")
    try:
        rrulestr(cleaned, dtstart=dt.datetime(2000, 1, 1))
    except (ValueError, TypeError) as exc:
        raise InvalidRecurrenceRule(f"Not a valid recurrence rule: {exc}") from exc
    return cleaned


def to_local(moment: dt.datetime, zone: ZoneInfo) -> dt.datetime:
    """The wall-clock time in `zone`, naive, for feeding to dateutil."""
    aware = moment if moment.tzinfo is not None else moment.replace(tzinfo=dt.UTC)
    return aware.astimezone(zone).replace(tzinfo=None)


def from_local(local: dt.datetime, zone: ZoneInfo) -> dt.datetime:
    """Re-attach `zone` to a naive wall-clock time and return UTC.

    Two edge cases the DST transition creates, both resolved deliberately:

    *Nonexistent* — the clocks jumped forward over this time. `fold` does not
    help; the time simply never happened. We push forward by an hour, which is
    what a person means by "09:30 on the day the clocks change".

    *Ambiguous* — the clocks went back, so this time happened twice. `fold=0`
    picks the first, which is the earlier real instant and the one a calendar
    entry made before the change refers to.
    """
    attached = local.replace(tzinfo=zone, fold=0)

    # A nonexistent local time round-trips to something different from itself.
    if to_local(attached, zone) != local:
        attached = (local + dt.timedelta(hours=1)).replace(tzinfo=zone, fold=0)

    return attached.astimezone(dt.UTC)


def localize_until(rule: str, zone: ZoneInfo) -> str:
    """Rewrite a UTC `UNTIL` into the zone's wall-clock time.

    Expansion runs against a naive local `DTSTART` so that DST is handled
    correctly, and dateutil refuses to mix a naive DTSTART with a UTC-qualified
    UNTIL. Storage keeps UNTIL in UTC as RFC 5545 requires; this converts it for
    the duration of the expansion only.

    Without this, *every* rule carrying an UNTIL fails to expand at all.
    """
    parts = rule.replace("RRULE:", "").split(";")
    rewritten: list[str] = []

    for piece in parts:
        name, _, value = piece.partition("=")
        if name.upper() != "UNTIL" or not value.endswith("Z"):
            rewritten.append(piece)
            continue

        try:
            moment = dt.datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=dt.UTC)
        except ValueError:
            rewritten.append(piece)
            continue

        local = moment.astimezone(zone).replace(tzinfo=None)
        rewritten.append(f"UNTIL={local.strftime('%Y%m%dT%H%M%S')}")

    return ";".join(rewritten)


def expand(
    *,
    starts_at: dt.datetime,
    ends_at: dt.datetime,
    rule: str | None,
    tzid: str,
    window_start: dt.datetime,
    window_end: dt.datetime,
    exclusions: frozenset[dt.datetime] = frozenset(),
    overridden: frozenset[dt.datetime] = frozenset(),
) -> list[Occurrence]:
    """Occurrences overlapping [window_start, window_end).

    `exclusions` are deleted occurrences (EXDATE); `overridden` are ones that a
    detached instance replaces, so the series must not also emit them — the
    override is returned separately by the caller and carries the edited values.
    Both are keyed on the occurrence's *original* start.

    An event that begins before the window but runs into it is included: a
    week-long holiday should appear when you look at its middle.
    """
    zone = zone_for(tzid)
    duration = ends_at - starts_at

    if not rule:
        single = Occurrence(starts_at=starts_at, ends_at=ends_at, original_start=starts_at)
        return [single] if _overlaps(single, window_start, window_end) else []

    try:
        rule_set = rrulestr(localize_until(rule, zone), dtstart=to_local(starts_at, zone))
    except (ValueError, TypeError) as exc:  # pragma: no cover - validated on write
        raise InvalidRecurrenceRule(str(exc)) from exc

    # Start generating from a little before the window so an occurrence that
    # began earlier and runs into it is not missed.
    from_local_naive = to_local(window_start - duration, zone)
    until_local_naive = to_local(window_end, zone)

    occurrences: list[Occurrence] = []
    for index, local_start in enumerate(
        rule_set.between(from_local_naive, until_local_naive, inc=True)
    ):
        if index >= MAX_OCCURRENCES:
            break

        original = from_local(local_start, zone)
        # Exclusions and overrides are applied *after* generation, which is what
        # keeps COUNT counting the full series per RFC 5545.
        if original in exclusions or original in overridden:
            continue

        candidate = Occurrence(
            starts_at=original, ends_at=original + duration, original_start=original
        )
        if _overlaps(candidate, window_start, window_end):
            occurrences.append(candidate)

    return occurrences


def _overlaps(occurrence: Occurrence, window_start: dt.datetime, window_end: dt.datetime) -> bool:
    return occurrence.starts_at < window_end and occurrence.ends_at > window_start


def occurrences_before(
    *,
    starts_at: dt.datetime,
    rule: str,
    tzid: str,
    split_at: dt.datetime,
) -> int:
    """How many occurrences fall strictly before `split_at`.

    Needed to split a COUNT-limited series: truncating it at an occurrence means
    the earlier part keeps the occurrences already taken and the new series gets
    the remainder. Getting this wrong makes a bounded series run long or stop
    short — invisibly, months later.
    """
    zone = zone_for(tzid)
    rule_set = rrulestr(rule, dtstart=to_local(starts_at, zone))
    local_split = to_local(split_at, zone)

    taken = 0
    for local_start in rule_set:
        if local_start >= local_split or taken >= MAX_OCCURRENCES:
            break
        taken += 1
    return taken


def rule_count(rule: str) -> int | None:
    """The COUNT of a rule, if it has one."""
    for piece in rule.upper().replace("RRULE:", "").split(";"):
        name, _, value = piece.partition("=")
        if name == "COUNT" and value.isdigit():
            return int(value)
    return None


def with_count(rule: str, count: int) -> str:
    """Replace COUNT, dropping UNTIL — RFC 5545 forbids both in one rule."""
    parts = [
        piece
        for piece in rule.replace("RRULE:", "").split(";")
        if piece and not piece.upper().startswith(("COUNT=", "UNTIL="))
    ]
    parts.append(f"COUNT={max(count, 0)}")
    return ";".join(parts)


def with_until(rule: str, until: dt.datetime) -> str:
    """Replace UNTIL, dropping COUNT. UNTIL is written in UTC, as RFC 5545 requires."""
    parts = [
        piece
        for piece in rule.replace("RRULE:", "").split(";")
        if piece and not piece.upper().startswith(("COUNT=", "UNTIL="))
    ]
    stamp = until.astimezone(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    parts.append(f"UNTIL={stamp}")
    return ";".join(parts)


def describe(rule: str) -> str:
    """A short human label. Falls back to the rule rather than inventing prose."""
    upper = rule.upper().replace("RRULE:", "")
    parts = dict(piece.split("=", 1) for piece in upper.split(";") if "=" in piece)
    freq = parts.get("FREQ", "")
    interval = int(parts.get("INTERVAL", "1") or 1)

    labels = {
        "DAILY": ("Daily", "Every {n} days"),
        "WEEKLY": ("Weekly", "Every {n} weeks"),
        "MONTHLY": ("Monthly", "Every {n} months"),
        "YEARLY": ("Yearly", "Every {n} years"),
    }.get(freq)
    if labels is None:
        return rule
    return labels[0] if interval == 1 else labels[1].format(n=interval)

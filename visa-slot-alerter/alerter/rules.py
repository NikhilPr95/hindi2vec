"""Decide which sightings deserve an alert, and remember what was already sent."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .fetcher import Sighting


@dataclass
class AlertRules:
    locations: list[str] = field(default_factory=list)   # [] = all cities
    include_vac: bool = True          # VAC rows = biometrics (OFC) appointment
    include_consulate: bool = True    # non-VAC rows = consular interview
    earliest_on_or_before: date | None = None   # alert only if earliest date <= this
    within_days: int | None = None              # ... or within N days of today
    min_slots: int = 1
    allow_unknown_slots: bool = True            # tracker often shows N/A for VAC
    max_sighting_age_minutes: int = 90          # ignore stale sightings
    renotify_after_minutes: int = 180           # same date still open -> remind

    @classmethod
    def from_dict(cls, d: dict) -> "AlertRules":
        d = dict(d or {})
        eob = d.get("earliest_on_or_before")
        if isinstance(eob, str):
            eob = date.fromisoformat(eob)
        elif isinstance(eob, datetime):
            eob = eob.date()
        return cls(
            locations=[str(x).upper().strip() for x in d.get("locations", []) or []],
            include_vac=bool(d.get("include_vac", True)),
            include_consulate=bool(d.get("include_consulate", True)),
            earliest_on_or_before=eob,
            within_days=d.get("within_days"),
            min_slots=int(d.get("min_slots", 1)),
            allow_unknown_slots=bool(d.get("allow_unknown_slots", True)),
            max_sighting_age_minutes=int(d.get("max_sighting_age_minutes", 90)),
            renotify_after_minutes=int(d.get("renotify_after_minutes", 180)),
        )

    def deadline(self, today: date) -> date | None:
        candidates = []
        if self.earliest_on_or_before:
            candidates.append(self.earliest_on_or_before)
        if self.within_days is not None:
            candidates.append(today + timedelta(days=int(self.within_days)))
        return min(candidates) if candidates else None

    def matches(self, s: Sighting, now: datetime) -> tuple[bool, str]:
        """Return (passes, reason-if-not)."""
        if self.locations and s.city not in self.locations:
            return False, "location not watched"
        if s.is_vac and not self.include_vac:
            return False, "VAC rows disabled"
        if not s.is_vac and not self.include_consulate:
            return False, "consulate rows disabled"
        age = now - s.seen
        if age > timedelta(minutes=self.max_sighting_age_minutes):
            return False, f"stale sighting ({int(age.total_seconds() // 60)} min old)"
        dl = self.deadline(now.date())
        if dl and s.earliest > dl:
            return False, f"earliest {s.earliest_date} is after deadline {dl.isoformat()}"
        if s.slots is None:
            if not self.allow_unknown_slots:
                return False, "slot count unknown"
        elif s.slots < self.min_slots:
            return False, f"only {s.slots} slot(s) < min {self.min_slots}"
        return True, ""


class State:
    """Tiny JSON file remembering, per (visa, location), what we last alerted on."""

    def __init__(self, path: str | os.PathLike):
        self.path = Path(path)
        self.data: dict = {"alerted": {}, "last_seen": {}}
        if self.path.exists():
            try:
                loaded = json.loads(self.path.read_text())
                if isinstance(loaded, dict):
                    self.data.update(loaded)
            except (ValueError, OSError):
                pass

    @staticmethod
    def key(s: Sighting) -> str:
        return f"{s.slug}|{s.location}"

    def should_alert(self, s: Sighting, rules: AlertRules) -> bool:
        prev = self.data["alerted"].get(self.key(s))
        if not prev:
            return True
        if prev.get("earliest_date") != s.earliest_date:
            return True  # a different (usually earlier) date opened
        if s.seen_utc <= prev.get("seen_utc", ""):
            return False  # same sighting we already announced
        last_alert = datetime.fromisoformat(prev["seen_utc"])
        return s.seen - last_alert >= timedelta(minutes=rules.renotify_after_minutes)

    def record_alert(self, s: Sighting) -> None:
        self.data["alerted"][self.key(s)] = {
            "earliest_date": s.earliest_date,
            "seen_utc": s.seen_utc,
            "slots": s.slots,
        }

    def record_seen(self, s: Sighting) -> None:
        self.data["last_seen"][self.key(s)] = s.to_dict()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, sort_keys=True))
        tmp.replace(self.path)


def evaluate(sightings: list[Sighting], rules: AlertRules, state: State,
             now: datetime | None = None) -> tuple[list[Sighting], list[tuple[Sighting, str]]]:
    """Return (sightings to alert on, (sighting, reason) pairs that were skipped)."""
    now = now or datetime.now(timezone.utc)
    hits: list[Sighting] = []
    skipped: list[tuple[Sighting, str]] = []
    for s in sightings:
        state.record_seen(s)
        ok, why = rules.matches(s, now)
        if not ok:
            skipped.append((s, why))
            continue
        if not state.should_alert(s, rules):
            skipped.append((s, "already alerted"))
            continue
        hits.append(s)
    hits.sort(key=lambda s: (s.earliest_date, s.location))
    return hits, skipped

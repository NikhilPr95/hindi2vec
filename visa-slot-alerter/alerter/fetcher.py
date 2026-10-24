"""Fetch and parse slot sightings from the public checkvisaslots.com tracker pages.

Each visa-type page (e.g. /latest-us-visa-availability/h-1b-regular/) has a table:

  Visa Location | Visa Type | Earliest Date | Slots on Earliest Date |
  Total Dates Available | Last Seen At | Relative Time

"Earliest Date" looks like "03 Nov, 26"; "Last Seen At" looks like
"24 Oct 2026, 11:36 AM" and is in UTC. Locations ending in "VAC" are the
Visa Application Centre (biometrics/OFC) appointments; the others are the
consular interview.
"""
from __future__ import annotations

import re
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from typing import Iterable

import requests
from bs4 import BeautifulSoup

DEFAULT_BASE = "https://checkvisaslots.com/latest-us-visa-availability/{slug}/"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
}


@dataclass(frozen=True)
class Sighting:
    slug: str            # tracker page slug, e.g. "h-1b-regular"
    visa_label: str      # e.g. "H-1B (Regular)"
    location: str        # upper-case, e.g. "HYDERABAD" or "HYDERABAD VAC"
    earliest_date: str   # ISO date
    slots: int | None    # seats on the earliest date (None = tracker shows N/A)
    total_dates: int | None
    seen_utc: str        # ISO 8601 datetime with +00:00 offset

    @property
    def is_vac(self) -> bool:
        return self.location.endswith(" VAC")

    @property
    def city(self) -> str:
        return self.location[:-4] if self.is_vac else self.location

    @property
    def earliest(self) -> date:
        return date.fromisoformat(self.earliest_date)

    @property
    def seen(self) -> datetime:
        return datetime.fromisoformat(self.seen_utc)

    def to_dict(self) -> dict:
        return asdict(self)


def _int(s: str) -> int | None:
    s = s.strip().replace(",", "")
    return int(s) if s.isdigit() else None


def parse_earliest(s: str) -> date | None:
    s = s.strip()
    for fmt in ("%d %b, %y", "%d %b %y", "%d %b, %Y", "%d %b %Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    return None


def parse_seen(s: str) -> datetime | None:
    s = s.strip()
    for fmt in ("%d %b %Y, %I:%M %p", "%d %b %Y %I:%M %p", "%d %b %Y, %H:%M", "%d %b %Y %H:%M"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return None


# Fallback for pages rendered without a real <table>: scan pipe-joined text.
_ROW_RE = re.compile(
    r"(?P<loc>[A-Z][A-Z ]+?(?: VAC)?)\s*\|\s*(?P<visa>[^|]+?)\s*\|\s*"
    r"(?P<earliest>\d{1,2} [A-Z][a-z]{2},? \d{2,4})\s*\|\s*(?P<slots>N/A|\d+)\s*\|\s*"
    r"(?P<total>N/A|\d+)\s*\|\s*(?P<seen>\d{1,2} [A-Z][a-z]{2} \d{4},? \d{1,2}:\d{2} [AP]M)"
)


def _rows_from_html(html: str) -> list[list[str]]:
    soup = BeautifulSoup(html, "html.parser")
    rows: list[list[str]] = []
    for table in soup.find_all("table"):
        for tr in table.find_all("tr"):
            cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
            if len(cells) >= 6 and cells[0].strip().lower() != "visa location":
                rows.append(cells)
    if rows:
        return rows
    text = soup.get_text("|", strip=True)
    for m in _ROW_RE.finditer(text):
        rows.append([m["loc"], m["visa"], m["earliest"], m["slots"], m["total"], m["seen"]])
    return rows


def parse_page(html: str, slug: str) -> list[Sighting]:
    out: list[Sighting] = []
    for cells in _rows_from_html(html):
        loc, visa, earliest, slots, total, seen = (c.strip() for c in cells[:6])
        ed, st = parse_earliest(earliest), parse_seen(seen)
        if not ed or not st:
            continue
        out.append(
            Sighting(
                slug=slug,
                visa_label=visa,
                location=re.sub(r"\s+", " ", loc).upper(),
                earliest_date=ed.isoformat(),
                slots=_int(slots),
                total_dates=_int(total),
                seen_utc=st.isoformat(),
            )
        )
    return out


def fetch_page(slug: str, base: str = DEFAULT_BASE, session: requests.Session | None = None,
               timeout: int = 20) -> str:
    s = session or requests.Session()
    r = s.get(base.format(slug=slug), headers=HEADERS, timeout=timeout)
    r.raise_for_status()
    return r.text


def fetch_all(slugs: Iterable[str], base: str = DEFAULT_BASE, delay: float = 2.0,
              session: requests.Session | None = None) -> tuple[list[Sighting], list[str]]:
    """Fetch every tracker page in `slugs`. Returns (sightings, errors)."""
    s = session or requests.Session()
    sightings: list[Sighting] = []
    errors: list[str] = []
    slugs = list(slugs)
    for i, slug in enumerate(slugs):
        try:
            rows = parse_page(fetch_page(slug, base, s), slug)
            if not rows:
                errors.append(f"{slug}: page loaded but no rows parsed")
            sightings.extend(rows)
        except Exception as e:  # keep going on one bad page
            errors.append(f"{slug}: {e}")
        if i < len(slugs) - 1 and delay > 0:
            time.sleep(delay)
    return sightings, errors


# ---------------------------------------------------------------------------
# Optional JSON source: the checkvisaslots.com app API used by its extension.
# GET https://app.checkvisaslots.com/slots/v3 with header x-api-key=<access code>
# -> {"slotDetails": [{"visa_location": "HYDERABAD VAC", "createdon": "...",
#                      "slots": 3, "start_date": "2026-11-05", ...}], "visa_type": ...}
# The access code is the one the extension asks for; the API answers 429 for
# accounts that have not contributed recently, in which case we fall back to
# the public HTML pages.
# ---------------------------------------------------------------------------
API_BASE = "https://app.checkvisaslots.com"


def _api_date(s: str) -> date | None:
    s = (s or "").strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%d %b, %y", "%d %b %Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(s[:19] if "T" in s else s, fmt).date()
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _api_datetime(s: str) -> datetime | None:
    s = (s or "").strip()
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        d = parse_seen(s)
        if not d:
            return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def parse_api(payload, slug: str = "api") -> list[Sighting]:
    rows = (payload or {}).get("slotDetails") if isinstance(payload, dict) else None
    label = str((payload or {}).get("visa_type") or "").strip() if isinstance(payload, dict) else ""
    out: list[Sighting] = []
    for r in rows or []:
        if not isinstance(r, dict) or not r.get("visa_location"):
            continue
        ed = _api_date(str(r.get("start_date") or r.get("earliest_date") or ""))
        seen = _api_datetime(str(r.get("createdon") or ""))
        if not ed or not seen:
            continue
        try:
            n = int(r.get("slots") or 0)
        except (TypeError, ValueError):
            n = 0
        out.append(Sighting(
            slug=slug, visa_label=r.get("visa_type") or label or slug,
            location=re.sub(r"\s+", " ", str(r["visa_location"])).upper(),
            earliest_date=ed.isoformat(), slots=None, total_dates=n,
            seen_utc=seen.astimezone(timezone.utc).isoformat(),
        ))
    return out


def fetch_api(api_key: str, session: requests.Session | None = None, timeout: int = 20) -> tuple[list[Sighting], int]:
    """Returns (sightings, http_status). Raises nothing; a non-200 gives ([], status)."""
    s = session or requests.Session()
    r = s.get(f"{API_BASE}/slots/v3", headers={"x-api-key": api_key, "extVersion": "5.1.0",
                                                "User-Agent": HEADERS["User-Agent"]}, timeout=timeout)
    if r.status_code != 200:
        return [], r.status_code
    try:
        return parse_api(r.json()), 200
    except ValueError:
        return [], 200

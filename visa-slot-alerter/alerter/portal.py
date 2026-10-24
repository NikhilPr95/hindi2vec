"""Direct checker for the official portal, https://www.usvisascheduling.com/.

This drives a real Chromium window with Playwright and a *persistent* browser
profile so that you log in by hand once (Cloudflare check, captcha, security
questions, OTP) and later runs reuse that session.

Read this before enabling it
----------------------------
The portal is known to hide availability for ~24 h after a handful of calendar
refreshes and to lock accounts for ~72 h when it decides a user is a bot. So:

* every run does ONE pass: home -> Reschedule Appointment -> pick consular
  post -> read the calendar -> leave. No clicking around, no submits, ever;
* `max_checks_per_day` (default 6) is enforced through the state file;
* the tracker-based checker (alerter.main) stays your high-frequency source,
  this one is a low-frequency ground truth.

The login/menu part of the flow is based on selectors confirmed against the
live site by the VisaPilot project (Azure B2C form: #signInName, #password,
#continue; a Queue-it virtual queue; "Reschedule Appointment" link; a
<select> of consular posts). The calendar part has NOT been verified against a
live account; run with `--inspect` once after logging in and send the dump if
it fails to find dates, then adjust `CALENDAR_SELECTORS` below.
"""
from __future__ import annotations

import json
import re
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

HOME_URL = "https://www.usvisascheduling.com/en-us/"

SEL = {
    "user": "#signInName",
    "pass": "#password",
    "submit": "#continue",
}
QUEUE_MARKERS = [
    re.compile(r"You are now in line", re.I),
    re.compile(r"estimated wait time", re.I),
    re.compile(r"virtual queue", re.I),
]
LOCKOUT_MARKERS = [
    re.compile(r"locked", re.I),
    re.compile(r"too many", re.I),
    re.compile(r"try again (later|after)", re.I),
    re.compile(r"no appointments? (are )?available", re.I),
]

# Candidate selectors for selectable calendar days. Each is tried in turn;
# the first one that yields at least one element wins.
CALENDAR_SELECTORS = [
    # bootstrap-datepicker / Power Pages date picker
    "td.day:not(.disabled):not(.old):not(.new)",
    # jQuery UI datepicker
    "td:not(.ui-datepicker-unselectable) a.ui-state-default",
    # generic: any day cell flagged available
    "td.available, td.has-slots, .calendar-day.available",
    # generic: buttons/links inside a calendar whose text is a day number
    "[class*=calendar] button:not([disabled]), [class*=calendar] a[data-date]",
]
MONTH_YEAR_RE = re.compile(r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{4})", re.I)


@dataclass
class PortalResult:
    post: str
    checked_at: str
    available_dates: list[str]      # ISO dates found on the first calendar page(s)
    note: str = ""                   # e.g. "queue timeout", "lockout text seen"

    @property
    def earliest(self) -> str | None:
        return min(self.available_dates) if self.available_dates else None


class Throttle:
    """Counts checks per UTC day in the shared state file."""

    def __init__(self, state_path: str | Path, max_per_day: int):
        self.path = Path(state_path)
        self.max_per_day = max_per_day
        self.data: dict = {}
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text())
            except (ValueError, OSError):
                self.data = {}
        self.data.setdefault("portal", {"day": "", "count": 0, "last_result": None})

    def allow(self, now: datetime) -> bool:
        p = self.data["portal"]
        today = now.date().isoformat()
        if p["day"] != today:
            p["day"], p["count"] = today, 0
        return p["count"] < self.max_per_day

    def record(self, now: datetime, result: "PortalResult | None") -> None:
        p = self.data["portal"]
        p["day"] = now.date().isoformat()
        p["count"] = int(p.get("count", 0)) + 1
        if result is not None:
            p["last_result"] = result.__dict__
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=2, sort_keys=True))


def _page_text(page) -> str:
    try:
        return page.evaluate("() => document.body ? document.body.innerText : ''") or ""
    except Exception:
        return ""


def wait_through_queue(page, max_minutes: float = 45) -> bool:
    """The site inserts a Queue-it page that refreshes itself; just wait it out."""
    deadline = time.time() + max_minutes * 60
    announced = False
    while time.time() < deadline:
        text = _page_text(page)
        if not any(m.search(text) for m in QUEUE_MARKERS):
            if announced:
                print("  queue released")
            return True
        if not announced:
            print("  in the portal's virtual queue; waiting...")
            announced = True
        page.wait_for_timeout(10_000)
    return False


def ensure_logged_in(page, interactive: bool, timeout_minutes: float = 10) -> bool:
    """Return True once we are on a logged-in portal page.

    If the B2C sign-in form shows up we do NOT type credentials: with
    `interactive` the user signs in by hand in the open window (captcha,
    security questions, OTP), otherwise we give up.
    """
    page.goto(HOME_URL, wait_until="domcontentloaded", timeout=60_000)
    try:
        page.wait_for_load_state("networkidle", timeout=30_000)
    except Exception:
        pass
    if not wait_through_queue(page):
        return False

    deadline = time.time() + timeout_minutes * 60
    while time.time() < deadline:
        url = page.url
        on_login = "b2clogin.com" in url or page.locator(SEL["user"]).count() > 0
        if not on_login:
            # Logged-in landing page shows the application menu.
            if page.get_by_role("link", name=re.compile("Reschedule Appointment", re.I)).count() > 0:
                return True
            # Public home page without a session shows a sign-in link.
            signin = page.get_by_role("link", name=re.compile(r"sign ?in|log ?in", re.I))
            if signin.count() > 0 and interactive:
                print("  not signed in; opening the sign-in page for you")
                signin.first.click()
                page.wait_for_timeout(3_000)
                continue
        if not interactive:
            print("  session expired: run again with --login to sign in by hand", file=sys.stderr)
            return False
        print("  >>> Please sign in in the browser window (captcha / security questions / OTP). Waiting...")
        page.wait_for_timeout(5_000)
    return False


def open_reschedule(page) -> bool:
    link = page.get_by_role("link", name=re.compile("Reschedule Appointment", re.I)).first
    try:
        link.wait_for(state="visible", timeout=15_000)
    except Exception:
        return False
    link.click()
    try:
        page.wait_for_load_state("networkidle", timeout=30_000)
    except Exception:
        pass
    return wait_through_queue(page)


def select_post(page, post_label: str) -> bool:
    """Pick the consular post in the first visible <select> that offers it."""
    selects = page.locator("select:visible")
    try:
        selects.first.wait_for(state="visible", timeout=30_000)
    except Exception:
        return False
    for i in range(selects.count()):
        cand = selects.nth(i)
        options = cand.locator("option").evaluate_all(
            "els => els.map(e => ({value: e.value, label: e.innerText.trim()}))"
        )
        match = next((o for o in options if o["value"] and o["label"].lower() == post_label.lower()), None)
        if match:
            cand.select_option(match["value"])
            try:
                page.wait_for_load_state("networkidle", timeout=30_000)
            except Exception:
                pass
            page.wait_for_timeout(3_000)
            return wait_through_queue(page)
    print("  consular post dropdown found but no option labelled %r; options were: %s"
          % (post_label, options), file=sys.stderr)
    return False


def _month_year_near(page) -> tuple[int, int] | None:
    m = MONTH_YEAR_RE.search(_page_text(page))
    if not m:
        return None
    month = datetime.strptime(m.group(1)[:3], "%b").month
    return int(m.group(2)), month


def read_calendar(page) -> list[str]:
    """Collect ISO dates for every selectable day visible on the current calendar."""
    found: list[str] = []
    for sel in CALENDAR_SELECTORS:
        cells = page.locator(sel)
        n = cells.count()
        if n == 0:
            continue
        ym = _month_year_near(page)
        for i in range(n):
            cell = cells.nth(i)
            data_date = cell.get_attribute("data-date") or cell.get_attribute("data-day")
            if data_date:
                try:
                    found.append(datetime.fromisoformat(data_date[:10]).date().isoformat())
                    continue
                except ValueError:
                    pass
            txt = (cell.inner_text() or "").strip()
            if txt.isdigit() and ym:
                try:
                    found.append(date(ym[0], ym[1], int(txt)).isoformat())
                except ValueError:
                    pass
        if found:
            break
    return sorted(set(found))


def inspect_dump(page, out_dir: Path) -> Path:
    """Save the page's HTML + a screenshot + a field dump so selectors can be tuned."""
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    (out_dir / f"{stamp}.html").write_text(page.content(), encoding="utf-8")
    try:
        page.screenshot(path=str(out_dir / f"{stamp}.png"), full_page=True)
    except Exception:
        pass
    dump = page.evaluate(
        """() => ({
          url: location.href,
          selects: [...document.querySelectorAll('select')].map(s => ({id: s.id, name: s.name,
              options: [...s.options].map(o => o.text.trim())})),
          calendars: [...document.querySelectorAll('[class*=calendar], [class*=datepicker], table')]
              .slice(0, 10).map(e => ({tag: e.tagName, cls: e.className, text: (e.innerText||'').slice(0, 300)})),
        })"""
    )
    (out_dir / f"{stamp}.json").write_text(json.dumps(dump, indent=2))
    return out_dir / f"{stamp}.html"


def check_portal(profile_dir: str | Path, post_label: str, *, interactive: bool = False,
                 headless: bool = False, inspect_dir: Path | None = None) -> PortalResult:
    from playwright.sync_api import sync_playwright

    now = datetime.now(timezone.utc)
    result = PortalResult(post=post_label, checked_at=now.isoformat(), available_dates=[])
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            str(profile_dir), headless=headless, viewport=None, args=["--start-maximized"]
        )
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            if not ensure_logged_in(page, interactive=interactive):
                result.note = "not logged in"
                return result
            if not open_reschedule(page):
                result.note = "Reschedule Appointment link not found (page layout changed?)"
                if inspect_dir:
                    result.note += f"; dump in {inspect_dump(page, inspect_dir)}"
                return result
            if not select_post(page, post_label):
                result.note = "could not select consular post"
                if inspect_dir:
                    result.note += f"; dump in {inspect_dump(page, inspect_dir)}"
                return result
            text = _page_text(page)
            if any(m.search(text) for m in LOCKOUT_MARKERS):
                result.note = "portal shows a lockout / no-availability notice; back off"
            result.available_dates = read_calendar(page)
            if inspect_dir:
                inspect_dump(page, inspect_dir)
            if not result.available_dates and not result.note:
                result.note = "no selectable dates found (none open, or calendar selectors need tuning: run --inspect)"
        finally:
            ctx.close()
    return result

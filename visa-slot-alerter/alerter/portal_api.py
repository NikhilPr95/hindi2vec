"""Direct, UI-free checks against the official portal's own data calls.

How the portal works (learned from the browser extensions that read it):

* The *Reschedule Appointment* page lists consular posts in ``<select id="post_select">``;
  each option's value is the portal's internal ``postId``.
* When a post is chosen, the page fires an XHR to a URL on usvisascheduling.com
  whose path contains ``schedule-group/get-family-consular-schedule-days``
  (or ``...-ofc-...`` for biometrics, ``get-family-emergency-...`` for
  expedited), with a query string ``route=...&cacheString=<ms>`` and a
  form body ``parameters=<url-encoded JSON {"postId": ...}>``.
  The JSON reply is ``{"ScheduleDays": [{"Date": "YYYY-MM-DD", ...}, ...]}``.
* Clicking a date fires ``...-schedule-entries`` with ``{"scheduleDate": ...}``
  and gets ``{"ScheduleEntries": [{"Time": ..., "EntriesAvailable": N}, ...]}``.

So the cheapest possible check is one POST per post per appointment type. This
module does it in two phases:

1. ``capture``: with your logged-in persistent profile, open the reschedule
   page once, pick each post, and record the exact request (URL, headers,
   body) the page makes. Saved to a recipe file. No guessing of paths.
2. ``check``: replay the recorded request from the same browser context (so
   the session cookies and anti-forgery headers are reused) and parse the JSON.
   No page is loaded. Each check is one HTTP request of a few hundred ms.

The server still counts these calls, so the daily cap in config.yaml applies.
"""
from __future__ import annotations

import json
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

SCHEDULE_DAYS_RE = re.compile(r"schedule-group/get-family-(emergency-)?(ofc|consular)-schedule-days", re.I)
SCHEDULE_ENTRIES_RE = re.compile(r"schedule-group/get-family-(emergency-)?(ofc|consular)-schedule-entries", re.I)
PRIVATE_HEADERS = {"cookie", "content-length", "host", "origin", "referer"}


@dataclass
class Recipe:
    """One recorded schedule-days request, replayable later."""
    kind: str                  # "consular" or "ofc"
    url: str                   # full URL incl. route=...&cacheString=...
    method: str
    headers: dict              # request headers minus cookies (cookies live in the profile)
    body: str                  # e.g. "parameters=%7B%22postId%22%3A%22...%22%7D"
    post_id: str
    post_label: str
    captured_at: str
    entries_url: str | None = None   # schedule-entries request, if observed

    def with_cache_buster(self) -> str:
        u = urlparse(self.url)
        q = parse_qs(u.query, keep_blank_values=True)
        q["cacheString"] = [str(int(time.time() * 1000))]
        query = "&".join(f"{k}={quote(v[0], safe='/')}" for k, v in q.items())
        return u._replace(query=query).geturl()

    def body_for(self, post_id: str | None = None, **extra) -> str:
        params = parse_qs(self.body, keep_blank_values=True)
        raw = params.get("parameters", ["{}"])[0]
        try:
            obj = json.loads(raw)
        except ValueError:
            obj = {}
        if post_id:
            obj["postId"] = post_id
        obj.update(extra)
        others = {k: v[0] for k, v in params.items() if k != "parameters"}
        parts = [f"parameters={quote(json.dumps(obj, separators=(',', ':')), safe='')}"]
        parts += [f"{k}={quote(v, safe='')}" for k, v in others.items()]
        return "&".join(parts)


@dataclass
class RecipeBook:
    captured_at: str = ""
    page_url: str = ""
    posts: dict = field(default_factory=dict)       # label -> postId
    recipes: list = field(default_factory=list)     # list[Recipe as dict]

    @classmethod
    def load(cls, path: str | Path) -> "RecipeBook":
        d = json.loads(Path(path).read_text())
        return cls(captured_at=d.get("captured_at", ""), page_url=d.get("page_url", ""),
                   posts=d.get("posts", {}), recipes=d.get("recipes", []))

    def save(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(asdict(self), indent=2))

    def recipe(self, kind: str) -> Recipe | None:
        for r in self.recipes:
            if r["kind"] == kind:
                return Recipe(**r)
        return None

    def post_id(self, label: str) -> str | None:
        for k, v in self.posts.items():
            if k.strip().lower() == label.strip().lower():
                return v
        return None


def parse_schedule_days(payload) -> list[str]:
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            return []
    rows = (payload or {}).get("ScheduleDays") if isinstance(payload, dict) else None
    out = set()
    for row in rows or []:
        d = str((row or {}).get("Date", "")).strip()[:10]
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", d):
            out.add(d)
    return sorted(out)


def parse_schedule_entries(payload) -> list[tuple[str, int]]:
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            return []
    rows = (payload or {}).get("ScheduleEntries") if isinstance(payload, dict) else None
    out = []
    for row in rows or []:
        if not isinstance(row, dict) or row.get("Time") in (None, ""):
            continue
        try:
            out.append((str(row["Time"]), int(row.get("EntriesAvailable", 0))))
        except (TypeError, ValueError):
            continue
    return out


def _kind(url: str) -> str | None:
    m = SCHEDULE_DAYS_RE.search(url)
    return m.group(2).lower() if m else None


def capture(profile_dir: str | Path, recipe_path: str | Path, *, headless: bool = False,
            post_labels: list[str] | None = None, interactive: bool = True) -> RecipeBook:
    """Open the reschedule page once, select posts, record the schedule-days requests."""
    from playwright.sync_api import sync_playwright

    from .portal import ensure_logged_in, open_reschedule, wait_through_queue

    book = RecipeBook(captured_at=datetime.now(timezone.utc).isoformat())
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(str(profile_dir), headless=headless, viewport=None)
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            seen: dict[str, dict] = {}

            def on_request(req):
                k = _kind(req.url)
                if k and req.method.upper() == "POST":
                    seen[k] = {
                        "url": req.url, "method": req.method,
                        "headers": {h: v for h, v in req.headers.items() if h.lower() not in PRIVATE_HEADERS},
                        "body": req.post_data or "",
                    }
                elif SCHEDULE_ENTRIES_RE.search(req.url):
                    seen.setdefault("_entries", {})["url"] = req.url

            page.on("request", on_request)
            if not ensure_logged_in(page, interactive=interactive):
                raise RuntimeError("not logged in")
            if not open_reschedule(page):
                raise RuntimeError("Reschedule Appointment link not found")
            book.page_url = page.url
            sel = page.locator("#post_select")
            if sel.count() == 0:
                sel = page.locator("select:visible").first
            sel.wait_for(state="visible", timeout=30_000)
            options = sel.locator("option").evaluate_all(
                "els => els.filter(e => e.value).map(e => ({value: e.value, label: e.innerText.trim()}))")
            book.posts = {o["label"]: o["value"] for o in options}
            wanted = [o for o in options if not post_labels
                      or o["label"].lower() in {p.lower() for p in post_labels}]
            if not wanted:
                raise RuntimeError(f"none of {post_labels} in dropdown; options: {[o['label'] for o in options]}")
            for o in wanted:
                seen.pop("consular", None); seen.pop("ofc", None)
                sel.select_option(o["value"])
                deadline = time.time() + 30
                while time.time() < deadline and not any(k in seen for k in ("consular", "ofc")):
                    page.wait_for_timeout(500)
                wait_through_queue(page, max_minutes=5)
                page.wait_for_timeout(2_000)
                for k in ("consular", "ofc"):
                    if k in seen and not any(r["kind"] == k for r in book.recipes):
                        r = Recipe(kind=k, post_id=o["value"], post_label=o["label"],
                                   captured_at=book.captured_at,
                                   entries_url=(seen.get("_entries") or {}).get("url"), **seen[k])
                        book.recipes.append(asdict(r))
                if len(wanted) > 1:
                    page.wait_for_timeout(3_000)  # be gentle between posts
            if not book.recipes:
                raise RuntimeError("no schedule-days request observed; the page may not load the "
                                   "calendar until a post is chosen, or the route changed")
        finally:
            ctx.close()
    book.save(recipe_path)
    return book


@dataclass
class ApiCheck:
    post_label: str
    kind: str
    checked_at: str
    dates: list[str]
    status: int
    note: str = ""
    earliest_slots: list[tuple[str, int]] | None = None


def check(profile_dir: str | Path, book: RecipeBook, post_label: str, kinds=("consular", "ofc"),
          *, with_entries: bool = False, headless: bool = True) -> list[ApiCheck]:
    """Replay the recorded requests inside the logged-in browser context (cookies reused)."""
    from playwright.sync_api import sync_playwright

    post_id = book.post_id(post_label)
    if not post_id:
        raise ValueError(f"post {post_label!r} not in recipe; known: {list(book.posts)}")
    results: list[ApiCheck] = []
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(str(profile_dir), headless=headless)
        try:
            for kind in kinds:
                r = book.recipe(kind)
                if not r:
                    continue
                now = datetime.now(timezone.utc).isoformat()
                resp = ctx.request.post(r.with_cache_buster(), headers=r.headers, data=r.body_for(post_id),
                                        max_redirects=0, timeout=30_000)
                text = resp.text()
                res = ApiCheck(post_label=post_label, kind=kind, checked_at=now, dates=[], status=resp.status)
                ctype = resp.headers.get("content-type", "")
                if resp.status in (301, 302, 401, 403) or ("json" not in ctype and "<html" in text[:500].lower()):
                    res.note = "session expired or blocked (got a redirect/HTML); run --portal-api --capture again"
                elif resp.status != 200:
                    res.note = f"HTTP {resp.status}: {text[:200]}"
                else:
                    res.dates = parse_schedule_days(text)
                    if not res.dates and "ScheduleDays" not in text:
                        res.note = f"unexpected body: {text[:200]}"
                    if with_entries and res.dates and r.entries_url:
                        eresp = ctx.request.post(Recipe(**{**asdict(r), "url": r.entries_url}).with_cache_buster(),
                                                 headers=r.headers,
                                                 data=r.body_for(post_id, scheduleDate=res.dates[0]), timeout=30_000)
                        if eresp.status == 200:
                            res.earliest_slots = parse_schedule_entries(eresp.text())
                results.append(res)
                time.sleep(1.5)
        finally:
            ctx.close()
    return results

"""Command-line entry point.

  python -m alerter.main --config config.yaml            # one check (what the cron runs)
  python -m alerter.main --config config.yaml --loop 10  # keep checking every 10 minutes
  python -m alerter.main --config config.yaml --dry-run  # print instead of sending
  python -m alerter.main --test-notify                   # send a hello to every channel
  python -m alerter.main --telegram-chat-id              # discover your Telegram chat id

Direct portal checks (see alerter/portal.py for the risks):
  python -m alerter.main --portal --login                # first time: sign in by hand, then one check
  python -m alerter.main --portal                        # one low-frequency check of the official portal
  python -m alerter.main --portal --inspect              # also dump HTML/screenshot to tune selectors
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

import yaml

from .fetcher import DEFAULT_BASE, Sighting, fetch_all, parse_page
from .notify import Notifier, format_message, telegram_chat_ids
from .rules import AlertRules, State, evaluate

DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "config.yaml"


def load_config(path: str | os.PathLike) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    cfg.setdefault("tracker_base", DEFAULT_BASE)
    cfg.setdefault("visa_types", ["h-1b-regular"])
    cfg.setdefault("state_file", "state.json")
    cfg.setdefault("request_delay_seconds", 2)
    cfg.setdefault("alert", {})
    cfg.setdefault("portal", {})
    return cfg


def slugs_from_config(cfg: dict) -> list[str]:
    out = []
    for item in cfg["visa_types"]:
        out.append(item["slug"] if isinstance(item, dict) else str(item))
    return out


def run_once(cfg: dict, notifier: Notifier, html_files: list[str] | None = None,
             verbose: bool = False) -> int:
    """Fetch, evaluate, notify. Returns the number of alerts sent."""
    rules = AlertRules.from_dict(cfg["alert"])
    state = State(cfg["state_file"])
    now = datetime.now(timezone.utc)

    if html_files:
        sightings: list[Sighting] = []
        errors: list[str] = []
        for path in html_files:
            slug = Path(path).stem
            sightings.extend(parse_page(Path(path).read_text(encoding="utf-8"), slug))
    else:
        sightings, errors = fetch_all(
            slugs_from_config(cfg), base=cfg["tracker_base"],
            delay=float(cfg["request_delay_seconds"]),
        )

    hits, skipped = evaluate(sightings, rules, state, now=now)
    stamp = now.strftime("%Y-%m-%d %H:%M UTC")
    print(f"[{stamp}] parsed {len(sightings)} row(s); {len(hits)} alert(s); {len(skipped)} skipped")
    if verbose:
        for s, why in skipped:
            print(f"  skip {s.location:<16} {s.earliest_date} slots={s.slots} seen={s.seen_utc}: {why}")
    for err in errors:
        print(f"  error: {err}", file=sys.stderr)

    if hits:
        text = format_message(hits)
        print(text)
        send_errors = notifier.send(text)
        for err in send_errors:
            print(f"  notify error: {err}", file=sys.stderr)
        if not send_errors or notifier.dry_run:
            for s in hits:
                state.record_alert(s)
    if not notifier.dry_run:
        state.save()
    if errors and not sightings:
        return -1
    return len(hits)


def run_portal(cfg: dict, notifier: Notifier, *, login: bool, inspect: bool,
               headless: bool | None) -> int:
    """One pass over the official portal. Returns 0 ok, 1 skipped/throttled, 2 failed."""
    from .portal import Throttle, check_portal

    pcfg = cfg["portal"]
    post = pcfg.get("consular_post")
    if not post:
        print("portal.consular_post is not set in config.yaml (e.g. \"HYDERABAD\").", file=sys.stderr)
        return 2
    now = datetime.now(timezone.utc)
    throttle = Throttle(cfg["state_file"], int(pcfg.get("max_checks_per_day", 6)))
    if not login and not throttle.allow(now):
        print(f"portal: already checked {throttle.max_per_day}x today; skipping to avoid a lockout")
        return 1

    profile = Path(pcfg.get("profile_dir", ".portal-profile")).expanduser()
    if headless is None:
        headless = bool(pcfg.get("headless", False)) and not login
    inspect_dir = Path(pcfg.get("inspect_dir", "portal-dumps")) if inspect else None
    result = check_portal(profile, post, interactive=login, headless=headless, inspect_dir=inspect_dir)
    throttle.record(now, result)

    rules = AlertRules.from_dict(cfg["alert"])
    deadline = rules.deadline(now.date())
    good = [d for d in result.available_dates if not deadline or date.fromisoformat(d) <= deadline]
    print(f"portal [{post}] {result.checked_at[:16]}: dates={result.available_dates or 'none'} "
          f"{('note: ' + result.note) if result.note else ''}")
    if result.note == "not logged in":
        return 2

    prev = (throttle.data["portal"].get("previous_dates") or [])
    throttle.data["portal"]["previous_dates"] = result.available_dates
    throttle.path.write_text(json.dumps(throttle.data, indent=2, sort_keys=True))
    new = sorted(set(good) - set(prev))
    if new:
        text = ("🛂 Official portal shows open dates\n"
                f"• {post.title()}: " + ", ".join(new) + "\n\n"
                f"Book now: https://www.usvisascheduling.com/")
        print(text)
        for err in notifier.send(text, subject="US visa slot: official portal"):
            print("  notify error:", err, file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Alert when US visa slots in India appear on the tracker.")
    p.add_argument("--config", default=str(DEFAULT_CONFIG), help="path to config.yaml")
    p.add_argument("--loop", type=float, metavar="MINUTES", help="keep running, checking every N minutes")
    p.add_argument("--dry-run", action="store_true", help="print alerts instead of sending; do not save state")
    p.add_argument("--verbose", "-v", action="store_true", help="explain every skipped row")
    p.add_argument("--html-file", action="append", help="parse a saved tracker page instead of fetching (file stem = slug)")
    p.add_argument("--test-notify", action="store_true", help="send a test message to every configured channel")
    p.add_argument("--telegram-chat-id", action="store_true", help="print chat ids that messaged your bot")
    p.add_argument("--portal", action="store_true", help="check the official usvisascheduling.com portal (low frequency!)")
    p.add_argument("--login", action="store_true", help="with --portal: open a window so you can sign in by hand")
    p.add_argument("--inspect", action="store_true", help="with --portal: save HTML/screenshot/field dump for tuning")
    p.add_argument("--headless", action="store_true", help="with --portal: run the browser without a window")
    args = p.parse_args(argv)

    if args.telegram_chat_id:
        token = os.environ.get("TELEGRAM_BOT_TOKEN")
        if not token:
            print("Set TELEGRAM_BOT_TOKEN first.", file=sys.stderr)
            return 2
        chats = telegram_chat_ids(token)
        if not chats:
            print("No chats yet. Open Telegram, send your bot any message (e.g. /start), then rerun.")
        for c in chats:
            name = c.get("title") or " ".join(x for x in (c.get("first_name"), c.get("last_name")) if x)
            print(f"chat id {c['id']}  ({c.get('type')}: {name})")
        return 0

    notifier = Notifier(dry_run=args.dry_run)
    if args.test_notify:
        if not notifier.channels:
            print("No notification channel configured. See README (Telegram / ntfy / email).", file=sys.stderr)
            return 2
        errs = notifier.send("✅ visa-slot-alerter is set up. You will get a message here when slots appear.")
        for e in errs:
            print("error:", e, file=sys.stderr)
        print("sent to:", ", ".join(notifier.channels))
        return 1 if errs else 0

    cfg = load_config(args.config)
    if not notifier.channels and not args.dry_run:
        print("warning: no notification channel configured; running in dry-run mode", file=sys.stderr)
        notifier.dry_run = True

    if args.portal:
        return run_portal(cfg, notifier, login=args.login, inspect=args.inspect,
                          headless=True if args.headless else None)

    if not args.loop:
        return 1 if run_once(cfg, notifier, args.html_file, args.verbose) < 0 else 0

    interval = max(2.0, args.loop) * 60
    while True:
        try:
            run_once(cfg, notifier, args.html_file, args.verbose)
        except Exception as e:  # never let one bad cycle kill the loop
            print("cycle failed:", e, file=sys.stderr)
        time.sleep(interval)


if __name__ == "__main__":
    sys.exit(main())

# US visa slot alerter (India, H-1B)

Get a Telegram / ntfy / email ping when an H-1B appointment opens at a US
consulate or Visa Application Centre (VAC) in India.

It reads the public, crowdsourced tracker at
[checkvisaslots.com](https://checkvisaslots.com/latest-us-visa-availability/h-1b-regular/)
(updated every ~2 minutes from thousands of applicants' browser extensions),
applies your rules (cities, date window, minimum slots) and notifies you once
per new opening. Booking still happens by you, on the official portal
<https://www.usvisascheduling.com/>.

Two sources, used together:

| Source | Frequency | Touches your account? | Section |
|--------|-----------|-----------------------|---------|
| checkvisaslots.com tracker (crowdsourced) | every 10 min, on GitHub Actions | no | 2 |
| usvisascheduling.com portal (official, logged in) | a few times a day, on your PC | yes | 6 |

The portal is known to hide availability for ~24 h after a handful of calendar
refreshes and to lock accounts for ~72 h when it decides you are a bot, so the
direct checker is deliberately low-frequency and the tracker does the fast
polling.

## 1. Pick a notification channel

Any one is enough; all configured channels receive every alert.

| Channel  | Secrets / env vars                                                   | Notes |
|----------|----------------------------------------------------------------------|-------|
| Telegram | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`                              | Recommended: instant, free, loud on phone |
| ntfy     | `NTFY_TOPIC` (+ optional `NTFY_SERVER`, `NTFY_TOKEN`)                 | Install the ntfy app, subscribe to a random topic name |
| Email    | `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `EMAIL_TO`, optional `EMAIL_FROM` | Gmail: use an App Password with `smtp.gmail.com:587` |

Telegram setup:
1. In Telegram open **@BotFather**, send `/newbot`, follow the prompts, copy the token.
2. Open your new bot and send it any message (e.g. `/start`).
3. Find your chat id:
   ```bash
   TELEGRAM_BOT_TOKEN=123:abc python -m alerter.main --telegram-chat-id
   ```

## 2. Run it for free on GitHub Actions (recommended)

The workflow `.github/workflows/visa-slot-alert.yml` at the repo root runs a
check every 10 minutes and remembers what it already sent via the Actions cache.

1. Push this repo to GitHub (a fork or your own copy is fine).
2. Repo **Settings → Secrets and variables → Actions → New repository secret**:
   add the secrets for your channel from the table above.
3. **Actions** tab → enable workflows if GitHub asks.
4. Smoke test: **Actions → "US visa slot alert" → Run workflow → tick
   "Send a test message"**. You should get "✅ visa-slot-alerter is set up".
5. Edit `visa-slot-alerter/config.yaml` to taste (see below) and commit.

Notes
- Scheduled runs on GitHub can lag 3–10 minutes; that is fine because the
  tracker itself lags real openings by a minute or two.
- A private repo burns ~1 minute of Actions quota per run (~4,300 min/month at
  every-10-minutes). Switch the cron to `*/30 * * * *` to stay inside the free
  2,000 minutes, or make the repo public (unlimited).
- GitHub disables the schedule after 60 days without a commit; a trivial commit
  re-enables it.

## 3. Or run it on your own machine / a small VPS

```bash
cd visa-slot-alerter
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
export TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=...
python -m alerter.main --test-notify          # hello message
python -m alerter.main --loop 10 --verbose    # check every 10 minutes, forever
```
Keep it alive with `nohup`, `tmux`, `systemd` or a cron entry such as
`*/10 * * * * cd /path/visa-slot-alerter && .venv/bin/python -m alerter.main >> alerter.log 2>&1`.

## 4. Tune `config.yaml`

```yaml
visa_types:
  - h-1b-regular              # fresh H-1B stamping (interview + VAC biometrics)
  # - h-1b-interview-waiver   # dropbox renewals
  # - h-4-regular             # dependants
alert:
  locations: [HYDERABAD, CHENNAI, MUMBAI, NEW DELHI, KOLKATA]  # [] = all
  include_consulate: true     # interview rows
  include_vac: true           # "... VAC" rows (biometrics / OFC)
  earliest_on_or_before: null # e.g. "2027-01-31" to ignore far-away dates
  within_days: 120            # and/or: only dates within N days of today
  min_slots: 1
  allow_unknown_slots: true   # VAC rows usually show N/A
  max_sighting_age_minutes: 90   # ignore rows the tracker last saw long ago
  renotify_after_minutes: 180    # remind if the same date is still open later
```

How alerts are deduplicated: for each (visa type, location) the alerter
remembers the last date it told you about and when the tracker saw it. A
**new earliest date** triggers immediately; the **same date seen again** only
re-triggers after `renotify_after_minutes`; the exact same tracker row never
repeats. Slug names come from the tracker's
[index page](https://checkvisaslots.com/latest-us-visa-availability/)
(the last URL path segment).

An alert looks like:

```
🛂 US visa slots spotted
• Hyderabad — consular interview: earliest 05 Nov 2026 (1 slot(s), 2 date(s)) — seen 24 Oct 11:36 UTC [H-1B (Regular)]
• Chennai — biometrics (VAC): earliest 13 Nov 2026 (N/A slot(s), 1 date(s)) — seen 24 Oct 16:59 UTC [H-1B (Regular)]

Book now: https://www.usvisascheduling.com/
Tracker: https://checkvisaslots.com/latest-us-visa-availability/h-1b-regular/
```

## 5. Develop / test

```bash
pip install -r requirements-dev.txt
pytest
# offline end-to-end run against a saved tracker page (file stem = slug):
python -m alerter.main --dry-run -v --html-file tests/fixtures/h-1b-regular.html
```

If the tracker changes its page layout, only `alerter/fetcher.py` needs
updating; the table parser falls back to a text-pattern scan.

## 6. Direct checks on the official portal (optional, your machine only)

`alerter/portal.py` drives a real Chromium window with Playwright and a
persistent profile. You sign in by hand once (Cloudflare check, captcha,
security questions, OTP are never automated), and each later run does exactly
one pass: home → *Reschedule Appointment* → pick your consular post → read the
calendar → close. It never selects applicants, submits or books.

```bash
pip install -r requirements-portal.txt
python -m playwright install chromium          # once
# edit config.yaml -> portal.consular_post (exact dropdown label, e.g. HYDERABAD)
python -m alerter.main --portal --login        # window opens; sign in; it then checks once
python -m alerter.main --portal                # later runs reuse the saved session
python -m alerter.main --portal --inspect      # saves HTML + screenshot to portal-dumps/
```

Safety rails
- `portal.max_checks_per_day` (default 6) is enforced through `state.json`;
  extra runs are skipped. Keep it low: the 24 h "no availability" hide-out
  reportedly kicks in after 3–5 refreshes.
- Schedule it with cron / Task Scheduler at most every few hours, e.g.
  `0 7,10,13,16,19,22 * * *`, on a machine where the browser window can open.
- Do **not** run it on GitHub Actions: no saved session, data-centre IPs are
  blocked, and headless browsers trip the bot check.
- The profile directory holds your logged-in session; it is git-ignored.
  Never commit it or share it.

Calibration: the sign-in form, queue page, menu link and post dropdown
selectors come from a project that verified them against the live site; the
calendar reader tries several common date-picker layouts but has not been
checked against a live account. If a run reports "no selectable dates found",
run once with `--inspect` and adjust `CALENDAR_SELECTORS` in
`alerter/portal.py` using the saved HTML.

Alerts from the portal are separate from tracker alerts: you get one when a
date appears that was not in the previous portal check and that is inside your
`alert` date window.

## Caveats

- The tracker is crowdsourced: an opening shows up only after someone with the
  extension loads that calendar, and it may already be gone when you look. Keep
  the official portal logged in on your phone so you can book within seconds.
- Both VAC (biometrics) and consulate (interview) appointments are needed for a
  regular H-1B; the VAC date must be before the interview date.
- The tracker checker never touches usvisascheduling.com. The optional portal
  checker logs in with *your* saved browser session, reads one calendar, and
  never books. Automating the portal may be against its terms of use; you take
  that risk knowingly, and the lockout is real.

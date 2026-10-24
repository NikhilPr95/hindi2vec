"""Notification channels. Each is enabled by the presence of its environment variables.

  Telegram : TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
  ntfy.sh  : NTFY_TOPIC (optional NTFY_SERVER, default https://ntfy.sh)
  Email    : SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, EMAIL_TO (optional EMAIL_FROM)
"""
from __future__ import annotations

import os
import smtplib
from email.message import EmailMessage

import requests

from .fetcher import Sighting

PORTAL_URL = "https://www.usvisascheduling.com/"
TRACKER_URL = "https://checkvisaslots.com/latest-us-visa-availability/{slug}/"


def format_message(hits: list[Sighting], title: str = "US visa slots spotted") -> str:
    lines = [f"🛂 {title}"]
    for s in hits:
        kind = "biometrics (VAC)" if s.is_vac else "consular interview"
        slots = "N/A" if s.slots is None else str(s.slots)
        seen = s.seen.strftime("%d %b %H:%M UTC")
        lines.append(
            f"• {s.city.title()} — {kind}: earliest {s.earliest.strftime('%d %b %Y')}"
            f" ({slots} slot(s), {s.total_dates or '?'} date(s)) — seen {seen}"
            f" [{s.visa_label}]"
        )
    lines.append("")
    lines.append(f"Book now: {PORTAL_URL}")
    slugs = sorted({s.slug for s in hits})
    for slug in slugs:
        lines.append(f"Tracker: {TRACKER_URL.format(slug=slug)}")
    lines.append("Source is crowdsourced; confirm on the official portal before relying on it.")
    return "\n".join(lines)


class Notifier:
    def __init__(self, env: dict | None = None, dry_run: bool = False):
        self.env = env if env is not None else os.environ
        self.dry_run = dry_run

    @property
    def channels(self) -> list[str]:
        out = []
        if self.env.get("TELEGRAM_BOT_TOKEN") and self.env.get("TELEGRAM_CHAT_ID"):
            out.append("telegram")
        if self.env.get("NTFY_TOPIC"):
            out.append("ntfy")
        if all(self.env.get(k) for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "EMAIL_TO")):
            out.append("email")
        return out

    def send(self, text: str, subject: str = "US visa slot alert") -> list[str]:
        """Send to every configured channel. Returns the list of errors (empty = all good)."""
        errors: list[str] = []
        if self.dry_run:
            print("[dry-run] would send to", self.channels or "nobody", "\n" + text)
            return errors
        for ch in self.channels:
            try:
                getattr(self, f"_send_{ch}")(text, subject)
            except Exception as e:  # one broken channel must not block the others
                errors.append(f"{ch}: {e}")
        return errors

    def _send_telegram(self, text: str, subject: str) -> None:
        token, chat_id = self.env["TELEGRAM_BOT_TOKEN"], self.env["TELEGRAM_CHAT_ID"]
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
            timeout=20,
        )
        r.raise_for_status()

    def _send_ntfy(self, text: str, subject: str) -> None:
        server = self.env.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
        headers = {"Title": subject, "Priority": "high", "Tags": "passport_control"}
        if self.env.get("NTFY_TOKEN"):
            headers["Authorization"] = f"Bearer {self.env['NTFY_TOKEN']}"
        r = requests.post(f"{server}/{self.env['NTFY_TOPIC']}", data=text.encode("utf-8"),
                          headers=headers, timeout=20)
        r.raise_for_status()

    def _send_email(self, text: str, subject: str) -> None:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = self.env.get("EMAIL_FROM", self.env["SMTP_USER"])
        msg["To"] = self.env["EMAIL_TO"]
        msg.set_content(text)
        port = int(self.env.get("SMTP_PORT", "587"))
        with smtplib.SMTP(self.env["SMTP_HOST"], port, timeout=30) as smtp:
            smtp.starttls()
            smtp.login(self.env["SMTP_USER"], self.env["SMTP_PASSWORD"])
            smtp.send_message(msg)


def telegram_chat_ids(token: str) -> list[dict]:
    """Helper: list chats that have messaged the bot (so you can find your chat id)."""
    r = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=20)
    r.raise_for_status()
    seen: dict[int, dict] = {}
    for upd in r.json().get("result", []):
        msg = upd.get("message") or upd.get("channel_post") or {}
        chat = msg.get("chat")
        if chat:
            seen[chat["id"]] = chat
    return list(seen.values())

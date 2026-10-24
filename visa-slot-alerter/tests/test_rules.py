from datetime import datetime, timezone
from pathlib import Path

from alerter.fetcher import parse_page
from alerter.notify import format_message
from alerter.rules import AlertRules, State, evaluate

FIXTURE = Path(__file__).parent / "fixtures" / "h-1b-regular.html"
NOW = datetime(2026, 10, 24, 17, 30, tzinfo=timezone.utc)


def rows():
    return parse_page(FIXTURE.read_text(), "h-1b-regular")


def test_filters_by_city_vac_and_deadline(tmp_path):
    rules = AlertRules.from_dict({
        "locations": ["hyderabad", "chennai"],
        "include_vac": False,
        "earliest_on_or_before": "2026-11-30",
        "max_sighting_age_minutes": 24 * 60,
    })
    hits, skipped = evaluate(rows(), rules, State(tmp_path / "s.json"), now=NOW)
    assert [h.location for h in hits] == ["CHENNAI", "HYDERABAD"]  # sorted by earliest date
    reasons = {s.location: why for s, why in skipped}
    assert reasons["MUMBAI VAC"] == "location not watched"
    assert reasons["CHENNAI VAC"] == "VAC rows disabled"


def test_stale_and_too_few_slots_are_skipped(tmp_path):
    rules = AlertRules.from_dict({"max_sighting_age_minutes": 60, "min_slots": 2,
                                  "allow_unknown_slots": False})
    hits, skipped = evaluate(rows(), rules, State(tmp_path / "s.json"), now=NOW)
    assert [h.location for h in hits] == []  # everything is >1h old at NOW or N/A
    reasons = {s.location: why for s, why in skipped}
    assert reasons["NEW DELHI"].startswith("stale sighting")
    assert reasons["CHENNAI VAC"] == "slot count unknown"

    # with age relaxed, HYDERABAD (1 slot) is below min_slots but CHENNAI (4) passes
    rules.max_sighting_age_minutes = 24 * 60
    hits, skipped = evaluate(rows(), rules, State(tmp_path / "s.json"), now=NOW)
    assert [h.location for h in hits] == ["CHENNAI"]
    assert dict((s.location, why) for s, why in skipped)["HYDERABAD"] == "only 1 slot(s) < min 2"


def test_within_days_deadline(tmp_path):
    rules = AlertRules.from_dict({"within_days": 15, "max_sighting_age_minutes": 24 * 60})
    hits, _ = evaluate(rows(), rules, State(tmp_path / "s.json"), now=NOW)
    # today is 24 Oct; 15 days -> 8 Nov. Only 03 Nov and 05 Nov qualify.
    assert sorted(h.location for h in hits) == ["CHENNAI", "HYDERABAD"]


def test_dedupe_and_renotify(tmp_path):
    path = tmp_path / "state.json"
    rules = AlertRules.from_dict({"locations": ["chennai"], "include_vac": False,
                                  "max_sighting_age_minutes": 24 * 60, "renotify_after_minutes": 180})
    state = State(path)
    hits, _ = evaluate(rows(), rules, state, now=NOW)
    assert len(hits) == 1
    for h in hits:
        state.record_alert(h)
    state.save()

    # Same tracker row again -> no repeat
    state = State(path)
    hits, skipped = evaluate(rows(), rules, state, now=NOW)
    assert hits == []
    assert ("already alerted" in {why for _, why in skipped})

    # Same date, re-seen 1h later -> still quiet (within renotify window)
    html = FIXTURE.read_text().replace("24 Oct 2026, 11:36 AM", "24 Oct 2026, 12:36 PM")
    hits, _ = evaluate(parse_page(html, "h-1b-regular"), rules, State(path), now=NOW)
    assert hits == []

    # Same date, re-seen 4h later -> remind
    html = FIXTURE.read_text().replace("24 Oct 2026, 11:36 AM", "24 Oct 2026, 03:40 PM")
    hits, _ = evaluate(parse_page(html, "h-1b-regular"), rules, State(path), now=NOW)
    assert [h.location for h in hits] == ["CHENNAI"]

    # Earlier date appears -> alert immediately regardless of time
    html = FIXTURE.read_text().replace("03 Nov, 26", "28 Oct, 26")
    hits, _ = evaluate(parse_page(html, "h-1b-regular"), rules, State(path), now=NOW)
    assert [h.earliest_date for h in hits] == ["2026-10-28"]


def test_message_format(tmp_path):
    rules = AlertRules.from_dict({"locations": ["chennai"], "max_sighting_age_minutes": 24 * 60})
    hits, _ = evaluate(rows(), rules, State(tmp_path / "s.json"), now=NOW)
    text = format_message(hits)
    assert "Chennai — consular interview: earliest 03 Nov 2026 (4 slot(s)" in text
    assert "Chennai — biometrics (VAC): earliest 13 Nov 2026 (N/A slot(s)" in text
    assert "https://www.usvisascheduling.com/" in text
    assert "h-1b-regular" in text

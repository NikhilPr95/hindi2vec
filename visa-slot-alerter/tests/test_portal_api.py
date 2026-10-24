import json
from urllib.parse import parse_qs, urlparse

from alerter.portal_api import Recipe, RecipeBook, parse_schedule_days, parse_schedule_entries


def make_recipe(**over):
    base = dict(
        kind="consular",
        url="https://www.usvisascheduling.com/en-US/reschedule/?route=%2Fschedule-group%2Fget-family-consular-schedule-days&cacheString=1700000000000",
        method="POST",
        headers={"content-type": "application/x-www-form-urlencoded; charset=UTF-8", "x-requested-with": "XMLHttpRequest"},
        body="parameters=%7B%22postId%22%3A%22abc-123%22%7D&__RequestVerificationToken=tok",
        post_id="abc-123", post_label="HYDERABAD", captured_at="2026-10-24T00:00:00+00:00",
    )
    base.update(over)
    return Recipe(**base)


def test_parse_schedule_days_dedupes_and_sorts():
    payload = {"ScheduleDays": [{"Date": "2026-11-05"}, {"Date": "2026-11-03T00:00:00"}, {"Date": "2026-11-05"}, {"Date": "bad"}, None]}
    assert parse_schedule_days(payload) == ["2026-11-03", "2026-11-05"]
    assert parse_schedule_days(json.dumps(payload)) == ["2026-11-03", "2026-11-05"]
    assert parse_schedule_days("not json") == []
    assert parse_schedule_days({"ScheduleDays": None}) == []


def test_parse_schedule_entries():
    payload = {"ScheduleEntries": [{"Time": "09:00 AM", "EntriesAvailable": 3}, {"Time": "10:00 AM", "EntriesAvailable": "1"}, {"bogus": 1}]}
    assert parse_schedule_entries(payload) == [("09:00 AM", 3), ("10:00 AM", 1)]


def test_cache_buster_replaces_only_cachestring():
    r = make_recipe()
    u = urlparse(r.with_cache_buster())
    q = parse_qs(u.query)
    assert q["route"] == ["/schedule-group/get-family-consular-schedule-days"]
    assert q["cacheString"][0] != "1700000000000" and q["cacheString"][0].isdigit()
    assert u.path == "/en-US/reschedule/"


def test_body_for_swaps_post_id_and_keeps_other_fields():
    r = make_recipe()
    body = parse_qs(r.body_for("xyz-9"))
    assert json.loads(body["parameters"][0]) == {"postId": "xyz-9"}
    assert body["__RequestVerificationToken"] == ["tok"]
    body = parse_qs(r.body_for("xyz-9", scheduleDate="2026-11-03"))
    assert json.loads(body["parameters"][0]) == {"postId": "xyz-9", "scheduleDate": "2026-11-03"}


def test_recipe_book_roundtrip(tmp_path):
    book = RecipeBook(captured_at="t", page_url="u", posts={"HYDERABAD": "abc-123", "CHENNAI": "def"},
                      recipes=[make_recipe().__dict__])
    book.save(tmp_path / "r.json")
    loaded = RecipeBook.load(tmp_path / "r.json")
    assert loaded.post_id("hyderabad") == "abc-123"
    assert loaded.post_id("mumbai") is None
    assert loaded.recipe("consular").post_label == "HYDERABAD"
    assert loaded.recipe("ofc") is None

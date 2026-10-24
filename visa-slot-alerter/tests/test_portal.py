import os
from datetime import datetime, timezone

import pytest

from alerter.portal import QUEUE_MARKERS, Throttle, read_calendar, select_post, wait_through_queue

pw = pytest.importorskip("playwright.sync_api")


@pytest.fixture(scope="module")
def page():
    with pw.sync_playwright() as p:
        kwargs = {}
        if os.environ.get("PW_CHROMIUM"):
            kwargs["executable_path"] = os.environ["PW_CHROMIUM"]
        browser = p.chromium.launch(headless=True, **kwargs)
        pg = browser.new_page()
        yield pg
        browser.close()


def test_throttle_caps_checks_per_day(tmp_path):
    t = Throttle(tmp_path / "state.json", max_per_day=2)
    now = datetime(2026, 10, 24, 9, tzinfo=timezone.utc)
    assert t.allow(now)
    t.record(now, None)
    assert t.allow(now)
    t.record(now, None)
    assert not t.allow(now)
    # new day resets the counter, and the count survives a reload
    assert Throttle(tmp_path / "state.json", 2).allow(datetime(2026, 10, 25, tzinfo=timezone.utc))
    assert not Throttle(tmp_path / "state.json", 2).allow(now)


def test_read_calendar_bootstrap_datepicker(page):
    page.set_content("""
      <div class="datepicker"><div class="datepicker-switch">November 2026</div>
      <table><tr>
        <td class="day old disabled">31</td>
        <td class="day disabled">1</td>
        <td class="day">3</td>
        <td class="day">5</td>
        <td class="day new">1</td>
      </tr></table></div>""")
    assert read_calendar(page) == ["2026-11-03", "2026-11-05"]


def test_read_calendar_data_date_attributes(page):
    page.set_content("""<div class="calendar">
        <button data-date="2026-12-01">1</button>
        <button data-date="2026-12-09" >9</button>
        <button disabled data-date="2026-12-10">10</button></div>""")
    assert read_calendar(page) == ["2026-12-01", "2026-12-09"]


def test_read_calendar_empty_when_nothing_selectable(page):
    page.set_content("<p>No appointments available</p><table><tr><td class='day disabled'>2</td></tr></table>")
    assert read_calendar(page) == []


def test_select_post_picks_matching_option(page):
    page.set_content("""
      <select id="other"><option value="">--</option><option value="x">Something</option></select>
      <select id="post"><option value="">Select</option><option value="7">HYDERABAD</option></select>""")
    assert select_post(page, "hyderabad") is True
    assert page.eval_on_selector("#post", "s => s.value") == "7"
    assert select_post(page, "TORONTO") is False


def test_queue_markers_and_wait(page):
    page.set_content("<p>Welcome back</p>")
    assert wait_through_queue(page, max_minutes=0.01) is True
    page.set_content("<p>You are now in line. Your estimated wait time is 3 minutes.</p>")
    assert any(m.search("You are now in line") for m in QUEUE_MARKERS)
    assert wait_through_queue(page, max_minutes=0.001) is False

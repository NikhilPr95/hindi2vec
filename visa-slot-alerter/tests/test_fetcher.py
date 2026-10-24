from pathlib import Path

from alerter.fetcher import parse_earliest, parse_page, parse_seen

FIXTURE = Path(__file__).parent / "fixtures" / "h-1b-regular.html"


def test_parse_table_rows():
    rows = parse_page(FIXTURE.read_text(), "h-1b-regular")
    assert len(rows) == 8
    chennai = next(r for r in rows if r.location == "CHENNAI")
    assert chennai.visa_label == "H-1B (Regular)"
    assert chennai.earliest_date == "2026-11-03"
    assert chennai.slots == 4
    assert chennai.total_dates == 2
    assert chennai.seen_utc == "2026-10-24T11:36:00+00:00"
    assert not chennai.is_vac and chennai.city == "CHENNAI"

    vac = next(r for r in rows if r.location == "HYDERABAD VAC")
    assert vac.is_vac and vac.city == "HYDERABAD"
    assert vac.slots is None
    assert vac.earliest_date == "2027-10-15"


def test_fallback_text_parsing_without_table():
    html = """<div>
    <div>HYDERABAD VAC</div><div>H-1B (Regular)</div><div>05 Nov, 26</div>
    <div>N/A</div><div>1</div><div>24 Oct 2026, 11:36 AM</div><div>9 hrs ago</div>
    </div>"""
    rows = parse_page(html, "h-1b-regular")
    assert len(rows) == 1
    assert rows[0].location == "HYDERABAD VAC"
    assert rows[0].earliest_date == "2026-11-05"


def test_date_parsers_tolerate_variants():
    assert parse_earliest("03 Nov, 26").isoformat() == "2026-11-03"
    assert parse_earliest("3 Nov 2026").isoformat() == "2026-11-03"
    assert parse_earliest("garbage") is None
    assert parse_seen("24 Oct 2026, 04:59 PM").isoformat() == "2026-10-24T16:59:00+00:00"
    assert parse_seen("nope") is None


def test_parse_api_payload():
    from alerter.fetcher import parse_api
    payload = {"visa_type": "H-1B (Regular)", "slotDetails": [
        {"visa_location": "HYDERABAD VAC", "createdon": "2026-10-24T16:59:00Z", "slots": 2, "start_date": "2026-11-13"},
        {"visa_location": "CHENNAI", "createdon": "2026-10-24 11:36:00+00:00", "slots": "4", "start_date": "2026-11-03"},
        {"visa_location": "", "createdon": "x"}, {"visa_location": "MUMBAI", "createdon": "", "start_date": ""},
    ]}
    rows = parse_api(payload)
    assert [(r.location, r.earliest_date, r.total_dates, r.visa_label) for r in rows] == [
        ("HYDERABAD VAC", "2026-11-13", 2, "H-1B (Regular)"), ("CHENNAI", "2026-11-03", 4, "H-1B (Regular)")]
    assert rows[0].seen_utc == "2026-10-24T16:59:00+00:00"

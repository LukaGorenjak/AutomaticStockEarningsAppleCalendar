"""Tests for the SEOnet LJSE financial calendar watcher (offline, fixture-based)."""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from earnings_cal.models import load_config
from earnings_cal.seonet.cli import main as seonet_main
from earnings_cal.seonet.cli import run_seonet
from earnings_cal.seonet.dates import extract_candidate_dates
from earnings_cal.seonet.documents import (
    AnnouncementContent,
    DocumentError,
    ExtractedAttachment,
    extract_attachment_file,
    extract_from_announcement_html,
)
from earnings_cal.seonet.feed import FeedError, FeedItem, parse_feed
from earnings_cal.seonet.filter import filter_items, is_financial_calendar_title
from earnings_cal.seonet.report import render_announcement_report
from earnings_cal.seonet.state import load_seen, save_seen

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def test_parse_real_feed_fixtures() -> None:
    """Real SEOnet RSS XML fixtures parse cleanly with expected IDs, issuers, and titles."""
    rss_all_items = parse_feed((FIXTURES_DIR / "rss_all.xml").read_bytes())
    assert len(rss_all_items) == 20

    nlb_en = next(item for item in rss_all_items if item.id == "93283")
    assert nlb_en.issuer == "NLB, d. d., Ljubljana"
    assert nlb_en.title == "NLB publishes Financial Calendar for 2027"
    assert nlb_en.link == "https://seonet.ljse.si/?doc_id=93283"
    assert nlb_en.published == "Fri, 06 Nov 2026 10:05:26 GMT"
    assert nlb_en.instrument == "NLBR"

    nlb_sl = next(item for item in rss_all_items if item.id == "93282")
    assert nlb_sl.title == "NLB objavlja finančni koledar za leto 2027"

    krka = next(item for item in rss_all_items if item.id == "93285")
    assert krka.issuer == "KRKA, d. d., Novo mesto"
    assert krka.instrument == "KRKG"

    rss_en_items = parse_feed((FIXTURES_DIR / "rss_en.xml").read_bytes())
    assert len(rss_en_items) == 11
    assert any(item.id == "93283" for item in rss_en_items)

    with pytest.raises(FeedError):
        parse_feed(b"<not-rss><broken/></not-rss>")

    with pytest.raises(FeedError):
        parse_feed(b"<rss version='2.0'></rss>")


def test_filter_titles_and_watchlist_issuers() -> None:
    """Filter matches Slovene/English financial calendar titles and ignores non-watchlist issuers."""
    assert is_financial_calendar_title("NLB publishes Financial Calendar for 2027")
    assert is_financial_calendar_title("NLB objavlja finančni koledar za leto 2027")
    assert is_financial_calendar_title("Sprememba finančnega koledarja 2026")
    assert is_financial_calendar_title("Financni koledar 2027")
    assert is_financial_calendar_title("OBJAVA FINANČNEGA KOLEDARJA CINKARNE CELJE, D.D.")

    assert not is_financial_calendar_title("Acquisition of treasury shares")
    assert not is_financial_calendar_title("Trgovalni koledar za leto 2027")
    assert not is_financial_calendar_title("Issuance of notes")

    items = parse_feed((FIXTURES_DIR / "rss_all.xml").read_bytes())
    # Watch KRKA only -> KRKA has no financial calendar in rss_all.xml
    assert filter_items(items, {"KRKG": "KRKA, d. d., Novo mesto"}) == []

    # Watch NLB -> matches both financial calendars (93282, 93283) and AGM/dividend notice (93270)
    nlb_matches = filter_items(items, {"NLBR": "NLB, d. d., Ljubljana"})
    assert [m.item.id for m in nlb_matches] == ["93282", "93283", "93270"]
    assert [m.category for m in nlb_matches] == [
        "financial_calendar",
        "financial_calendar",
        "dividend_agm",
    ]
    assert all(m.ticker == "NLBR" for m in nlb_matches)


def test_config_validation_for_ljse_issuers(tmp_path: Path) -> None:
    """Config validates ljse_issuers and fails clearly on unknown or malformed entries."""
    good = tmp_path / "tickers.yaml"
    good.write_text(
        'tickers:\n  - AAPL\nljse_issuers:\n  krkg: "KRKA, d. d., Novo mesto"\n',
        encoding="utf-8",
    )
    cfg = load_config(good)
    assert cfg.ljse_issuers == {"KRKG": "KRKA, d. d., Novo mesto"}

    unknown_key = tmp_path / "bad_key.yaml"
    unknown_key.write_text("tickers: []\nunknown_section: 123\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Unknown configuration key"):
        load_config(unknown_key)

    bad_type = tmp_path / "bad_type.yaml"
    bad_type.write_text("ljse_issuers:\n  - KRKG\n", encoding="utf-8")
    with pytest.raises(ValueError, match="must be a mapping"):
        load_config(bad_type)

    empty_name = tmp_path / "empty_name.yaml"
    empty_name.write_text('ljse_issuers:\n  KRKG: ""\n', encoding="utf-8")
    with pytest.raises(ValueError, match="non-empty string"):
        load_config(empty_name)


def test_dates_extraction_formats_and_ambiguous_lines() -> None:
    """Extract numeric, written/inflected Slovene months, ranges, and handle no-date text."""
    sample_text = "\n".join(
        [
            "Finančni koledar 2027",
            "Objava letnega poročila: 30. 3. 2027",
            "Skupščina delničarjev: 30.03.2027",
            "Polletno poročilo bo objavljeno 30. marca 2027 po zaprtju borze.",
            "Prvo četrtletje: 15. januarja 2027",
            "Drugo četrtletje: 5. maja 2027",
            "Izplačilo dividend: 12. avgustu 2027",
            "Napoved za leto: 20. decembrom 2027",
            "20.10. do 19.11. 2026 – tiho obdobje",
            "• Objava preliminarnih rezultatov: prihodki matične družbe in",
            "12.01.2026",
            "ladijskega pretovora za leto 2025",
            "Ta vrstica nima nobenega datuma (le številka 2027 ali 3.14).",
        ]
    )

    candidates = extract_candidate_dates(sample_text)
    extracted_dates = [c.date for c in candidates]

    assert extracted_dates == [
        date(2027, 3, 30),
        date(2027, 3, 30),
        date(2027, 3, 30),
        date(2027, 1, 15),
        date(2027, 5, 5),
        date(2027, 8, 12),
        date(2027, 12, 20),
        date(2026, 10, 20),
        date(2026, 11, 19),
        date(2026, 1, 12),
    ]

    # Standalone date line pulls surrounding label context and preserves exact raw_line
    standalone = candidates[-1]
    assert standalone.raw_line == "12.01.2026"
    assert "Objava preliminarnih rezultatov" in standalone.label_context
    assert "ladijskega pretovora" in standalone.label_context

    # dict representation check
    as_dict = candidates[0].to_dict()
    assert set(as_dict.keys()) == {"date", "label_context", "raw_line"}

    # Text with no dates returns empty list
    assert extract_candidate_dates("Brez datumov in brez koledarja.") == []


def test_real_announcement_html_and_pdf_extraction() -> None:
    """Real Luka Koper and NLB HTML/PDF fixtures extract dates or report external links."""
    lkpg_html = (FIXTURES_DIR / "doc_92849.html").read_text(encoding="utf-8")
    lkpg_content = extract_from_announcement_html(
        html_text=lkpg_html,
        announcement_url="https://seonet.ljse.si/?doc_id=92849",
        local_attachment_map={
            "https://seonet.ljse.si/file.aspx?AttachmentID=56105": FIXTURES_DIR / "att_56105.pdf",
        },
    )
    assert len(lkpg_content.attachments) == 1
    assert lkpg_content.attachments[0].status == "extracted"
    lkpg_dates = extract_candidate_dates(lkpg_content.combined_text)
    assert any(c.date == date(2026, 11, 19) for c in lkpg_dates)
    assert any(c.date == date(2026, 8, 31) for c in lkpg_dates)

    # NLB fixture has no attachments, only an external link in body
    nlb_html = (FIXTURES_DIR / "doc_93283.html").read_text(encoding="utf-8")
    nlb_content = extract_from_announcement_html(
        html_text=nlb_html,
        announcement_url="https://seonet.ljse.si/?doc_id=93283",
    )
    assert nlb_content.attachments == []
    assert "https://www.nlbgroup.com/int-en/investor-relations/financial-calendar" in nlb_content.external_links


def test_scanned_pdf_and_unsupported_attachment_handling(tmp_path: Path) -> None:
    """Scanned PDF reports 'needs_manual_reading' and unsupported files report 'unsupported'."""
    scanned_pdf = tmp_path / "scanned.pdf"
    scanned_pdf.write_bytes(b"%PDF-1.4\n%fake\n")

    with patch("earnings_cal.seonet.documents.extract_pdf_text", return_value=""):
        att = extract_attachment_file(scanned_pdf, url="https://seonet.ljse.si/file.aspx?AttachmentID=1")
    assert att.status == "needs_manual_reading"
    assert "needs manual reading" in att.note.lower()

    xlsx_file = tmp_path / "calendar.xlsx"
    xlsx_file.write_bytes(b"PK\x03\x04dummy-xlsx")
    att_xlsx = extract_attachment_file(
        xlsx_file,
        url="https://seonet.ljse.si/file.aspx?AttachmentID=2",
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    assert att_xlsx.status == "unsupported"


def test_report_markdown_contains_valid_parseable_yaml() -> None:
    """Rendered Markdown report contains a valid YAML snippet with type: TODO and source_url."""
    lkpg_html = (FIXTURES_DIR / "doc_92849.html").read_text(encoding="utf-8")
    content = extract_from_announcement_html(
        html_text=lkpg_html,
        announcement_url="https://seonet.ljse.si/?doc_id=92849",
        local_attachment_map={
            "https://seonet.ljse.si/file.aspx?AttachmentID=56105": FIXTURES_DIR / "att_56105.pdf",
        },
    )
    candidates = extract_candidate_dates(content.combined_text)
    items = [
        FeedItem(
            id="92849",
            issuer="LUKA KOPER, d.d., Koper",
            title="Sprememba finančnega koledarja 2026",
            published="Wed, 03 Jun 2026 15:05:00 GMT",
            link="https://seonet.ljse.si/?doc_id=92849",
        )
    ]
    match = filter_items(items, {"LKPG": "LUKA KOPER, d.d., Koper"})[0]
    report = render_announcement_report(match, content, candidates)

    assert "<!-- seonet-doc-id: 92849 -->" in report
    assert "# New financial calendar: LKPG" in report
    assert "LUKA KOPER, d.d., Koper" in report
    assert "https://seonet.ljse.si/?doc_id=92849" in report

    yaml_match = re.search(r"```yaml\n(.*?)\n```", report, re.DOTALL)
    assert yaml_match is not None
    parsed = yaml.safe_load(yaml_match.group(1))
    assert isinstance(parsed, dict)
    events = parsed["ljse_events"]
    assert len(events) == len(candidates)
    assert all(ev["ticker"] == "LKPG" for ev in events)
    assert all(ev["type"] == "TODO" for ev in events)
    assert all(ev["source_url"] == "https://seonet.ljse.si/?doc_id=92849" for ev in events)

    # Scanned PDF report still emits valid YAML and explicitly notes manual reading
    scanned_content = AnnouncementContent(
        url="https://seonet.ljse.si/?doc_id=99999",
        body_text="",
        attachments=[
            ExtractedAttachment(
                filename="scan.pdf",
                url="https://seonet.ljse.si/file.aspx?AttachmentID=9",
                content_type="application/pdf",
                status="needs_manual_reading",
                note="Scanned PDF with no extractable text — needs manual reading.",
            )
        ],
    )
    scanned_report = render_announcement_report(match, scanned_content, [])
    assert "needs manual reading" in scanned_report.lower()
    scanned_yaml_match = re.search(r"```yaml\n(.*?)\n```", scanned_report, re.DOTALL)
    assert scanned_yaml_match is not None
    scanned_parsed = yaml.safe_load(scanned_yaml_match.group(1))
    assert scanned_parsed["ljse_events"][0]["type"] == "TODO"


def test_state_not_reported_twice_and_failed_retried(tmp_path: Path) -> None:
    """Processed items are not reported twice; failed announcements are retried next run."""
    cfg_path = tmp_path / "tickers.yaml"
    cfg_path.write_text(
        'ljse_issuers:\n  LKPG: "LUKA KOPER, d.d., Koper"\n  TLSG: "TELEKOM SLOVENIJE, d.d., Ljubljana"\n',
        encoding="utf-8",
    )
    state_path = tmp_path / "state" / "seonet_seen.json"
    reports_dir = tmp_path / "reports"

    feed_items = [
        FeedItem(
            id="92849",
            issuer="LUKA KOPER, d.d., Koper",
            title="Sprememba finančnega koledarja 2026",
            published="Wed, 03 Jun 2026 15:05:00 GMT",
            link="https://seonet.ljse.si/?doc_id=92849",
        ),
        FeedItem(
            id="92747",
            issuer="TELEKOM SLOVENIJE, d.d., Ljubljana",
            title="Sprememba Finančnega koledarja družbe Telekom Slovenije, d.d., za leto 2026",
            published="Thu, 14 May 2026 07:40:00 GMT",
            link="https://seonet.ljse.si/?doc_id=92747",
        ),
    ]

    attempt_state = {"fail_tlsg": True}

    def fake_content_fetcher(url: str) -> AnnouncementContent:
        if "92747" in url and attempt_state["fail_tlsg"]:
            raise DocumentError("Temporary download error")
        if "92849" in url:
            html = (FIXTURES_DIR / "doc_92849.html").read_text(encoding="utf-8")
            return extract_from_announcement_html(
                html,
                url,
                local_attachment_map={
                    "https://seonet.ljse.si/file.aspx?AttachmentID=56105": FIXTURES_DIR / "att_56105.pdf"
                },
            )
        html = (FIXTURES_DIR / "doc_92747.html").read_text(encoding="utf-8")
        return extract_from_announcement_html(
            html,
            url,
            local_attachment_map={
                "https://seonet.ljse.si/file.aspx?AttachmentID=56031": FIXTURES_DIR / "att_56031.pdf"
            },
        )

    # Run 1: LKPG succeeds, TLSG fails -> exit 1, LKPG marked seen, TLSG NOT marked seen
    rc1 = run_seonet(
        config_path=cfg_path,
        state_path=state_path,
        reports_dir=reports_dir,
        feed_fetcher=lambda: feed_items,
        content_fetcher=fake_content_fetcher,
    )
    assert rc1 == 1
    assert load_seen(state_path) == {"92849"}
    assert (reports_dir / "LKPG_92849.md").exists()
    assert not (reports_dir / "TLSG_92747.md").exists()

    # Run 2: TLSG recovers -> LKPG is NOT re-reported, TLSG succeeds and is marked seen
    (reports_dir / "LKPG_92849.md").unlink()
    attempt_state["fail_tlsg"] = False
    rc2 = run_seonet(
        config_path=cfg_path,
        state_path=state_path,
        reports_dir=reports_dir,
        feed_fetcher=lambda: feed_items,
        content_fetcher=fake_content_fetcher,
    )
    assert rc2 == 0
    assert load_seen(state_path) == {"92849", "92747"}
    assert not (reports_dir / "LKPG_92849.md").exists()
    assert (reports_dir / "TLSG_92747.md").exists()

    # Run 3: Zero new matches -> exit 0, no new reports
    (reports_dir / "TLSG_92747.md").unlink()
    rc3 = run_seonet(
        config_path=cfg_path,
        state_path=state_path,
        reports_dir=reports_dir,
        feed_fetcher=lambda: feed_items,
        content_fetcher=fake_content_fetcher,
    )
    assert rc3 == 0
    assert list(reports_dir.glob("*.md")) == []

    # Deterministic sorted keys check
    raw_json = json.loads(state_path.read_text(encoding="utf-8"))
    assert raw_json == {"seen_ids": ["92747", "92849"]}
    assert save_seen({"92747", "92849"}, state_path) is False


def test_feed_failure_does_not_update_state(tmp_path: Path) -> None:
    """When RSS feed fails, exit non-zero and leave state untouched."""
    cfg_path = tmp_path / "tickers.yaml"
    cfg_path.write_text('ljse_issuers:\n  NLBR: "NLB, d. d., Ljubljana"\n', encoding="utf-8")
    state_path = tmp_path / "seonet_seen.json"
    save_seen({"100"}, state_path)

    def broken_feed() -> list[FeedItem]:
        raise FeedError("Network unreachable")

    rc = run_seonet(
        config_path=cfg_path,
        state_path=state_path,
        reports_dir=tmp_path / "reports",
        feed_fetcher=broken_feed,
    )
    assert rc != 0
    assert load_seen(state_path) == {"100"}


def test_cli_against_saved_fixtures(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """CLI `python -m earnings_cal.seonet` runs cleanly against saved fixtures."""
    cfg_path = tmp_path / "tickers.yaml"
    cfg_path.write_text('ljse_issuers:\n  NLBR: "NLB, d. d., Ljubljana"\n', encoding="utf-8")
    reports_dir = tmp_path / "reports"
    state_path = tmp_path / "seonet_seen.json"

    rc = seonet_main(
        [
            "--config",
            str(cfg_path),
            "--state",
            str(state_path),
            "--reports-dir",
            str(reports_dir),
            "--feed-file",
            str(FIXTURES_DIR / "rss_en.xml"),
            "--fixtures-dir",
            str(FIXTURES_DIR),
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "# New financial calendar: NLBR" in out
    assert "https://www.nlbgroup.com/int-en/investor-relations/financial-calendar" in out
    assert (reports_dir / "NLBR_93283.md").exists()
    assert (reports_dir / "NLBR_93268.md").exists()
    assert load_seen(state_path) == {"93268", "93283"}


def test_ljse_search_parsing_and_earnings_classification() -> None:
    """Automatic LJSE scraper parses search HTML and filters real KRKG/CICG PDFs cleanly."""
    from icalendar import Calendar
    from earnings_cal.main import run as run_calendar
    from earnings_cal.models import EarningsEvent
    from earnings_cal.seonet.ljse import (
        extract_ljse_earnings_events,
        load_ljse_cache,
        parse_search_results,
        parse_symbol_map,
        save_ljse_cache,
    )

    # Symbol dropdown parsing
    sample_select = '<select name="field.symbol"><option value="110">KRKG</option><option value="305">CICG</option></select>'
    assert parse_symbol_map(sample_select) == {"KRKG": "110", "CICG": "305"}

    # Real search result parsing on KRKG & CICG fixtures
    krkg_rows = parse_search_results((FIXTURES_DIR / "krkg_koledar.html").read_text(encoding="utf-8"))
    assert krkg_rows[0][0] == "92042"
    assert "Finančni koledar za leto 2026" in krkg_rows[0][3]

    cicg_rows = parse_search_results((FIXTURES_DIR / "cicg_koledar.html").read_text(encoding="utf-8"))
    assert cicg_rows[0][0] == "91972"

    # Real KRKG 2026 PDF extraction -> keeps only earnings reports, drops quiet periods & dividends/AGM
    krkg_html = (FIXTURES_DIR / "doc_92042.html").read_text(encoding="utf-8")
    krkg_content = extract_from_announcement_html(
        krkg_html,
        "https://seonet.ljse.si/?doc_id=92042",
        local_attachment_map={
            "https://seonet.ljse.si/file.aspx?AttachmentID=55582": FIXTURES_DIR / "att_55582.pdf",
        },
    )
    krkg_events = extract_ljse_earnings_events(
        ticker="KRKG",
        announcement_url="https://seonet.ljse.si/?doc_id=92042",
        text=krkg_content.combined_text,
        start_date=date(2026, 1, 1),
        end_date=date(2026, 12, 31),
    )
    assert [e.date for e in krkg_events] == [
        date(2026, 1, 29),
        date(2026, 3, 12),
        date(2026, 4, 9),
        date(2026, 5, 14),
        date(2026, 7, 23),
        date(2026, 11, 12),
    ]

    # Real CICG 2026 PDF extraction
    cicg_html = (FIXTURES_DIR / "doc_91972.html").read_text(encoding="utf-8")
    cicg_content = extract_from_announcement_html(
        cicg_html,
        "https://seonet.ljse.si/?doc_id=91972",
        local_attachment_map={
            "https://seonet.ljse.si/file.aspx?AttachmentID=55535": FIXTURES_DIR / "att_55535.pdf",
        },
    )
    cicg_events = extract_ljse_earnings_events(
        ticker="CICG",
        announcement_url="https://seonet.ljse.si/?doc_id=91972",
        text=cicg_content.combined_text,
        start_date=date(2026, 11, 10),
        end_date=date(2027, 11, 10),
    )
    assert [e.date for e in cicg_events] == [date(2026, 11, 11)]


def test_tickers_ljse_cache_and_calendar_merge(tmp_path: Path) -> None:
    """`tickers-ljse` cache merges into docs/earnings.ics and drops removed tickers."""
    from icalendar import Calendar
    from earnings_cal.main import run as run_calendar
    from earnings_cal.models import EarningsEvent
    from earnings_cal.seonet.ljse import load_ljse_cache, save_ljse_cache

    cfg_path = tmp_path / "tickers.yaml"
    cfg_path.write_text("tickers-ljse:\n  - KRKG\n  - CICG\n", encoding="utf-8")
    cache_path = tmp_path / "state" / "ljse_cache.json"
    out_ics = tmp_path / "docs" / "earnings.ics"

    save_ljse_cache(
        [
            EarningsEvent(ticker="KRKG", date=date(2026, 11, 12), note="Q1-Q3 2026 report"),
            EarningsEvent(ticker="CICG", date=date(2026, 11, 11), note="Q1-Q3 2026 report"),
            EarningsEvent(ticker="TLSG", date=date(2026, 11, 12), note="Removed ticker"),
        ],
        cache_path,
    )
    assert [e.ticker for e in load_ljse_cache(cache_path, allowed_tickers=["KRKG", "CICG"])] == [
        "CICG",
        "KRKG",
    ]

    rc = run_calendar(
        config_path=cfg_path,
        output_path=out_ics,
        today=date(2026, 11, 10),
        ljse_cache_path=cache_path,
    )
    assert rc == 0
    cal = Calendar.from_ical(out_ics.read_bytes())
    summaries = [str(c.get("summary")) for c in cal.walk() if c.name == "VEVENT"]
    assert summaries == ["CICG earnings", "KRKG earnings"]


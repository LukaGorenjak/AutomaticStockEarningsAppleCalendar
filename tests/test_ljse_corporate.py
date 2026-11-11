"""Tests for LJSE dividend/AGM corporate events, validation, and SEOnet dividend fixtures."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
import yaml

from earnings_cal.markets.ljse import LJSEValidationError, load_ljse_events_yaml
from earnings_cal.models import Event, EventKind
from earnings_cal.seonet.dates import extract_candidate_dates, extract_dividend_amounts
from earnings_cal.seonet.documents import extract_from_announcement_html
from earnings_cal.seonet.feed import FeedItem
from earnings_cal.seonet.filter import (
    classify_announcement_title,
    filter_items,
    is_dividend_or_agm_title,
)
from earnings_cal.seonet.report import render_announcement_report

FIXTURES_DIR = Path(__file__).parent / "fixtures"
REPO_ROOT = Path(__file__).resolve().parent.parent


def test_repo_ljse_events_yaml_validates_all_corporate_kinds() -> None:
    """Repository ljse_events.yaml parses cleanly and covers all five corporate event types."""
    events = load_ljse_events_yaml(REPO_ROOT / "ljse_events.yaml")
    kinds = {ev.kind for ev in events}
    assert {
        EventKind.DIVIDEND_DECISION,
        EventKind.EX_DIVIDEND,
        EventKind.RECORD_DATE,
        EventKind.DIVIDEND_PAYMENT,
        EventKind.AGM,
    }.issubset(kinds)

    krkg_dec = next(
        ev for ev in events if ev.ticker == "KRKG" and ev.kind == EventKind.DIVIDEND_DECISION
    )
    assert "Dividend Amount: 9.10 EUR per share" in krkg_dec.description
    assert krkg_dec.uid == "ljse-KRKG-2026-07-09-dividend_decision@earnings-cal"

    nlbr_ex = next(ev for ev in events if ev.ticker == "NLBR" and ev.kind == EventKind.EX_DIVIDEND)
    nlbr_rec = next(ev for ev in events if ev.ticker == "NLBR" and ev.kind == EventKind.RECORD_DATE)
    assert nlbr_ex.date < nlbr_rec.date
    assert "Dividend Amount: 6.92 EUR per share" in nlbr_ex.description


@pytest.mark.parametrize(
    ("yaml_block", "error_pattern"),
    [
        # dividend_decision missing amount_per_share
        (
            """
ljse_events:
  - ticker: KRKG
    date: "2026-07-09"
    kind: dividend_decision
    source_url: "https://seonet.ljse.si/?doc_id=92979"
""",
            "amount_per_share",
        ),
        # ex_dividend missing source_url
        (
            """
ljse_events:
  - ticker: NLBR
    date: "2026-12-18"
    kind: ex_dividend
""",
            "source_url",
        ),
        # record_date invalid source_url
        (
            """
ljse_events:
  - ticker: CICG
    date: "2026-06-29"
    kind: record_date
    source_url: "not-a-url"
""",
            "http\\(s\\) source_url",
        ),
        # dividend_payment non-positive amount_per_share
        (
            """
ljse_events:
  - ticker: LKPG
    date: "2026-08-31"
    kind: dividend_payment
    amount_per_share: 0
    source_url: "https://seonet.ljse.si/?doc_id=92821"
""",
            "must be positive",
        ),
        # agm missing source_url
        (
            """
ljse_events:
  - ticker: KRKG
    date: "2027-07-08"
    kind: agm
""",
            "source_url",
        ),
    ],
)
def test_validation_per_event_type_required_fields(
    tmp_path: Path,
    yaml_block: str,
    error_pattern: str,
) -> None:
    """Each corporate event kind enforces its required fields and rejects malformed values."""
    bad_file = tmp_path / "ljse_events.yaml"
    bad_file.write_text(yaml_block, encoding="utf-8")
    with pytest.raises(LJSEValidationError, match=error_pattern):
        load_ljse_events_yaml(bad_file)


def test_ex_dividend_vs_record_date_safety_and_computed_flag(tmp_path: Path) -> None:
    """Reject equal/inverted ex_dividend vs record_date and require flagging for computed dates."""
    same_date = tmp_path / "same.yaml"
    same_date.write_text(
        """
ljse_events:
  - ticker: NLBR
    date: "2026-12-21"
    kind: ex_dividend
    source_url: "https://seonet.ljse.si/?doc_id=93272"
  - ticker: NLBR
    date: "2026-12-21"
    kind: record_date
    source_url: "https://seonet.ljse.si/?doc_id=93272"
""",
        encoding="utf-8",
    )
    with pytest.raises(LJSEValidationError, match="must precede record_date"):
        load_ljse_events_yaml(same_date)

    unflagged_computed = tmp_path / "unflagged.yaml"
    unflagged_computed.write_text(
        """
ljse_events:
  - ticker: CICG
    date: "2026-06-26"
    kind: ex_dividend
    computed: true
    source_url: "https://seonet.ljse.si/?doc_id=92794"
""",
        encoding="utf-8",
    )
    with pytest.raises(LJSEValidationError, match="computed_note"):
        load_ljse_events_yaml(unflagged_computed)

    flagged_computed = tmp_path / "flagged.yaml"
    flagged_computed.write_text(
        """
ljse_events:
  - ticker: CICG
    date: "2026-06-26"
    kind: ex_dividend
    computed: true
    computed_note: "Estimated T-1 business day prior to KDD record date 2026-06-29"
    amount_per_share: 1.80
    source_url: "https://seonet.ljse.si/?doc_id=92794"
""",
        encoding="utf-8",
    )
    loaded = load_ljse_events_yaml(flagged_computed)
    assert len(loaded) == 1
    ev = loaded[0]
    assert ev.computed is True
    assert "[computed — verify settlement]" in ev.summary
    assert "Flag: Computed date (Estimated T-1 business day prior to KDD record date 2026-06-29)" in ev.description
    assert "Dividend Amount: 1.80 EUR per share" in ev.description


def test_seonet_dividend_and_agm_keyword_filtering() -> None:
    """Second SEOnet keyword filter matches Slovene/English dividend & AGM titles without false positives."""
    # Positive dividend / AGM matches
    for title in (
        "Obvestilo o sklepih 32. skupščine delničarjev Krke, d. d., Novo mesto",
        "Obvestilo o sklepih 30. redne skupščine delničarjev",
        "NLB, d.d., Ljubljana – predlog izplačila dividend",
        "Potrjena druga dividenda NLB v letu; skupno izplačilo znaša 257,2 milijona evrov",
        "Določitev dneva presečne liste in izplačilo dividend",
        "32nd Annual General Meeting notice",
        "NLB, d.d., Ljubljana - proposed dividend payment",
        "Ex-dividend date and record date notice",
    ):
        assert is_dividend_or_agm_title(title), f"Expected match for: {title}"
        assert classify_announcement_title(title) == "dividend_agm"

    # Negative non-matching financial/earnings/treasury titles
    for title in (
        "Nerevidirano poročilo o poslovanju Cinkarne Celje, d. d. za obdobje 1. 1. 2026 do 30. 9. 2026",
        "Telekom Slovenije Group Annual Business Plan for 2027",
        "Začetek programa odkupa lastnih delnic",
        "Prevzemna ponudba za odkup delnic",
    ):
        assert classify_announcement_title(title) is None, f"Expected no match for: {title}"

    # Verify filter_items catches both issuer AGM/dividend items and Ljubljanska borza ex-dividend notices
    items = [
        FeedItem(
            id="92979",
            issuer="KRKA, d. d., Novo mesto",
            title="Obvestilo o sklepih 32. skupščine delničarjev Krke, d. d., Novo mesto",
            published="Fri, 10 Jul 2026 08:39:00 GMT",
            link="https://seonet.ljse.si/?doc_id=92979",
            instrument="KRKG",
        ),
        FeedItem(
            id="93272",
            issuer="Ljubljanska borza, d. d., Ljubljana",
            title="NLB, d.d., Ljubljana – predlog izplačila dividend",
            published="Wed, 04 Nov 2026 15:30:00 GMT",
            link="https://seonet.ljse.si/?doc_id=93272",
            instrument="NLBR",
        ),
        FeedItem(
            id="93300",
            issuer="CINKARNA CELJE, d.d., Celje",
            title="Nerevidirano poročilo o poslovanju Cinkarne Celje, d. d. za obdobje 1. 1. 2026 do 30. 9. 2026",
            published="Wed, 11 Nov 2026 08:45:00 GMT",
            link="https://seonet.ljse.si/?doc_id=93300",
            instrument="CICG",
        ),
    ]
    matched = filter_items(
        items,
        {
            "KRKG": "KRKA, d. d., Novo mesto",
            "NLBR": "NLB, d. d., Ljubljana",
            "CICG": "CINKARNA CELJE, d.d., Celje",
        },
    )
    assert [(m.item.id, m.ticker, m.category) for m in matched] == [
        ("92979", "KRKG", "dividend_agm"),
        ("93272", "NLBR", "dividend_agm"),
    ]


def test_step0_dividend_fixtures_date_and_amount_extraction_and_todo_report() -> None:
    """Real SEOnet dividend/AGM fixtures extract wrapped dates, ex/record/pay dates, amounts, and TODO YAML."""
    # 1) KRKA 32nd AGM resolutions PDF (written Slovene month + record date + payout date + 9,10 EUR)
    krka_content = extract_from_announcement_html(
        (FIXTURES_DIR / "doc_92979.html").read_text(encoding="utf-8"),
        "https://seonet.ljse.si/?doc_id=92979",
        local_attachment_map={
            "https://seonet.ljse.si/file.aspx?AttachmentID=56165": FIXTURES_DIR / "att_92979_sklepi.pdf",
        },
    )
    krka_dates = {c.date for c in extract_candidate_dates(krka_content.combined_text)}
    assert {date(2026, 7, 9), date(2026, 7, 22), date(2026, 7, 23)}.issubset(krka_dates)
    assert extract_dividend_amounts(krka_content.combined_text) == [9.10]

    # 2) Cinkarna Celje 30th AGM resolutions PDF (handles wrapped "29. 6.\n2026" record date + 1,80 EUR)
    cicg_content = extract_from_announcement_html(
        (FIXTURES_DIR / "doc_92794.html").read_text(encoding="utf-8"),
        "https://seonet.ljse.si/?doc_id=92794",
        local_attachment_map={
            "https://seonet.ljse.si/file.aspx?AttachmentID=56075": FIXTURES_DIR / "att_92794_sklepi.pdf",
        },
    )
    cicg_dates = {c.date for c in extract_candidate_dates(cicg_content.combined_text)}
    assert {date(2026, 5, 20), date(2026, 6, 29), date(2026, 6, 30)}.issubset(cicg_dates)
    assert extract_dividend_amounts(cicg_content.combined_text) == [1.80]

    # 3) Ljubljanska borza HTML notice for NLBR (AGM + ex-date + record date + payout + 6,92 EUR)
    nlb_content = extract_from_announcement_html(
        (FIXTURES_DIR / "doc_93272.html").read_text(encoding="utf-8"),
        "https://seonet.ljse.si/?doc_id=93272",
    )
    nlb_candidates = extract_candidate_dates(nlb_content.combined_text)
    by_date = {c.date: c for c in nlb_candidates}
    assert set(by_date.keys()) == {
        date(2026, 12, 14),
        date(2026, 12, 18),
        date(2026, 12, 21),
        date(2026, 12, 22),
    }
    assert "skupščine" in by_date[date(2026, 12, 14)].label_context.lower()
    assert "brez upravičenja do dividende" in by_date[date(2026, 12, 18)].label_context.lower()
    assert "presečni dan" in by_date[date(2026, 12, 21)].label_context.lower()
    assert "začetka izplačil" in by_date[date(2026, 12, 22)].label_context.lower()
    assert extract_dividend_amounts(nlb_content.combined_text) == [6.92]

    # Render report and confirm manual-review format with type: TODO & amount_per_share
    item = FeedItem(
        id="93272",
        issuer="Ljubljanska borza, d. d., Ljubljana",
        title="NLB, d.d., Ljubljana – predlog izplačila dividend",
        published="Wed, 04 Nov 2026 15:30:00 GMT",
        link="https://seonet.ljse.si/?doc_id=93272",
        instrument="NLBR",
    )
    match = filter_items([item], {"NLBR": "NLB, d. d., Ljubljana"})[0]
    report = render_announcement_report(match, nlb_content, nlb_candidates)
    assert "<!-- seonet-doc-id: 93272 -->" in report
    assert "# New dividend/AGM announcement: NLBR" in report
    assert "6.92 EUR per share" in report

    yaml_block = report.split("```yaml\n", 1)[1].split("\n```", 1)[0]
    parsed = yaml.safe_load(yaml_block)
    assert all(ev["type"] == "TODO" for ev in parsed["ljse_events"])
    assert all(ev["amount_per_share"] == 6.92 for ev in parsed["ljse_events"])

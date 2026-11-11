"""Markdown report rendering for matched SEOnet financial calendar and dividend/AGM announcements."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import yaml

from earnings_cal.seonet.dates import DateCandidate, extract_dividend_amounts
from earnings_cal.seonet.documents import AnnouncementContent
from earnings_cal.seonet.filter import MatchedItem


def _escape_md_cell(value: str) -> str:
    return " ".join(value.replace("|", "\\|").splitlines()).strip()


def build_ljse_yaml_snippet(
    ticker: str,
    announcement_url: str,
    candidates: Sequence[DateCandidate],
    dividend_amounts: Sequence[float] = (),
) -> str:
    """Build a valid, ready-to-paste YAML snippet for `ljse_events.yaml`."""
    entries: list[dict[str, Any]] = []
    single_amount = dividend_amounts[0] if len(dividend_amounts) == 1 else None

    if candidates:
        for cand in candidates:
            entry: dict[str, Any] = {
                "ticker": ticker,
                "date": cand.date.isoformat(),
                "type": "TODO",
                "note": cand.label_context or cand.raw_line,
                "source_url": announcement_url,
            }
            if single_amount is not None:
                entry["amount_per_share"] = single_amount
            entries.append(entry)
    else:
        entry = {
            "ticker": ticker,
            "date": "YYYY-MM-DD",
            "type": "TODO",
            "note": "Manual review required — inspect announcement/attachment",
            "source_url": announcement_url,
        }
        if single_amount is not None:
            entry["amount_per_share"] = single_amount
        entries.append(entry)

    payload = {"ljse_events": entries}
    return yaml.safe_dump(
        payload,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    ).rstrip()


def render_announcement_report(
    match: MatchedItem,
    content: AnnouncementContent,
    candidates: Sequence[DateCandidate],
) -> str:
    """Render a Markdown manual-review report for one matched SEOnet announcement."""
    item = match.item
    amounts = extract_dividend_amounts(content.combined_text)
    heading = (
        f"# New dividend/AGM announcement: {match.ticker}"
        if match.category == "dividend_agm"
        else f"# New financial calendar: {match.ticker}"
    )

    lines: list[str] = [
        f"<!-- seonet-doc-id: {item.id} -->",
        heading,
        "",
        f"- **Category:** `{match.category}`",
        f"- **Issuer:** {match.issuer_name}",
        f"- **Ticker:** `{match.ticker}`",
        f"- **Announcement Title:** {item.title}",
        f"- **Announcement ID:** `{item.id}`",
        f"- **Announcement Link:** {item.link}",
        f"- **Publish Date:** {item.published or 'Unknown'}",
    ]
    if amounts:
        formatted = ", ".join(f"{amt:.2f} EUR per share" for amt in amounts)
        lines.append(f"- **Detected Dividend Per Share:** {formatted}")
    lines.append("")

    lines.append("## Attachments")
    lines.append("")
    if content.attachments:
        for att in content.attachments:
            if att.status == "needs_manual_reading":
                lines.append(
                    f"- `{att.filename}` ({att.content_type}): **needs manual reading** — {att.note}"
                )
            elif att.status == "unsupported":
                lines.append(
                    f"- `{att.filename}` ({att.content_type}): **unsupported format** — {att.note}"
                )
            else:
                lines.append(f"- `{att.filename}` ({att.content_type}): extracted text")
        lines.append("")
    else:
        lines.append("- No attached documents on this SEOnet announcement.")
        if content.external_links:
            lines.append("- External link(s) referenced in announcement body:")
            for ext_url in content.external_links:
                lines.append(f"  - {ext_url}")
        lines.append("")

    lines.append("## Candidate Dates")
    lines.append("")
    if candidates:
        lines.append("| Date | Label Context | Raw Source Line |")
        lines.append("| --- | --- | --- |")
        for cand in candidates:
            lines.append(
                f"| `{cand.date.isoformat()}` "
                f"| {_escape_md_cell(cand.label_context)} "
                f"| `{_escape_md_cell(cand.raw_line)}` |"
            )
    else:
        lines.append("_No candidate dates were automatically extracted. Please review the announcement manually._")
    lines.append("")

    yaml_snippet = build_ljse_yaml_snippet(
        ticker=match.ticker,
        announcement_url=item.link,
        candidates=candidates,
        dividend_amounts=amounts,
    )
    lines.extend(
        [
            "## Ready-to-paste YAML for `ljse_events.yaml`",
            "",
            "Review the candidate dates above, keep only the events you want, set `type`, and paste into `ljse_events.yaml`:",
            "",
            "```yaml",
            yaml_snippet,
            "```",
            "",
        ]
    )

    return "\n".join(lines)

"""Markdown report rendering for matched SEOnet financial calendar announcements."""

from __future__ import annotations

from collections.abc import Sequence

import yaml

from earnings_cal.seonet.dates import DateCandidate
from earnings_cal.seonet.documents import AnnouncementContent
from earnings_cal.seonet.filter import MatchedItem


def _escape_md_cell(value: str) -> str:
    return " ".join(value.replace("|", "\\|").splitlines()).strip()


def build_ljse_yaml_snippet(
    ticker: str,
    announcement_url: str,
    candidates: Sequence[DateCandidate],
) -> str:
    """Build a valid, ready-to-paste YAML snippet for `ljse_events.yaml`."""
    entries: list[dict[str, str]] = []
    if candidates:
        for cand in candidates:
            entries.append(
                {
                    "ticker": ticker,
                    "date": cand.date.isoformat(),
                    "type": "TODO",
                    "note": cand.label_context or cand.raw_line,
                    "source_url": announcement_url,
                }
            )
    else:
        entries.append(
            {
                "ticker": ticker,
                "date": "YYYY-MM-DD",
                "type": "TODO",
                "note": "Manual review required — inspect announcement/attachment",
                "source_url": announcement_url,
            }
        )

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
    lines: list[str] = [
        f"<!-- seonet-doc-id: {item.id} -->",
        f"# New financial calendar: {match.ticker}",
        "",
        f"- **Issuer:** {match.issuer_name}",
        f"- **Ticker:** `{match.ticker}`",
        f"- **Announcement Title:** {item.title}",
        f"- **Announcement ID:** `{item.id}`",
        f"- **Announcement Link:** {item.link}",
        f"- **Publish Date:** {item.published or 'Unknown'}",
        "",
    ]

    if content.attachments:
        lines.append("## Attachments")
        lines.append("")
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
        lines.append("## Attachments")
        lines.append("")
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

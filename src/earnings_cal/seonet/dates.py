"""Candidate date and dividend amount extraction from SEOnet announcement text and PDFs."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from earnings_cal.seonet.filter import normalize_text

# Slovene month names (nominative, genitive, dative/locative, instrumental, common abbreviations)
# plus English month names for English SEOnet announcements.
MONTH_LOOKUP: dict[str, int] = {
    # January
    "januar": 1,
    "januarja": 1,
    "januarju": 1,
    "januarjem": 1,
    "jan": 1,
    "january": 1,
    # February
    "februar": 2,
    "februarja": 2,
    "februarju": 2,
    "februarjem": 2,
    "feb": 2,
    "february": 2,
    # March
    "marec": 3,
    "marca": 3,
    "marcu": 3,
    "marcem": 3,
    "mar": 3,
    "march": 3,
    # April
    "april": 4,
    "aprila": 4,
    "aprilu": 4,
    "aprilom": 4,
    "apr": 4,
    # May
    "maj": 5,
    "maja": 5,
    "maju": 5,
    "majem": 5,
    "may": 5,
    # June
    "junij": 6,
    "junija": 6,
    "juniju": 6,
    "junijem": 6,
    "jun": 6,
    "june": 6,
    # July
    "julij": 7,
    "julija": 7,
    "juliju": 7,
    "julijem": 7,
    "jul": 7,
    "july": 7,
    # August
    "avgust": 8,
    "avgusta": 8,
    "avgustu": 8,
    "avgustom": 8,
    "avg": 8,
    "august": 8,
    "aug": 8,
    # September
    "september": 9,
    "septembra": 9,
    "septembru": 9,
    "septembrom": 9,
    "sep": 9,
    "sept": 9,
    # October
    "oktober": 10,
    "oktobra": 10,
    "oktobru": 10,
    "oktobrom": 10,
    "okt": 10,
    "october": 10,
    "oct": 10,
    # November
    "november": 11,
    "novembra": 11,
    "novembru": 11,
    "novembrom": 11,
    "nov": 11,
    # December
    "december": 12,
    "decembra": 12,
    "decembru": 12,
    "decembrom": 12,
    "dec": 12,
}

_MONTH_ALT = "|".join(sorted(MONTH_LOOKUP.keys(), key=len, reverse=True))

# Range with shared trailing year: e.g. "20.10. do 19.11. 2026" or "22.04 do 21.05. 2026"
_RANGE_SHARED_YEAR_RE = re.compile(
    r"(?<!\d)(\d{1,2})\.\s*(\d{1,2})\.?\s*(?:do|–|-|to)\s*(\d{1,2})\.\s*(\d{1,2})\.\s*(20\d{2})(?!\d)",
    re.IGNORECASE,
)

# Numeric Slovenian date: "30. 3. 2027", "30.03.2027", "3. 6. 2026"
_NUMERIC_DATE_RE = re.compile(
    r"(?<!\d)(\d{1,2})\.\s*(\d{1,2})\.\s*(20\d{2})(?!\d)"
)

# Written month after day: "30. marca 2027", "30 marca 2027", "6 November 2026"
_WRITTEN_DMY_RE = re.compile(
    rf"(?<!\d)(\d{{1,2}})\.?\s+({_MONTH_ALT})\.?\s*,?\s*(20\d{{2}})(?!\d)",
    re.IGNORECASE,
)

# Written month before day (English): "November 6, 2026"
_WRITTEN_MDY_RE = re.compile(
    rf"\b({_MONTH_ALT})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?\s*,\s*(20\d{{2}})(?!\d)",
    re.IGNORECASE,
)

# Partial numeric date split across a line wrap in PDFs, e.g. "29. 6.\n2026."
_WRAPPED_DATE_RE = re.compile(r"(\d{1,2}\.\s*\d{1,2}\.)\s*\n\s*(20\d{2}\b)")

# Dividend amount per share expressions observed in SEOnet notices:
# "9,10 EUR bruto na delnico", "1,80 EUR na delnico", "2,30 evra na navadno delnico",
# "6,43 evra bruto na delnico", "Višina predlagane dividende: 6,92 EUR", "EUR 6.92"
_PER_SHARE_AMOUNT_RE = re.compile(
    r"(?:"
    r"(\d+[.,]\d{2})\s*(?:EUR|€|evr[ao]v?)(?:\s+bruto|\s+gross)?\s+na\s+(?:\S+\s+){0,2}delnic\w*"
    r"|(?:visina\s+predlagane\s+dividende|proposed\s+amount\s+of\s+dividend)[^\d]{1,30}(?:EUR\s*)?(\d+[.,]\d{2})(?:\s*EUR)?"
    r"|(\d+[.,]\d{2})\s*(?:EUR|€)\s+per\s+share"
    r"|EUR\s+(\d+[.,]\d{2})\s+per\s+share"
    r")",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class DateCandidate:
    """Extracted candidate date with nearby label context and exact raw source line."""

    date: date
    label_context: str
    raw_line: str

    def to_dict(self) -> dict[str, str]:
        """Return `{date, label_context, raw_line}` dictionary representation."""
        return {
            "date": self.date.isoformat(),
            "label_context": self.label_context,
            "raw_line": self.raw_line,
        }


def _safe_date(year: int, month: int, day: int) -> date | None:
    if not (2000 <= year <= 2100):
        return None
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _preprocess_wrapped_dates(text: str) -> str:
    """Join numeric dates split at PDF line wraps (e.g. `29. 6.\\n2026.` -> `29. 6. 2026.`)."""
    return _WRAPPED_DATE_RE.sub(r"\1 \2", text)


def _extract_dates_from_line(line: str) -> list[tuple[date, tuple[int, int]]]:
    """Return ordered `(date, (start, end))` matches from a single line."""
    normalized = normalize_text(line)
    matches: list[tuple[date, tuple[int, int]]] = []
    used_spans: list[tuple[int, int]] = []

    def _overlaps(span: tuple[int, int]) -> bool:
        return any(not (span[1] <= s or span[0] >= e) for s, e in used_spans)

    for m in _RANGE_SHARED_YEAR_RE.finditer(line):
        d1, m1, d2, m2, y = map(int, m.groups())
        dt1 = _safe_date(y, m1, d1)
        dt2 = _safe_date(y, m2, d2)
        span = m.span()
        if dt1 is not None and dt2 is not None:
            used_spans.append(span)
            matches.append((dt1, span))
            matches.append((dt2, span))

    for m in _NUMERIC_DATE_RE.finditer(line):
        if _overlaps(m.span()):
            continue
        d, mo, y = map(int, m.groups())
        dt = _safe_date(y, mo, d)
        if dt is not None:
            used_spans.append(m.span())
            matches.append((dt, m.span()))

    for m in _WRITTEN_DMY_RE.finditer(normalized):
        d_str, month_str, y_str = m.groups()
        month = MONTH_LOOKUP.get(month_str.lower())
        if month is None:
            continue
        dt = _safe_date(int(y_str), month, int(d_str))
        if dt is not None:
            matches.append((dt, m.span()))

    for m in _WRITTEN_MDY_RE.finditer(normalized):
        month_str, d_str, y_str = m.groups()
        month = MONTH_LOOKUP.get(month_str.lower())
        if month is None:
            continue
        dt = _safe_date(int(y_str), month, int(d_str))
        if dt is not None:
            matches.append((dt, m.span()))

    deduped: list[tuple[date, tuple[int, int]]] = []
    seen_dates: set[date] = set()
    for dt, span in matches:
        if dt not in seen_dates:
            seen_dates.add(dt)
            deduped.append((dt, span))
    return deduped


def _strip_dates_from_line(line: str) -> str:
    """Remove matched date tokens from a line to check remaining descriptive label text."""
    cleaned = _RANGE_SHARED_YEAR_RE.sub(" ", line)
    cleaned = _NUMERIC_DATE_RE.sub(" ", cleaned)
    cleaned_norm = normalize_text(cleaned)
    if _WRITTEN_DMY_RE.search(cleaned_norm) or _WRITTEN_MDY_RE.search(cleaned_norm):
        cleaned = re.sub(
            rf"(?<!\d)\d{{1,2}}\.?\s+\S+\.?\s*,?\s*20\d{{2}}(?!\d)",
            " ",
            cleaned,
        )
        cleaned = re.sub(
            r"\b\S+\.?\s+\d{1,2}(?:st|nd|rd|th)?\s*,\s*20\d{2}(?!\d)",
            " ",
            cleaned,
        )
    cleaned = re.sub(r"^[\s•\-–:*|,]+|[\s•\-–:*|,]+$", "", cleaned)
    return " ".join(cleaned.split())


_NON_LABEL_TOKENS = {
    "ponedeljek",
    "torek",
    "sreda",
    "cetrtek",
    "petek",
    "sobota",
    "nedelja",
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
    "ob",
    "uri",
    "at",
    "am",
    "pm",
}


def _line_has_meaningful_label(text: str) -> bool:
    tokens = [
        tok
        for tok in re.findall(r"[^\s,;:.]+", normalize_text(text))
        if tok not in _NON_LABEL_TOKENS and not re.fullmatch(r"\d{1,2}[.:]\d{2}", tok)
    ]
    letters = sum(1 for tok in tokens for ch in tok if ch.isalpha())
    return letters >= 3


def _build_label_context(lines: list[str], idx: int) -> str:
    """Build nearby label context for line `idx`, pulling adjacent lines when needed."""
    current_leftover = _strip_dates_from_line(lines[idx])
    if _line_has_meaningful_label(current_leftover):
        parts = [current_leftover]
        if idx > 0:
            prev = lines[idx - 1].strip()
            if (
                prev
                and not _extract_dates_from_line(prev)
                and (prev.startswith("•") or prev.endswith(("in", ":")) or len(current_leftover) < 25)
            ):
                parts.insert(0, prev.lstrip("•").strip())
        combined = " ".join(parts)
        return combined[:200]

    # Current line is mostly/only the date (common in multi-line PDF/HTML table rows)
    context_parts: list[str] = []
    prev_ended_with_colon = False
    for back in (1, 2):
        if idx - back >= 0:
            prev = lines[idx - back].strip()
            if not prev or _extract_dates_from_line(prev):
                break
            if back == 1 and prev.endswith(":"):
                prev_ended_with_colon = True
            context_parts.insert(0, prev.lstrip("•").strip())
            if prev.startswith("•") or prev.endswith(":"):
                break

    # Do not append the following line if the preceding line already ended with `:`
    # (HTML key-value layout where the next line is the next field's label).
    if not prev_ended_with_colon and idx + 1 < len(lines):
        nxt = lines[idx + 1].strip()
        if nxt and not _extract_dates_from_line(nxt) and not nxt.endswith(":"):
            context_parts.append(nxt.lstrip("•").strip())

    if context_parts:
        return " ".join(context_parts)[:200]
    return lines[idx].strip()[:200]


def extract_dividend_amounts(text: str) -> list[float]:
    """Extract dividend per-share amounts (in EUR) explicitly stated in `text`."""
    if not text or not text.strip():
        return []
    normalized = normalize_text(text)
    amounts: list[float] = []
    seen: set[float] = set()
    for match in _PER_SHARE_AMOUNT_RE.finditer(normalized):
        raw_val = next((g for g in match.groups() if g), None)
        if not raw_val:
            continue
        val = float(raw_val.replace(",", "."))
        if val > 0 and val not in seen:
            seen.add(val)
            amounts.append(val)
    return amounts


def extract_candidate_dates(text: str) -> list[DateCandidate]:
    """Extract candidate dates and nearby label context from extracted document text."""
    if not text or not text.strip():
        return []

    preprocessed = _preprocess_wrapped_dates(text)
    lines = [" ".join(raw.split()) for raw in preprocessed.splitlines() if raw.strip()]
    candidates: list[DateCandidate] = []
    seen_pairs: set[tuple[date, str]] = set()

    for idx, line in enumerate(lines):
        found = _extract_dates_from_line(line)
        if not found:
            continue
        label_context = _build_label_context(lines, idx)
        for dt, _span in found:
            key = (dt, line)
            if key in seen_pairs:
                continue
            seen_pairs.add(key)
            candidates.append(
                DateCandidate(
                    date=dt,
                    label_context=label_context,
                    raw_line=line,
                )
            )

    return candidates

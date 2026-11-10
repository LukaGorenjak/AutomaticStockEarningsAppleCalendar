"""Announcement page parsing, attachment downloading, and text extraction."""

from __future__ import annotations

import logging
import re
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

import pdfplumber
import requests

from earnings_cal.seonet.feed import RETRYABLE_STATUS_CODES, USER_AGENT

logger = logging.getLogger(__name__)

SEONET_BASE_URL = "https://seonet.ljse.si/"


class DocumentError(RuntimeError):
    """Raised when an announcement page or its attachment cannot be downloaded/parsed."""


@dataclass(frozen=True)
class AttachmentRef:
    """Reference to an attachment found on a SEOnet announcement page."""

    url: str
    label: str


@dataclass(frozen=True)
class ExtractedAttachment:
    """Result of downloading and extracting text from one attachment."""

    filename: str
    url: str
    content_type: str
    status: str  # "extracted", "needs_manual_reading", "unsupported"
    text: str = ""
    note: str = ""


@dataclass(frozen=True)
class AnnouncementContent:
    """Parsed body text, external links, and extracted attachments for an announcement."""

    url: str
    body_text: str
    external_links: list[str] = field(default_factory=list)
    attachments: list[ExtractedAttachment] = field(default_factory=list)

    @property
    def combined_text(self) -> str:
        """Combined text from attachments (preferred) and announcement body."""
        parts: list[str] = []
        for att in self.attachments:
            if att.text.strip():
                parts.append(att.text)
        if self.body_text.strip():
            parts.append(self.body_text)
        return "\n".join(parts)


class _SeoDocumentHTMLParser(HTMLParser):
    """Extract `#seo_document_body` text/links and `a.att` attachment references."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._div_stack: list[str] = []
        self._in_body = False
        self._in_attachments = False
        self._skip_depth = 0
        self._body_chunks: list[str] = []
        self.external_links: list[str] = []
        self.attachments: list[AttachmentRef] = []
        self._current_att_href: str | None = None
        self._current_att_label: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = {k.lower(): (v or "") for k, v in attrs}
        tag_l = tag.lower()

        if tag_l in {"script", "style"}:
            self._skip_depth += 1
            return

        if tag_l == "div":
            div_id = attr_map.get("id", "")
            self._div_stack.append(div_id)
            if div_id == "seo_document_body":
                self._in_body = True
            elif div_id == "seo_document_attachments":
                self._in_attachments = True

        if tag_l == "br" and self._in_body:
            self._body_chunks.append("\n")

        if tag_l == "a":
            href = attr_map.get("href", "").strip()
            classes = set(attr_map.get("class", "").split())
            if ("att" in classes or self._in_attachments) and href:
                self._current_att_href = href
                self._current_att_label = []
            elif self._in_body and href and not href.startswith("visit_publisher.aspx"):
                if href not in self.external_links:
                    self.external_links.append(href)

    def handle_endtag(self, tag: str) -> None:
        tag_l = tag.lower()
        if tag_l in {"script", "style"}:
            if self._skip_depth > 0:
                self._skip_depth -= 1
            return

        if tag_l == "a" and self._current_att_href is not None:
            label = " ".join("".join(self._current_att_label).split())
            self.attachments.append(AttachmentRef(url=self._current_att_href, label=label))
            self._current_att_href = None
            self._current_att_label = []

        if self._in_body and tag_l in {"p", "div", "li", "tr", "h1", "h2", "h3", "h4"}:
            self._body_chunks.append("\n")

        if tag_l == "div" and self._div_stack:
            popped = self._div_stack.pop()
            if popped == "seo_document_body":
                self._in_body = False
            elif popped == "seo_document_attachments":
                self._in_attachments = False

    def handle_data(self, data: str) -> None:
        if self._skip_depth > 0:
            return
        if self._current_att_href is not None:
            self._current_att_label.append(data)
        if self._in_body:
            self._body_chunks.append(data)

    @property
    def body_text(self) -> str:
        raw = "".join(self._body_chunks)
        lines = [" ".join(line.split()) for line in raw.splitlines()]
        return "\n".join(line for line in lines if line)


class _PlainHTMLTextParser(HTMLParser):
    """Minimal HTML-to-text converter for HTML attachments."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag_l = tag.lower()
        if tag_l in {"script", "style"}:
            self._skip_depth += 1
        elif tag_l in {"br", "p", "div", "tr", "li"}:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag_l = tag.lower()
        if tag_l in {"script", "style"} and self._skip_depth > 0:
            self._skip_depth -= 1
        elif tag_l in {"p", "div", "tr", "li"}:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            self._chunks.append(data)

    @property
    def text(self) -> str:
        lines = [" ".join(line.split()) for line in "".join(self._chunks).splitlines()]
        return "\n".join(line for line in lines if line)


def parse_announcement_html(
    html_text: str,
    announcement_url: str = SEONET_BASE_URL,
) -> tuple[str, list[str], list[AttachmentRef]]:
    """Parse a SEOnet announcement page into (body_text, external_links, attachment_refs)."""
    parser = _SeoDocumentHTMLParser()
    parser.feed(html_text)
    parser.close()

    resolved_attachments: list[AttachmentRef] = []
    seen_urls: set[str] = set()
    for ref in parser.attachments:
        full_url = urljoin(announcement_url, ref.url)
        if full_url not in seen_urls:
            seen_urls.add(full_url)
            resolved_attachments.append(AttachmentRef(url=full_url, label=ref.label))

    return parser.body_text, parser.external_links, resolved_attachments


def _parse_filename_from_headers(content_disposition: str, url: str, fallback_label: str) -> str:
    if content_disposition:
        utf8_match = re.search(r"filename\*\s*=\s*UTF-8''([^;]+)", content_disposition, re.IGNORECASE)
        if utf8_match:
            return Path(unquote(utf8_match.group(1).strip().strip('"'))).name
        plain_match = re.search(r'filename\s*=\s*"?([^";]+)"?', content_disposition, re.IGNORECASE)
        if plain_match:
            return Path(plain_match.group(1).strip()).name

    url_path = Path(urlparse(url).path).name
    if url_path and url_path.lower() != "file.aspx":
        return url_path

    safe_label = re.sub(r"[^A-Za-z0-9._-]+", "_", fallback_label).strip("_")
    return safe_label or "attachment"


def _http_get_with_retry(
    session: requests.Session,
    url: str,
    timeout: float = 10.0,
    backoff_seconds: float = 2.0,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> requests.Response:
    headers = {"User-Agent": USER_AGENT}
    last_error: Exception | None = None

    for attempt in range(2):
        try:
            response = session.get(url, headers=headers, timeout=timeout)
        except requests.RequestException as exc:
            last_error = exc
            if attempt == 0:
                logger.warning("Request error fetching %s (%s); retrying in %.1fs...", url, exc, backoff_seconds)
                sleep_fn(backoff_seconds)
                continue
            raise DocumentError(f"Failed to fetch {url}: {exc}") from exc

        if response.status_code in RETRYABLE_STATUS_CODES:
            last_error = DocumentError(f"HTTP {response.status_code} fetching {url}")
            if attempt == 0:
                logger.warning("HTTP %d fetching %s; retrying in %.1fs...", response.status_code, url, backoff_seconds)
                sleep_fn(backoff_seconds)
                continue
            raise last_error

        if response.status_code >= 400:
            raise DocumentError(f"HTTP {response.status_code} fetching {url}")

        return response

    raise DocumentError(f"Failed to fetch {url}: {last_error}")


def extract_pdf_text(pdf_path: Path) -> str:
    """Extract text from all pages of a PDF using `pdfplumber`."""
    try:
        with pdfplumber.open(pdf_path) as pdf:
            pages_text = [(page.extract_text() or "").strip() for page in pdf.pages]
    except Exception as exc:
        raise DocumentError(f"Failed to read PDF {pdf_path.name}: {exc}") from exc

    return "\n".join(text for text in pages_text if text)


def extract_attachment_file(
    file_path: Path,
    url: str,
    content_type: str = "",
) -> ExtractedAttachment:
    """Extract text from a downloaded attachment file on disk."""
    filename = file_path.name
    ct_lower = content_type.split(";")[0].strip().lower()
    suffix = file_path.suffix.lower()

    head = file_path.read_bytes()[:8]
    is_pdf = head.startswith(b"%PDF-") or ct_lower == "application/pdf" or suffix == ".pdf"

    if is_pdf:
        text = extract_pdf_text(file_path)
        if not text.strip():
            return ExtractedAttachment(
                filename=filename,
                url=url,
                content_type=ct_lower or "application/pdf",
                status="needs_manual_reading",
                text="",
                note="Scanned PDF with no extractable text — needs manual reading.",
            )
        return ExtractedAttachment(
            filename=filename,
            url=url,
            content_type=ct_lower or "application/pdf",
            status="extracted",
            text=text,
        )

    if ct_lower in {"text/plain", "text/csv"} or suffix in {".txt", ".csv"}:
        text = file_path.read_text(encoding="utf-8", errors="replace").strip()
        return ExtractedAttachment(
            filename=filename,
            url=url,
            content_type=ct_lower or "text/plain",
            status="extracted" if text else "needs_manual_reading",
            text=text,
            note="" if text else "Empty text attachment — needs manual reading.",
        )

    if ct_lower == "text/html" or suffix in {".html", ".htm"}:
        html_parser = _PlainHTMLTextParser()
        html_parser.feed(file_path.read_text(encoding="utf-8", errors="replace"))
        html_parser.close()
        text = html_parser.text
        return ExtractedAttachment(
            filename=filename,
            url=url,
            content_type=ct_lower or "text/html",
            status="extracted" if text else "needs_manual_reading",
            text=text,
            note="" if text else "HTML attachment has no text — needs manual reading.",
        )

    return ExtractedAttachment(
        filename=filename,
        url=url,
        content_type=ct_lower or "application/octet-stream",
        status="unsupported",
        text="",
        note=f"Unsupported attachment format ({ct_lower or suffix or 'unknown'}) — needs manual review.",
    )


def fetch_announcement_content(
    announcement_url: str,
    session: requests.Session | None = None,
    timeout: float = 10.0,
    backoff_seconds: float = 2.0,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> AnnouncementContent:
    """Fetch an announcement page and sequentially download & extract all attachments."""
    http = session or requests.Session()
    page_resp = _http_get_with_retry(
        http,
        announcement_url,
        timeout=timeout,
        backoff_seconds=backoff_seconds,
        sleep_fn=sleep_fn,
    )
    html_text = page_resp.content.decode(page_resp.encoding or "utf-8", errors="replace")
    return extract_from_announcement_html(
        html_text=html_text,
        announcement_url=announcement_url,
        session=http,
        timeout=timeout,
        backoff_seconds=backoff_seconds,
        sleep_fn=sleep_fn,
    )


def extract_from_announcement_html(
    html_text: str,
    announcement_url: str,
    session: requests.Session | None = None,
    timeout: float = 10.0,
    backoff_seconds: float = 2.0,
    sleep_fn: Callable[[float], None] = time.sleep,
    local_attachment_map: dict[str, Path] | None = None,
) -> AnnouncementContent:
    """Parse announcement HTML and download (or load from `local_attachment_map`) attachments."""
    body_text, external_links, attachment_refs = parse_announcement_html(
        html_text,
        announcement_url=announcement_url,
    )
    extracted: list[ExtractedAttachment] = []

    if attachment_refs:
        with tempfile.TemporaryDirectory(prefix="seonet_att_") as tmp_dir:
            tmp_root = Path(tmp_dir)
            for idx, ref in enumerate(attachment_refs, start=1):
                if local_attachment_map is not None:
                    local_path = local_attachment_map.get(ref.url)
                    if local_path is None:
                        raise DocumentError(f"Missing local fixture attachment for {ref.url}")
                    extracted.append(extract_attachment_file(local_path, url=ref.url))
                    continue

                http = session or requests.Session()
                att_resp = _http_get_with_retry(
                    http,
                    ref.url,
                    timeout=timeout,
                    backoff_seconds=backoff_seconds,
                    sleep_fn=sleep_fn,
                )
                content_type = att_resp.headers.get("Content-Type", "")
                content_disp = att_resp.headers.get("Content-Disposition", "")
                filename = _parse_filename_from_headers(content_disp, ref.url, ref.label)
                if "." not in filename and "pdf" in content_type.lower():
                    filename = f"{filename}.pdf"

                target_path = tmp_root / f"{idx}_{filename}"
                target_path.write_bytes(att_resp.content)
                extracted.append(
                    extract_attachment_file(target_path, url=ref.url, content_type=content_type)
                )

    return AnnouncementContent(
        url=announcement_url,
        body_text=body_text,
        external_links=external_links,
        attachments=extracted,
    )

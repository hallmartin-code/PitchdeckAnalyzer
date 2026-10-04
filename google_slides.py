"""
Pull a Google Slides deck down as a PDF.

Claude's API takes PDFs, not Slides, so a pasted Slides link is turned into the
PDF that Google itself exports for that deck — no conversion of our own, no
extra system dependencies, and the layout is exactly what Google renders.

This uses the deck's public export URL, so it only works for decks shared as
"anyone with the link can view" (or published to the web). A private deck sends
us Google's sign-in page instead of a PDF, which `fetch_slides_pdf` reports as
a sharing problem rather than letting it reach the analysis as a bad file.

Public entry points: `is_slides_url(text)` and `fetch_slides_pdf(url, dest, ...)`.
"""

from __future__ import annotations

import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

TIMEOUT = 60  # seconds; Google renders large decks on the fly

# /presentation/d/<id>/... for a normal deck, /presentation/d/e/<id>/pub for one
# published to the web. The two take different export URLs.
_DECK_RE = re.compile(r"/presentation/d/(e/)?([A-Za-z0-9_-]{10,})")
_FILENAME_RE = re.compile(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", re.IGNORECASE)

_HOSTS = {"docs.google.com", "www.docs.google.com"}


class SlidesError(RuntimeError):
    """The link is not a usable Google Slides deck, or we cannot read it."""


def is_slides_url(text: str) -> bool:
    """True for anything that looks like a Google Slides link."""
    text = (text or "").strip()
    if not text:
        return False
    parsed = urllib.parse.urlparse(text if "//" in text else f"https://{text}")
    return parsed.netloc.lower() in _HOSTS and bool(_DECK_RE.search(parsed.path))


def _export_url(url: str) -> str:
    """The URL that returns this deck as a PDF."""
    text = (url or "").strip()
    if not text:
        raise SlidesError("Paste a Google Slides link.")
    parsed = urllib.parse.urlparse(text if "//" in text else f"https://{text}")
    if parsed.netloc.lower() not in _HOSTS:
        raise SlidesError(
            "That is not a Google Slides link. It should start with "
            "https://docs.google.com/presentation/..."
        )
    match = _DECK_RE.search(parsed.path)
    if not match:
        raise SlidesError(
            "That Google link does not point at a Slides deck. Open the deck and copy the "
            "link from the address bar, or use Share → Copy link."
        )
    published, deck_id = match.group(1), match.group(2)
    if published:
        return f"https://docs.google.com/presentation/d/e/{deck_id}/pub?output=pdf"
    return f"https://docs.google.com/presentation/d/{deck_id}/export/pdf"


def _suggested_name(headers, fallback: str = "Google Slides deck.pdf") -> str:
    """The deck's own name, from the response's Content-Disposition."""
    match = _FILENAME_RE.search(headers.get("Content-Disposition", "") or "")
    if not match:
        return fallback
    name = urllib.parse.unquote(match.group(1)).strip()
    if not name.lower().endswith(".pdf"):
        name = f"{name}.pdf"
    return name or fallback


_SHARING_HELP = (
    "Open the deck, choose Share → General access → Anyone with the link (Viewer), "
    "then paste the link again — or download it as a PDF and upload that instead."
)


def fetch_slides_pdf(url: str, dest: Path, max_bytes: int) -> str:
    """Download the deck to `dest` as a PDF; returns the deck's own file name.

    Raises `SlidesError` with a message meant for the person who pasted the link.
    """
    export = _export_url(url)
    request = urllib.request.Request(
        export,
        headers={"User-Agent": "TEN-Capital-Pitchdeck-Analyzer/1.0"},
    )

    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            # A deck we are not allowed to read answers with a sign-in page, not a PDF.
            if "pdf" not in (response.headers.get("Content-Type", "") or "").lower():
                raise SlidesError(f"That deck is not shared publicly. {_SHARING_HELP}")

            name = _suggested_name(response.headers)
            size = 0
            with dest.open("wb") as handle:
                while chunk := response.read(1024 * 1024):
                    size += len(chunk)
                    if size > max_bytes:
                        raise SlidesError(
                            f"That deck exports to more than {max_bytes // 1024 // 1024} MB, "
                            "which is over the limit the API accepts."
                        )
                    handle.write(chunk)
    except SlidesError:
        dest.unlink(missing_ok=True)
        raise
    except urllib.error.HTTPError as exc:
        dest.unlink(missing_ok=True)
        if exc.code in (401, 403, 404):
            raise SlidesError(f"That deck is not shared publicly. {_SHARING_HELP}") from exc
        raise SlidesError(f"Google returned an error for that link (HTTP {exc.code}).") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        dest.unlink(missing_ok=True)
        raise SlidesError(f"Could not reach Google to export that deck ({exc}).") from exc

    if dest.read_bytes()[:5] != b"%PDF-":
        dest.unlink(missing_ok=True)
        raise SlidesError(f"Google did not return a PDF for that link. {_SHARING_HELP}")

    return name

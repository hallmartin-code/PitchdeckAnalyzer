"""
Turn a PowerPoint deck into the PDF the analysis actually reads.

Claude's API takes PDFs, and the report grades layout and design, so the deck
has to be *rendered* rather than have its text scraped out. LibreOffice does
that rendering headlessly — the same engine, the same output, on every deploy.

LibreOffice is a system binary, not a Python package: `nixpacks.toml` installs
it in the Railway image, and `soffice_available()` lets the app say so plainly
when it is missing instead of failing minutes into a job.

Public entry points: `is_powerpoint(name)`, `soffice_available()`,
`convert_to_pdf(src)`.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

TIMEOUT = 240  # seconds; a heavy deck can take a while to render

SUFFIXES = (".pptx", ".ppt")

# Windows installs it outside PATH, which is where local test runs look.
_WINDOWS_PATHS = (
    r"C:\Program Files\LibreOffice\program\soffice.exe",
    r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
)


class ConversionError(RuntimeError):
    """The deck could not be rendered to a PDF."""


def is_powerpoint(name: str) -> bool:
    """True for the file extensions we convert."""
    return (name or "").lower().endswith(SUFFIXES)


def soffice_bin() -> str | None:
    """Path to the LibreOffice binary, or None if it is not installed."""
    configured = os.getenv("SOFFICE_BIN")
    if configured:
        return configured if Path(configured).exists() else None
    for name in ("soffice", "libreoffice"):
        found = shutil.which(name)
        if found:
            return found
    for path in _WINDOWS_PATHS:
        if Path(path).exists():
            return path
    return None


def soffice_available() -> bool:
    return soffice_bin() is not None


def _looks_like_deck(src: Path) -> bool:
    """True if the file's own bytes match its extension.

    LibreOffice will cheerfully render a text file that happens to be named
    `.pptx` as a one-page document, which would then be analyzed as if it were
    the deck. Checking the container up front turns that into a clear error.
    """
    head = src.open("rb").read(8)
    if src.suffix.lower() == ".pptx":
        return head[:2] == b"PK"  # .pptx is a zip
    return head == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"  # .ppt is an OLE2 container


def convert_to_pdf(src: Path, out_dir: Path | None = None) -> Path:
    """Render `src` to a PDF and return that path.

    The PDF keeps the deck's own name, so `out_dir` should be somewhere that
    cannot collide with the report files written for the same job.

    Raises `ConversionError` with a message meant for the person who uploaded
    the deck.
    """
    binary = soffice_bin()
    if binary is None:
        raise ConversionError(
            "PowerPoint conversion is not available on this server (LibreOffice is not "
            "installed). Save the deck as a PDF and upload that instead."
        )

    if not _looks_like_deck(src):
        raise ConversionError(
            f"{src.name} is not a readable PowerPoint file — its contents do not match its "
            "extension. Re-export it from PowerPoint, or save it as a PDF and upload that."
        )

    out_dir = out_dir or src.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    # Each run gets its own LibreOffice profile; concurrent jobs sharing one
    # profile is a known way to make soffice exit without converting anything.
    with tempfile.TemporaryDirectory(prefix="lo-profile-") as profile:
        command = [
            binary,
            f"-env:UserInstallation=file:///{Path(profile).as_posix().lstrip('/')}",
            "--headless",
            "--norestore",
            "--invisible",
            "--nolockcheck",
            "--convert-to",
            "pdf",
            "--outdir",
            str(out_dir),
            str(src),
        ]
        try:
            done = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=TIMEOUT,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ConversionError(
                f"Converting the deck took longer than {TIMEOUT // 60} minutes. "
                "Export it to PDF and upload that instead."
            ) from exc
        except OSError as exc:
            raise ConversionError(f"Could not run LibreOffice to convert the deck ({exc}).") from exc

    pdf_path = out_dir / f"{src.stem}.pdf"
    if not pdf_path.exists():
        detail = (done.stderr or done.stdout or "").strip().splitlines()
        tail = detail[-1] if detail else f"exit code {done.returncode}"
        raise ConversionError(
            f"The deck could not be converted to PDF ({tail}). It may be password-protected "
            "or corrupt — export it to PDF and upload that instead."
        )
    return pdf_path

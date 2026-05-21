"""docling parse + atomic sidecar write — the core conversion logic.

Kept deliberately separate from main.py so the FastAPI layer is a thin
adapter and the conversion path is straightforward to unit-test without
spinning up uvicorn.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from importlib.metadata import version as _pkg_version
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Public dataclass: returned by convert_pdf() and serialised both as the
# HTTP response body and as the on-disk sidecar.
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ConvertResult:
    """Result of a single PDF→MD conversion.

    Mirrors the JSON sidecar the original ductile-plugin implementation
    wrote (`<doc_id>.json`), preserving the completion-signal contract:
    callers can either consume the JSON returned over HTTP or filewatch
    the on-disk sidecar — either way they get the same shape.
    """

    doc_id: str
    input_path: str
    output_path: str
    sidecar_path: str
    page_count: int
    parse_duration_seconds: float
    docling_version: str
    started_at: str       # ISO-8601 UTC, RFC3339-compatible
    completed_at: str
    status: str = "ready"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def duration_ms(self) -> int:
        return int(self.parse_duration_seconds * 1000)


# ---------------------------------------------------------------------------
# Errors — typed so main.py can map them to HTTP status codes.
# ---------------------------------------------------------------------------


class InputError(ValueError):
    """Caller supplied bad inputs (missing file, bad path). 4xx territory."""


class TransientError(RuntimeError):
    """Worker-side hiccup that may resolve on retry. 5xx territory."""


# ---------------------------------------------------------------------------
# Module-level singleton: build the DocumentConverter once per process.
# The constructor is cheap; docling lazy-loads layout/table models on
# the first convert() call.
# ---------------------------------------------------------------------------

_CONVERTER = None
_CONVERTER_VERSION: str | None = None


def _docling_version() -> str:
    global _CONVERTER_VERSION
    if _CONVERTER_VERSION is None:
        try:
            _CONVERTER_VERSION = _pkg_version("docling")
        except Exception:  # noqa: BLE001
            _CONVERTER_VERSION = "unknown"
    return _CONVERTER_VERSION


def get_converter():
    """Lazily build the DocumentConverter once per process.

    The constructor itself is cheap; docling lazy-loads layout/table
    models on the first `convert()` call into `$HF_HOME` (= /models in
    the container, mounted on a persistent volume so subsequent
    containers reuse the cache).

    OCR is disabled by default — docling's rapidocr backend writes its
    model file to its own site-packages dir on first use, which fails
    under our non-root container user. Text-bearing PDFs convert
    cleanly without OCR; a future `enable_ocr` request field can
    re-enable it once we wire a writable OCR model cache.
    """
    global _CONVERTER
    if _CONVERTER is not None:
        return _CONVERTER

    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions
    from docling.document_converter import DocumentConverter, PdfFormatOption

    pipeline_options = PdfPipelineOptions()
    pipeline_options.do_ocr = False
    pipeline_options.do_table_structure = True

    _CONVERTER = DocumentConverter(
        format_options={
            InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options),
        }
    )
    return _CONVERTER


def warm() -> dict[str, Any]:
    """Force converter construction and report version + readiness.

    Called by `/healthz` so a probe can confirm docling actually loads,
    not just that the HTTP server is alive. Idempotent — safe to call
    many times.
    """
    get_converter()
    return {
        "status": "ok",
        "docling_version": _docling_version(),
        "ocr_enabled": False,
    }


# ---------------------------------------------------------------------------
# Atomic sidecar write — preserves the contract from the abandoned in-process
# ductile plugin: the .json sidecar lands FIRST, the .md lands LAST, so a
# filewatcher keyed on the .md file is the completion signal.
# ---------------------------------------------------------------------------


def _atomic_write_text(target: Path, content: str) -> None:
    """Write `content` to `target` via a `<dir>/.<name>.tmp.<pid>` rename.

    Same-directory tempfile ensures os.replace() is atomic on the filesystem
    that holds the target (the shared workspace volume).
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_str = tempfile.mkstemp(
        prefix=f".{target.name}.tmp.",
        dir=str(target.parent),
    )
    tmp = Path(tmp_str)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(tmp, target)
    except Exception:
        # Best-effort cleanup of the orphaned tempfile on any failure.
        try:
            tmp.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass
        raise


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _derive_sidecar_path(output_path: Path) -> Path:
    """Same directory, `.json` extension instead of `.md` (or appended if no .md)."""
    if output_path.suffix.lower() == ".md":
        return output_path.with_suffix(".json")
    return output_path.with_name(output_path.name + ".json")


def _derive_doc_id(output_path: Path) -> str:
    """`/workspace/out/foo.md` → `foo`."""
    return output_path.stem


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def convert_pdf(input_path: str, output_path: str) -> ConvertResult:
    """Convert `input_path` → `output_path` (markdown) with a JSON sidecar.

    Atomic write contract: the .json sidecar is written first, the .md last,
    so a downstream filewatcher on *.md is the completion signal.

    Raises:
        InputError: input file missing / unreadable, output dir invalid.
        TransientError: docling failed with an exception that might resolve
            on retry. The caller (gateway plugin) decides whether to retry.
    """
    src = Path(input_path)
    dst = Path(output_path)

    if not src.exists():
        raise InputError(f"input_path not found: {src}")
    if not src.is_file():
        raise InputError(f"input_path is not a regular file: {src}")
    if dst.suffix.lower() not in (".md", ".markdown"):
        raise InputError(f"output_path must end in .md (got {dst.suffix!r})")

    started_at = _now_iso()
    t0 = time.perf_counter()

    try:
        converter = get_converter()
        result = converter.convert(str(src))
        document = result.document
        markdown = document.export_to_markdown()
    except Exception as exc:
        # docling raises a wide variety of internal exceptions. We surface them
        # as TransientError so the gateway plugin returns 5xx; in practice a
        # corrupt PDF will fail the same way on retry, but the caller owns
        # that decision, not us.
        raise TransientError(f"docling conversion failed: {exc}") from exc

    parse_duration = time.perf_counter() - t0
    completed_at = _now_iso()

    # Best-effort page count — docling doesn't guarantee a stable shape here.
    page_count = 0
    try:
        pages = getattr(document, "pages", None)
        if pages is not None:
            page_count = len(pages)
    except Exception:  # noqa: BLE001
        page_count = 0

    doc_id = _derive_doc_id(dst)
    sidecar = _derive_sidecar_path(dst)

    result_obj = ConvertResult(
        doc_id=doc_id,
        input_path=str(src),
        output_path=str(dst),
        sidecar_path=str(sidecar),
        page_count=page_count,
        parse_duration_seconds=round(parse_duration, 3),
        docling_version=_docling_version(),
        started_at=started_at,
        completed_at=completed_at,
        status="ready",
    )

    # Sidecar FIRST, markdown LAST — preserves the *.md filewatch
    # completion-signal contract.
    _atomic_write_text(sidecar, json.dumps(result_obj.to_dict(), indent=2, sort_keys=True) + "\n")
    _atomic_write_text(dst, markdown)

    return result_obj

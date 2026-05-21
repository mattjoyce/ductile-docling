"""Smoke tests for worker.convert and the FastAPI surface.

We don't actually run docling here — it's too heavy for unit tests and
needs the model cache. The tests patch get_converter() with a fake that
returns a deterministic document, exercising:

  - input validation (missing file, wrong extension)
  - the atomic .json-first / .md-last write contract
  - the FastAPI happy path and error mappings
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

import pytest
from fastapi.testclient import TestClient

from worker import convert as cv
from worker.main import app


# ---------- fake docling -----------------------------------------------------


@dataclass
class _FakeDoc:
    pages: list

    def export_to_markdown(self) -> str:
        return "# fake doc\n\nfrom test fixture.\n"


@dataclass
class _FakeResult:
    document: _FakeDoc


class _FakeConverter:
    def convert(self, _path: str) -> _FakeResult:
        return _FakeResult(document=_FakeDoc(pages=[object(), object(), object()]))


@pytest.fixture
def fake_converter(monkeypatch):
    monkeypatch.setattr(cv, "_CONVERTER", _FakeConverter())
    monkeypatch.setattr(cv, "_CONVERTER_VERSION", "0.0.0-fake")
    yield


@pytest.fixture
def sample_pdf(tmp_path: Path) -> Path:
    p = tmp_path / "in" / "demo.pdf"
    p.parent.mkdir(parents=True)
    p.write_bytes(b"%PDF-1.4 fake\n")
    return p


# ---------- convert.convert_pdf ---------------------------------------------


def test_convert_pdf_writes_md_and_sidecar(tmp_path, sample_pdf, fake_converter):
    out = tmp_path / "out" / "demo.md"
    result = cv.convert_pdf(str(sample_pdf), str(out))

    assert out.exists()
    assert out.read_text().startswith("# fake doc")

    sidecar = tmp_path / "out" / "demo.json"
    assert sidecar.exists()
    data = json.loads(sidecar.read_text())
    assert data["doc_id"] == "demo"
    assert data["page_count"] == 3
    assert data["docling_version"] == "0.0.0-fake"
    assert data["status"] == "ready"

    assert result.doc_id == "demo"
    assert result.page_count == 3
    assert result.duration_ms() >= 0


def test_convert_pdf_rejects_missing_input(tmp_path, fake_converter):
    with pytest.raises(cv.InputError):
        cv.convert_pdf(str(tmp_path / "no.pdf"), str(tmp_path / "out.md"))


def test_convert_pdf_rejects_non_md_output(tmp_path, sample_pdf, fake_converter):
    with pytest.raises(cv.InputError):
        cv.convert_pdf(str(sample_pdf), str(tmp_path / "out.txt"))


def test_convert_pdf_transient_on_docling_exception(tmp_path, sample_pdf, monkeypatch):
    """If the converter raises, surface as TransientError so the caller decides retry."""

    class _BadConverter:
        def convert(self, _path):
            raise RuntimeError("docling exploded")

    monkeypatch.setattr(cv, "_CONVERTER", _BadConverter())
    monkeypatch.setattr(cv, "_CONVERTER_VERSION", "0.0.0-fake")
    with pytest.raises(cv.TransientError):
        cv.convert_pdf(str(sample_pdf), str(tmp_path / "out.md"))


# ---------- FastAPI surface --------------------------------------------------


def test_healthz_calls_warm(fake_converter):
    client = TestClient(app)
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["docling_version"] == "0.0.0-fake"
    assert body["ocr_enabled"] is False


def test_convert_endpoint_happy(tmp_path, sample_pdf, fake_converter):
    client = TestClient(app)
    out = tmp_path / "out" / "demo.md"
    r = client.post(
        "/convert",
        json={"input_path": str(sample_pdf), "output_path": str(out)},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["doc_id"] == "demo"
    assert body["page_count"] == 3
    assert Path(body["output_path"]).exists()
    assert Path(body["sidecar_path"]).exists()


def test_convert_endpoint_400_on_missing_input(tmp_path, fake_converter):
    client = TestClient(app)
    r = client.post(
        "/convert",
        json={"input_path": str(tmp_path / "no.pdf"),
              "output_path": str(tmp_path / "out.md")},
    )
    assert r.status_code == 400
    assert "not found" in r.json()["detail"]

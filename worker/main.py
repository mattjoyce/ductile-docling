"""FastAPI app — thin adapter over worker.convert."""

from __future__ import annotations

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from worker import __version__
from worker import convert as cv


app = FastAPI(
    title="ductile-docling",
    version=__version__,
    description="Bespoke worker: PDF → Markdown via IBM docling. "
                "Designed to be sidecar'd next to the ductile gateway on the "
                "compose network and reached via http://docling:8080/.",
)


class ConvertRequest(BaseModel):
    input_path: str = Field(
        ...,
        description="Absolute path to the PDF (must be visible to this worker, "
                    "typically via a /workspace mount shared with the gateway).",
        examples=["/workspace/in/report.pdf"],
    )
    output_path: str = Field(
        ...,
        description="Absolute path where the markdown lands (.md). The JSON "
                    "sidecar is written alongside as <basename>.json. Parent "
                    "directory is created if missing.",
        examples=["/workspace/out/report.md"],
    )


class ConvertResponse(BaseModel):
    """Same shape as the on-disk JSON sidecar (worker.convert.ConvertResult)."""
    doc_id: str
    input_path: str
    output_path: str
    sidecar_path: str
    page_count: int
    parse_duration_seconds: float
    docling_version: str
    started_at: str
    completed_at: str
    status: str


@app.get("/healthz", tags=["meta"])
def healthz() -> dict:
    """Confirms docling actually loads, not just that uvicorn is alive."""
    return cv.warm()


@app.get("/v1/version", tags=["meta"])
def version() -> dict:
    return {
        "name": "ductile-docling",
        "version": __version__,
        "docling_version": cv._docling_version(),
    }


@app.post("/convert", response_model=ConvertResponse, tags=["convert"])
def convert(req: ConvertRequest) -> ConvertResponse:
    try:
        result = cv.convert_pdf(req.input_path, req.output_path)
    except cv.InputError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except cv.TransientError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return ConvertResponse(**result.to_dict())

# ductile-docling

**A bespoke FastAPI worker that converts PDFs to Markdown via [IBM docling](https://github.com/DS4SD/docling).**

Designed to run as a docker sidecar next to the ductile gateway. The gateway's
`docling-pdf` plugin POSTs a request over the internal compose network; this
worker reads the input PDF off a shared `/workspace` volume, writes the
markdown back to the same volume, and returns the metadata inline.

```
caller ─POST─▶ gateway:8888 ─pipeline─▶ docling-pdf plugin
                                            │
                                            │ POST http://docling:8080/convert
                                            ▼
                                     docling worker (this repo)
                                            │
                                            │ reads /workspace/in/foo.pdf
                                            │ writes /workspace/out/foo.md
                                            │     + /workspace/out/foo.json sidecar
                                            ▼
                                     gateway returns the tree inline
```

The worker is a sidecar, not a peer: it has no LAN port, no persistent state
beyond the HuggingFace model cache, and no awareness of pipelines, jobs, or
the ductile control plane. PDFs and markdown move via the shared volume —
the HTTP body only carries paths and metadata.

## Why a sidecar, not a full ductile satellite

The earlier iteration of this repo embedded a full ductile binary + plugin to
talk to the gateway over relay sync-reply. That worked, but it forced us to
maintain a second SQLite control plane, a second config-lock, plugin
discovery, version skew, and a 2.9 GB image — all to expose one stateless
function. The RFC's two-tier rule is:

- **Stateless transformers** (this repo): bespoke HTTP worker.
- **Stateful satellites** (durable queue, scheduled jobs, multi-step pipelines):
  full ductile + relay sync-reply or async.

See [`admin:rfc-ductile-relay-satellite.md`](https://github.com/mattjoyce/ductile/issues)
for the long form.

## Repository layout

```
ductile-docling/
├── Dockerfile                       single-stage python:3.12-slim, CPU torch, HF cache at /models
├── pyproject.toml                   declarative deps (docling, fastapi, uvicorn, pydantic)
├── docker-compose.example.yml       reference: how to add this as a sidecar to the gateway compose
├── worker/
│   ├── __init__.py
│   ├── main.py                      FastAPI app: POST /convert, GET /healthz, GET /v1/version
│   └── convert.py                   docling parse + atomic .json-then-.md write
├── tests/
│   └── test_convert.py              pytest smoke: input validation, atomic writes, FastAPI endpoints
├── LICENSE                          Apache-2.0
└── README.md                        you are here
```

## API

### `POST /convert`

```jsonc
// request
{
  "input_path":  "/workspace/in/foo.pdf",
  "output_path": "/workspace/out/foo.md"
}

// 200 OK — same shape as the on-disk JSON sidecar
{
  "doc_id":                 "foo",
  "input_path":             "/workspace/in/foo.pdf",
  "output_path":            "/workspace/out/foo.md",
  "sidecar_path":           "/workspace/out/foo.json",
  "page_count":             8,
  "parse_duration_seconds": 51.234,
  "docling_version":        "2.94.0",
  "started_at":             "2026-05-20T20:28:27Z",
  "completed_at":           "2026-05-20T20:29:18Z",
  "status":                 "ready"
}
```

Errors:

- `400 Bad Request` — input file missing, not a file, or `output_path`
  doesn't end in `.md`.
- `503 Service Unavailable` — docling raised mid-parse. Caller may retry,
  but a corrupt PDF will fail the same way.

### `GET /healthz`

Constructs the docling `DocumentConverter` (lightweight) and reports
docling's version. The actual ML models are lazy-loaded on the first
`POST /convert`; this endpoint is intentionally fast and is safe to call
from a compose `healthcheck`.

```jsonc
{ "status": "ok", "docling_version": "2.94.0", "ocr_enabled": false }
```

The first conversion after a fresh data volume downloads layout/table
models (~500 MB) into `/models`. Mount that path on a persistent volume
in compose so subsequent containers reuse the cache and parse fast
(~50 s on the unraid CPU profile for an 8-page paper).

### `GET /v1/version`

```jsonc
{ "name": "ductile-docling", "version": "0.1.0", "docling_version": "2.94.0" }
```

## Atomic write contract

The worker writes the `.json` sidecar **first** and the `.md` file **last**,
so a filewatcher keyed on `*.md` is the completion signal for the whole
artifact. Same contract as the abandoned in-process plugin — downstream
consumers don't change.

Both writes use a same-directory tempfile + `os.replace()` so the
publish-rename is atomic on the shared workspace volume.

## OCR

OCR is **disabled** in v1. docling's default OCR backend (rapidocr) writes
its model file to its own site-packages directory on first use, which
fails under our non-root container user. Text-bearing PDFs convert
cleanly without it. To re-enable: wire a writable OCR model cache and
expose it behind an `enable_ocr` request field.

## Local development

```bash
pip install -e ".[dev]"
pytest -q
uvicorn worker.main:app --reload --port 8080
```

The `worker/convert.py` test fixtures patch the docling converter, so the
test suite doesn't need the heavy model download.

## Deployment

See [`docker-compose.example.yml`](docker-compose.example.yml). Production
deployment lives in
[`unraid_admin/ductile/docker-compose.yml`](https://github.com/mattjoyce/admin)
on the operator host.

## License

Apache License 2.0 — see [LICENSE](LICENSE).

"""StatEase import API.

Run locally (inside the backend directory):
    uvicorn main:app --host 0.0.0.0 --port 8000

Environment:
    ALLOW_ORIGINS=https://your-site.vercel.app
    MAX_UPLOAD_MB=200
    STATEASE_KEY=                 # optional; never expose a secret in public HTML

Endpoints: GET/HEAD /, GET /health, POST /import, GET /docs.
"""

import json
import os
import shutil
import tempfile
from pathlib import Path

import pandas as pd
import pyreadr
import pyreadstat
from fastapi import FastAPI, File, Header, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware

MAX_MB = int(os.getenv("MAX_UPLOAD_MB", "200"))
KEY = os.getenv("STATEASE_KEY") or None
EXTENSIONS = {".dta", ".sav", ".rds", ".rdata", ".rda"}
ORIGINS = [s.strip().rstrip("/") for s in os.getenv("ALLOW_ORIGINS", "http://localhost:8000").split(",") if s.strip()]

app = FastAPI(title="StatEase import API", version="0.1.1")
app.add_middleware(
    CORSMiddleware,
    allow_origins=ORIGINS,
    allow_methods=["GET", "POST", "HEAD", "OPTIONS"],
    allow_headers=["*"],
)


def json_safe(value):
    """Coerce package metadata to JSON-compatible types."""
    return json.loads(json.dumps(value, default=str))


@app.api_route("/", methods=["GET", "HEAD"])
def home():
    return {
        "service": "StatEase import API",
        "status": "running",
        "version": app.version,
        "health": "/health",
        "documentation": "/docs",
        "import_endpoint": "/import",
        "supported_formats": sorted(EXTENSIONS),
    }


@app.get("/health")
def health():
    return {
        "ok": True,
        "pyreadstat": pyreadstat.__version__,
        "pandas": pd.__version__,
    }


@app.post("/import")
async def import_file(
    file: UploadFile = File(...),
    max_rows: int = Query(100000, ge=1, le=1000000),
    x_api_key: str | None = Header(default=None),
):
    if KEY and x_api_key != KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")

    extension = Path(file.filename or "").suffix.lower()
    if extension not in EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"Unsupported extension: {extension or '(none)'}")

    temp_dir = tempfile.mkdtemp(prefix="statease_")
    path = os.path.join(temp_dir, "upload" + extension)
    try:
        total_bytes = 0
        with open(path, "wb") as output:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                total_bytes += len(chunk)
                if total_bytes > MAX_MB * 1024 * 1024:
                    raise HTTPException(status_code=413, detail=f"File exceeds {MAX_MB} MB")
                output.write(chunk)
        if total_bytes == 0:
            raise HTTPException(status_code=400, detail="Uploaded file is empty")

        warnings = []

"""StatEase import API. Run: uvicorn main:app --host 0.0.0.0 --port 8000
Env: ALLOW_ORIGINS (comma list, your site URL), MAX_UPLOAD_MB (200), STATEASE_KEY (optional API key)."""
import os, json, shutil, tempfile
import pandas as pd, pyreadstat, pyreadr
from fastapi import FastAPI, UploadFile, File, HTTPException, Header, Query
from fastapi.middleware.cors import CORSMiddleware

MAX_MB = int(os.getenv("MAX_UPLOAD_MB", "200")); KEY = os.getenv("STATEASE_KEY")
EXT = {".dta", ".sav", ".rds", ".rdata", ".rda"}
app = FastAPI(title="StatEase import API")
app.add_middleware(CORSMiddleware, allow_origins=os.getenv("ALLOW_ORIGINS", "http://localhost:8000").split(","),
                   allow_methods=["GET", "POST"], allow_headers=["*"])
clean = lambda x: json.loads(json.dumps(x, default=str))

@app.get("/health")
def health():
    return {"ok": True, "pyreadstat": pyreadstat.__version__, "pandas": pd.__version__}

@app.post("/import")
async def import_file(file: UploadFile = File(...), max_rows: int = Query(100000, ge=1, le=1000000),
                      x_api_key: str | None = Header(None)):
    if KEY and x_api_key != KEY: raise HTTPException(401, "Invalid API key")
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in EXT: raise HTTPException(400, f"Unsupported extension {ext!r}")
    d = tempfile.mkdtemp(prefix="statease_"); p = os.path.join(d, "upload" + ext); size = 0
    try:
        with open(p, "wb") as o:
            while chunk := await file.read(1 << 20):
                size += len(chunk)
                if size > MAX_MB << 20: raise HTTPException(413, f"File exceeds {MAX_MB} MB")
                o.write(chunk)
        warn, cols = [], []
        if ext in (".dta", ".sav"):
            rd = pyreadstat.read_dta if ext == ".dta" else pyreadstat.read_sav
            _, mm = rd(p, metadataonly=True)
            n_obs = mm.number_rows
            df, m = rd(p, row_limit=max_rows, user_missing=False)  # user-defined missing -> NaN, never valid data
            warn.append("User-defined missing codes were converted to missing; they are not analysed as valid values.")
            for i, c in enumerate(m.column_names):
                um = (m.missing_ranges or {}).get(c) or (m.missing_user_values or {}).get(c)
                cols.append({"name": c, "label": m.column_labels[i], "type": str(df[c].dtype),
                             "value_labels": {str(k): v for k, v in (m.variable_value_labels.get(c) or {}).items()},
                             "user_missing": clean(um) if um else None})
            engine = f"pyreadstat {pyreadstat.__version__}"
        else:
            res = pyreadr.read_r(p)
            dfs = {k: v for k, v in res.items() if isinstance(v, pd.DataFrame)}
            if not dfs: raise HTTPException(422, "No data frame found in file")
            name = next(iter(dfs)); full = dfs[name]; n_obs = len(full); df = full.head(max_rows)
            if len(dfs) > 1: warn.append(f"{len(dfs)} data frames found; imported '{name}' only.")
            warn.append("R files carry no SPSS/Stata-style labels; pyreadr reads the whole file into memory.")
            cols = [{"name": str(c), "label": "", "type": str(df[c].dtype), "value_labels": {}, "user_missing": None} for c in df.columns]
            engine = f"pyreadr {pyreadr.__version__}"
        if n_obs and n_obs > len(df): warn.append(f"Only the first {len(df)} of {n_obs} rows were returned (max_rows).")
        rows = json.loads(df.to_json(orient="values", date_format="iso"))
        return {"engine": engine, "n_obs": n_obs, "n_vars": len(cols), "columns": cols, "rows": rows, "warnings": warn}
    except HTTPException: raise
    except Exception as e: raise HTTPException(422, f"Could not parse file ({type(e).__name__})")
    finally: shutil.rmtree(d, ignore_errors=True)

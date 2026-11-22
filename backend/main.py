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
        columns = []
        if extension in (".dta", ".sav"):
            reader = pyreadstat.read_dta if extension == ".dta" else pyreadstat.read_sav
            _, metadata_only = reader(path, metadataonly=True)
            n_obs = metadata_only.number_rows
            # Converting user-defined missing values to NaN avoids analysing them as observed values.
            df, metadata = reader(path, row_limit=max_rows, user_missing=False)
            warnings.append("User-defined missing values were converted to missing for analysis.")
            labels = metadata.column_labels or []
            variable_labels = metadata.variable_value_labels or {}
            missing_ranges = getattr(metadata, "missing_ranges", None) or {}
            missing_values = getattr(metadata, "missing_user_values", None) or {}
            for index, col in enumerate(metadata.column_names):
                missing_definition = missing_ranges.get(col) or missing_values.get(col)
                columns.append({
                    "name": col,
                    "label": labels[index] if index < len(labels) else None,
                    "type": str(df[col].dtype),
                    "value_labels": {str(k): v for k, v in (variable_labels.get(col) or {}).items()},
                    "user_missing": json_safe(missing_definition) if missing_definition else None,
                })
            engine = f"pyreadstat {pyreadstat.__version__}"
        else:
            # pyreadr loads the R file fully in memory; use only small files on free instances.
            result = pyreadr.read_r(path)
            frames = {name: item for name, item in result.items() if isinstance(item, pd.DataFrame)}
            if not frames:
                raise HTTPException(status_code=422, detail="No data frame found in R file")
            frame_name, full_df = next(iter(frames.items()))
            n_obs = len(full_df)
            df = full_df.head(max_rows)
            if len(frames) > 1:
                warnings.append(f"Found {len(frames)} data frames; imported {frame_name!r} only.")
            warnings.append("R files are read fully into memory; SPSS/Stata-style value-label metadata is not provided.")
            columns = [
                {"name": str(col), "label": "", "type": str(df[col].dtype),
                 "value_labels": {}, "user_missing": None}
                for col in df.columns
            ]
            engine = f"pyreadr {getattr(pyreadr, '__version__', 'unknown')}"

        if n_obs is not None and n_obs > len(df):
            warnings.append(f"Only the first {len(df)} of {n_obs} rows were returned (max_rows).")
        if n_obs is None:
            warnings.append("Source total row count was not available from metadata.")

        # json orient=values retains the original API's row-array response shape.
        rows = json.loads(df.to_json(orient="values", date_format="iso"))
        return {
            "engine": engine,
            "n_obs": n_obs,
            "n_vars": len(columns),
            "columns": columns,
            "rows": rows,
            "warnings": warnings,
        }
    except HTTPException:
        raise
    except (MemoryError,):
        raise HTTPException(status_code=413, detail="Dataset needs more memory than available on this server")
    except Exception as exc:
        # Avoid returning filenames, file contents or internal paths to clients.
        raise HTTPException(status_code=422, detail=f"Could not parse file ({type(exc).__name__})")
    finally:

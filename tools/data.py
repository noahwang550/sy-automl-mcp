"""Dataset tools: load_dataset, validate_dataset, and a shared reader."""
from __future__ import annotations

import base64
import binascii
import io
import json
import os
import re
import shutil
import threading
import urllib.request
from pathlib import Path
from typing import Any

import pandas as pd

from config import (
    MAX_DATASET_COLUMNS,
    MAX_DATASET_MB,
    MAX_DATASET_ROWS,
    MAX_UPLOAD_CHUNK_BYTES,
    MAX_UPLOAD_TOTAL_BYTES,
    dataset_chunks_path,
    dataset_path,
    validate_id,
)
from serialization import sample_rows

from ._common import envelope_call, safe_tool

# Single-file dataset layout: artifacts/datasets/<dataset_id>/data.<ext>
_DATA_FILENAME = "data"


def _detect_format(fmt: str, content_or_name: str) -> str:
    if fmt and fmt != "auto":
        return fmt
    low = content_or_name.lower()
    if low.endswith(".parquet"):
        return "parquet"
    if low.endswith(".json"):
        return "json"
    return "csv"  # default


def _read_df(buf_or_path: Any, fmt: str) -> pd.DataFrame:
    if fmt == "parquet":
        return pd.read_parquet(buf_or_path)
    if fmt == "json":
        return pd.read_json(buf_or_path)
    return pd.read_csv(buf_or_path)  # csv default


def _write_df(df: pd.DataFrame, path: Any, fmt: str) -> None:
    if fmt == "parquet":
        df.to_parquet(path, index=False)
    elif fmt == "json":
        df.to_json(path, orient="records")
    else:
        df.to_csv(path, index=False)


def _is_url(source: str) -> bool:
    return source.startswith("http://") or source.startswith("https://")


def _is_inline(source: str) -> bool:
    """Heuristic: inline CSV/JSON has a newline (multi-row) or looks like JSON."""
    return "\n" in source or source.lstrip().startswith("{")


def _enforce_size_limits(df: pd.DataFrame) -> None:
    """Reject datasets that exceed configured row/column/memory limits."""
    rows, cols = df.shape
    if rows > MAX_DATASET_ROWS:
        raise ValueError(
            f"Dataset exceeds row limit: {rows} > {MAX_DATASET_ROWS}"
        )
    if cols > MAX_DATASET_COLUMNS:
        raise ValueError(
            f"Dataset exceeds column limit: {cols} > {MAX_DATASET_COLUMNS}"
        )
    # Rough memory estimate (pandas overhead is larger, but this caps abuse).
    mb = df.memory_usage(deep=True).sum() / (1024 * 1024)
    if mb > MAX_DATASET_MB:
        raise ValueError(
            f"Dataset exceeds memory limit: {mb:.1f}MB > {MAX_DATASET_MB}MB"
        )


def dataset_file(dataset_id: str, fmt: str) -> str:
    """Return the on-disk filename for a dataset of a given format."""
    ext = {"csv": "csv", "parquet": "parquet", "json": "json"}.get(fmt, "csv")
    return f"{_DATA_FILENAME}.{ext}"


def read_dataset_df(dataset_id: str) -> pd.DataFrame:
    """Read a previously loaded dataset back into a DataFrame."""
    validate_id(dataset_id, "dataset_id")
    ddir = dataset_path(dataset_id)
    if not ddir.exists():
        raise FileNotFoundError(f"Dataset not found: {dataset_id}")
    candidates = sorted(ddir.glob(f"{_DATA_FILENAME}.*"))
    if not candidates:
        raise FileNotFoundError(f"Dataset {dataset_id} has no data file")
    ext = candidates[0].suffix.lower().lstrip(".")
    return _read_df(candidates[0], ext if ext in {"csv", "parquet", "json"} else "csv")


def _load_dataset(
    source: str,
    dataset_id: str,
    format: str = "auto",
) -> dict[str, Any]:
    """Core implementation for load_dataset (returns a plain dict)."""
    validate_id(dataset_id, "dataset_id")
    fmt = _detect_format(format, source)

    ddir = dataset_path(dataset_id)
    ddir.mkdir(parents=True, exist_ok=True)
    out_file = ddir / dataset_file(dataset_id, fmt)

    if _is_url(source):
        urllib.request.urlretrieve(source, out_file)  # noqa: S310 (trusted local tool)
    elif _is_inline(source):
        out_file.write_text(source, encoding="utf-8")
    else:
        # Treat as a relative filename within the datasets volume.
        validate_id(source, "source_filename")
        src = (ddir.parent / source) if "/" not in source else None
        if src is None or not src.exists():
            # Allow a file already placed directly under this dataset_id dir.
            src = ddir / source
        if not src.exists():
            raise FileNotFoundError(
                f"source {source!r} not found in datasets volume; pass inline "
                "content or an http(s) URL, or place the file under artifacts/datasets/"
            )
        df = _read_df(src, fmt)
        _write_df(df, out_file, fmt)

    df = _read_df(out_file, fmt)
    _enforce_size_limits(df)
    return {
        "dataset_id": dataset_id,
        "path": str(out_file),
        "rows": int(len(df)),
        "columns": list(df.columns),
        "dtypes": {c: str(t) for c, t in df.dtypes.items()},
        "sample": sample_rows(df, 5),
    }


def load_dataset(
    source: str,
    dataset_id: str,
    format: str = "auto",
) -> dict[str, Any]:
    """Import a dataset into artifacts/datasets and return an envelope.

    Args:
        source: One of:
            - inline CSV/JSON text (contains a newline or starts with ``{``),
            - an ``http(s)://`` URL to download,
            - a filename already present in the mounted ``artifacts/datasets/``
              volume (relative name only; absolute paths are rejected).
        dataset_id: Unique identifier for the dataset (letters/digits/_ . -).
        format: ``csv`` | ``parquet`` | ``json`` | ``auto``.

    Returns:
        Envelope with data containing rows, columns, dtypes, and a 5-row sample.
    """
    return envelope_call(_load_dataset, source, dataset_id, format)


def _validate_dataset(
    dataset_id: str,
    task_type: str | None = None,
    target: str | None = None,
    required_columns: list[str] | None = None,
) -> dict[str, Any]:
    """Core implementation for validate_dataset."""
    df = read_dataset_df(dataset_id)
    columns = list(df.columns)
    issues: list[str] = []

    for col in required_columns or []:
        if col not in columns:
            issues.append(f"Missing required column: {col}")

    if target is not None:
        if target not in columns:
            issues.append(f"Target column not found: {target}")
        else:
            nunique = int(df[target].nunique(dropna=True))
            if task_type == "classification" and nunique < 2:
                issues.append(
                    f"Target {target} has {nunique} unique values; classification needs >=2"
                )

    missing = {c: int(df[c].isna().sum()) for c in columns if df[c].isna().any()}

    return {
        "dataset_id": dataset_id,
        "valid": len(issues) == 0,
        "issues": issues,
        "rows": int(len(df)),
        "columns": columns,
        "inferred_dtypes": {c: str(t) for c, t in df.dtypes.items()},
        "missing_counts": missing,
        "recommended_action": "ok" if not issues else "fix issues before training",
    }


def validate_dataset(
    dataset_id: str,
    task_type: str | None = None,
    target: str | None = None,
    required_columns: list[str] | None = None,
) -> dict[str, Any]:
    """Validate a dataset before training. Reports missing values and type issues."""
    return envelope_call(_validate_dataset, dataset_id, task_type, target, required_columns)


def read_inline_or_dataset(
    dataset_id: str | None, inline_csv: str | None
) -> pd.DataFrame:
    """Resolve exactly one of (dataset_id, inline_csv) to a DataFrame."""
    if (dataset_id is None) == (inline_csv is None):
        raise ValueError("Provide exactly one of dataset_id or inline_csv")
    if dataset_id is not None:
        return read_dataset_df(dataset_id)
    return pd.read_csv(io.StringIO(inline_csv))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Single-shot base64 upload (primary path for agent-attached files).
# ---------------------------------------------------------------------------

_DATA_URI_RE = re.compile(r"^data:[^;,]*(?:;[^;,]*)*;base64,", re.IGNORECASE)


def _decode_b64(content: str, label: str = "content_base64") -> bytes:
    """Decode base64 tolerantly: strip data-URI prefix + whitespace, then validate.

    Agent/LLM callers frequently produce base64 with a ``data:<mime>;base64,``
    prefix or line-wrapped (76-col) whitespace. The strict ``validate=True``
    path rejects those outright; this helper normalizes first so a well-formed
    payload survives transit through prompt-driven tool calls.
    """
    if not isinstance(content, str) or not content:
        raise ValueError(f"{label} must be a non-empty string")
    s = content.strip()
    m = _DATA_URI_RE.match(s)
    if m:
        s = s[m.end():]
    s = re.sub(r"\s+", "", s)
    try:
        return base64.b64decode(s, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(
        f"{label} is not valid base64: {exc}. Expected raw base64 "
        f"(A-Z a-z 0-9 + / =); got {len(content)} chars, first 40: "
        f"{content[:40]!r}"
    ) from exc


def _upload_dataset(
    dataset_id: str,
    content_base64: str,
    format: str = "auto",
) -> dict[str, Any]:
    """Decode a base64-encoded file and store it as a dataset in one shot."""
    validate_id(dataset_id, "dataset_id")
    if not isinstance(content_base64, str) or not content_base64:
        raise ValueError("content_base64 must be a non-empty string")

    decoded = _decode_b64(content_base64)
    if len(decoded) > MAX_UPLOAD_CHUNK_BYTES:
        raise ValueError(
            f"decoded size {len(decoded)} exceeds MAX_UPLOAD_CHUNK_BYTES "
            f"{MAX_UPLOAD_CHUNK_BYTES}; switch to upload_dataset_chunk + "
            f"finalize_dataset (split the file into <= "
            f"{MAX_UPLOAD_CHUNK_BYTES}-byte chunks)"
        )

    fmt = _detect_format(format, "data.csv")
    ddir = dataset_path(dataset_id)
    ddir.mkdir(parents=True, exist_ok=True)
    out_file = ddir / dataset_file(dataset_id, fmt)
    out_file.write_bytes(decoded)
    if out_file.stat().st_size > MAX_UPLOAD_TOTAL_BYTES:
        out_file.unlink(missing_ok=True)
        raise ValueError(
            f"assembled size {out_file.stat().st_size} exceeds "
            f"MAX_UPLOAD_TOTAL_BYTES {MAX_UPLOAD_TOTAL_BYTES}"
        )

    df = _read_df(out_file, fmt)
    _enforce_size_limits(df)
    return {
        "dataset_id": dataset_id,
        "path": str(out_file),
        "rows": int(len(df)),
        "columns": list(df.columns),
        "dtypes": {c: str(t) for c, t in df.dtypes.items()},
        "sample": sample_rows(df, 5),
    }


def upload_dataset(
    dataset_id: str,
    content_base64: str,
    format: str = "auto",
) -> dict[str, Any]:
    """Upload a file as base64 and store it as a dataset — single shot.

    The **primary path** for files the agent already has in memory (e.g.
    session attachments that live in a storage domain the MCP container
    cannot reach via http(s) or local mount). Base64-encode the raw file
    bytes and pass the whole blob in one call. Returns the same envelope as
    ``load_dataset``, so the ``dataset_id`` is immediately usable with
    ``train_tabular`` / ``predict_tabular`` / etc.

    Args:
        dataset_id: Unique identifier (letters/digits/_ . -).
        content_base64: Base64-encoded raw bytes of the file (for CSV/JSON,
            encode the raw text bytes; for parquet, the raw parquet bytes).
        format: ``csv`` | ``parquet`` | ``json`` | ``auto``.

    Returns:
        Envelope with dataset_id, path, rows, columns, dtypes, sample.

    Fallback: if the file is too large for one request (error mentions
    MAX_UPLOAD_CHUNK_BYTES), split the raw bytes into <=
    MAX_UPLOAD_CHUNK_BYTES-byte chunks, base64-encode each, and call
    ``upload_dataset_chunk`` for chunk_index 0..N-1 then
    ``finalize_dataset``.
    """
    return envelope_call(_upload_dataset, dataset_id, content_base64, format)


# ---------------------------------------------------------------------------
# Chunked upload (escape hatch for attachments that live in a separate
# storage domain from this container — e.g. agent-platform session
# attachments that cannot be inlined or fetched via http(s)).
# ---------------------------------------------------------------------------

_CHUNK_META_LOCK = threading.Lock()


def _chunk_meta_path(chunks_dir: Path) -> Path:
    return chunks_dir / "meta.json"


def _read_chunk_meta(chunks_dir: Path) -> dict:
    meta_p = _chunk_meta_path(chunks_dir)
    if not meta_p.exists():
        return {}
    try:
        with meta_p.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _write_chunk_meta(chunks_dir: Path, meta: dict) -> None:
    chunks_dir.mkdir(parents=True, exist_ok=True)
    meta_p = _chunk_meta_path(chunks_dir)
    tmp = meta_p.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    tmp.replace(meta_p)


def _coerce_int(value: Any, label: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be an integer, got {value!r}") from exc


def _upload_dataset_chunk(
    dataset_id: str,
    chunk_index: int,
    total_chunks: int,
    content_base64: str,
    format: str = "auto",
) -> dict[str, Any]:
    validate_id(dataset_id, "dataset_id")
    chunk_index = _coerce_int(chunk_index, "chunk_index")
    total_chunks = _coerce_int(total_chunks, "total_chunks")
    if total_chunks < 1:
        raise ValueError(f"total_chunks must be >= 1, got {total_chunks}")
    if not (0 <= chunk_index < total_chunks):
        raise ValueError(
            f"chunk_index {chunk_index} out of range [0, {total_chunks})"
        )
    if not isinstance(content_base64, str) or not content_base64:
        raise ValueError("content_base64 must be a non-empty string")

    decoded = _decode_b64(content_base64)
    if len(decoded) > MAX_UPLOAD_CHUNK_BYTES:
        raise ValueError(
            f"chunk size {len(decoded)} exceeds MAX_UPLOAD_CHUNK_BYTES "
            f"{MAX_UPLOAD_CHUNK_BYTES}"
        )

    chunks_dir = dataset_chunks_path(dataset_id)
    chunks_dir.mkdir(parents=True, exist_ok=True)
    chunk_file = chunks_dir / f"chunk.{chunk_index:06d}"
    with chunk_file.open("wb") as f:
        f.write(decoded)

    with _CHUNK_META_LOCK:
        meta = _read_chunk_meta(chunks_dir)
        declared_total = meta.get("total_chunks")
        if declared_total is not None and declared_total != total_chunks:
            raise ValueError(
                f"total_chunks mismatch: previously declared {declared_total}, "
                f"now {total_chunks}"
            )
        indices = set(meta.get("received_indices", []))
        indices.add(chunk_index)
        meta.update(
            {
                "total_chunks": total_chunks,
                "format": format,
                "received_indices": sorted(indices),
            }
        )
        _write_chunk_meta(chunks_dir, meta)

    return {
        "dataset_id": dataset_id,
        "chunk_index": chunk_index,
        "total_chunks": total_chunks,
        "bytes_received": len(decoded),
        "received_indices": sorted(indices),
    }


def upload_dataset_chunk(
    dataset_id: str,
    chunk_index: int,
    total_chunks: int,
    content_base64: str,
    format: str = "auto",
) -> dict[str, Any]:
    """Append one base64-encoded chunk of a dataset into artifacts/datasets/<id>/.chunks/.

    Use this when the source data is too large to inline in a single
    load_dataset call and cannot be fetched via http(s) — e.g. agent-platform
    session attachments that live in a separate storage domain. After all
    chunks are uploaded, call finalize_dataset to assemble them.

    Args:
        dataset_id: Unique identifier (letters/digits/_ . -).
        chunk_index: Zero-based index of this chunk (0 .. total_chunks-1).
        total_chunks: Total number of chunks that will be uploaded.
        content_base64: Base64-encoded raw bytes of this chunk (file content;
            for CSV/JSON, encode the raw text bytes).
        format: ``csv`` | ``parquet`` | ``json`` | ``auto`` (recorded for
            finalize; the last write wins).

    Returns:
        Envelope with chunk_index, total_chunks, bytes_received, and the
        sorted list of received_indices so far.
    """
    return envelope_call(
        _upload_dataset_chunk,
        dataset_id,
        chunk_index,
        total_chunks,
        content_base64,
        format,
    )


def _finalize_dataset(dataset_id: str, format: str = "auto") -> dict[str, Any]:
    validate_id(dataset_id, "dataset_id")
    chunks_dir = dataset_chunks_path(dataset_id)
    meta = _read_chunk_meta(chunks_dir)
    total_chunks = meta.get("total_chunks")
    if not total_chunks:
        raise FileNotFoundError(
            f"No upload in progress for dataset {dataset_id!r} "
            "(missing .chunks/meta.json)"
        )

    fmt_src = format if format and format != "auto" else meta.get("format", "auto")
    fmt = _detect_format(fmt_src, "data.csv")

    received = set(meta.get("received_indices", []))
    expected = set(range(total_chunks))
    missing = sorted(expected - received)
    if missing:
        raise FileNotFoundError(
            f"Cannot finalize: missing {len(missing)} chunk(s) for dataset "
            f"{dataset_id!r}: {missing} of {total_chunks}"
        )

    ddir = dataset_path(dataset_id)
    ddir.mkdir(parents=True, exist_ok=True)
    out_file = ddir / dataset_file(dataset_id, fmt)
    tmp_file = out_file.with_suffix(out_file.suffix + ".tmp")
    if tmp_file.exists():
        tmp_file.unlink()
    try:
        with tmp_file.open("wb") as out:
            for i in range(total_chunks):
                cf = chunks_dir / f"chunk.{i:06d}"
                with cf.open("rb") as src:
                    shutil.copyfileobj(src, out, length=1024 * 1024)
        total_bytes = tmp_file.stat().st_size
        if total_bytes > MAX_UPLOAD_TOTAL_BYTES:
            raise ValueError(
                f"assembled size {total_bytes} exceeds MAX_UPLOAD_TOTAL_BYTES "
                f"{MAX_UPLOAD_TOTAL_BYTES}"
            )
        os.replace(tmp_file, out_file)
    except Exception:
        tmp_file.unlink(missing_ok=True)
        raise

    try:
        df = _read_df(out_file, fmt)
        _enforce_size_limits(df)
    except Exception:
        # Roll back the assembled file and chunks so the caller can retry.
        out_file.unlink(missing_ok=True)
        shutil.rmtree(chunks_dir, ignore_errors=True)
        raise

    try:
        shutil.rmtree(chunks_dir)
    except OSError:
        pass

    return {
        "dataset_id": dataset_id,
        "path": str(out_file),
        "rows": int(len(df)),
        "columns": list(df.columns),
        "dtypes": {c: str(t) for c, t in df.dtypes.items()},
        "sample": sample_rows(df, 5),
    }


def finalize_dataset(dataset_id: str, format: str = "auto") -> dict[str, Any]:
    """Assemble previously uploaded chunks into artifacts/datasets/<id>/data.<ext>.

    Concatenates chunks 0..total_chunks-1 in order, atomically replaces the
    target data file, applies dataset size limits, removes the .chunks/
    directory, and returns the same envelope shape as load_dataset.

    Args:
        dataset_id: The id used in prior upload_dataset_chunk calls.
        format: ``csv`` | ``parquet`` | ``json`` | ``auto`` (defaults to the
            format recorded during chunk upload).
    """
    return envelope_call(_finalize_dataset, dataset_id, format)


# Wrap public tools so direct imports also return the unified envelope.
load_dataset = safe_tool(load_dataset)
validate_dataset = safe_tool(validate_dataset)
upload_dataset = safe_tool(upload_dataset)
upload_dataset_chunk = safe_tool(upload_dataset_chunk)
finalize_dataset = safe_tool(finalize_dataset)

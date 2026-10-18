"""Explicit verification, outside hourly collection.

A reader takes no lock: per-source records publish through atomic ``current``
swaps, and the snapshot's own ``derived/current`` link swaps atomically. The
snapshot is a disposable cache — a missing, unreadable or stale manifest makes
verify report against the live views only, and a corrupt snapshot falls back
to live rather than failing the read.
"""

import contextlib
import hashlib
import json
import sys

from . import query, store


def _digest(con, view):
    digest, count = hashlib.sha256(), 0
    cursor = con.execute(f"SELECT * FROM {view} ORDER BY ALL")  # noqa: S608 — internal fixed view names
    schema = [(column[0], str(column[1])) for column in cursor.description]
    digest.update(json.dumps(schema, ensure_ascii=True).encode() + b"\n")
    while rows := cursor.fetchmany(1000):
        for row in rows:
            digest.update(json.dumps(row, default=str, ensure_ascii=True).encode() + b"\n")
            count += 1
    return {"rows": count, "sha256": digest.hexdigest()}


def _manifest(data):
    """The snapshot manifest when it exists and matches the store today, else None."""
    current = data / "derived" / "current"
    try:
        manifest = json.loads((current / "MANIFEST.json").read_text())
    except (OSError, ValueError):
        return None
    if manifest.get("views_version") != query.VIEWS_VERSION:
        return None
    if manifest.get("inputs_signature") != store.inputs_signature(data):
        return None
    return manifest


def _snapshot_connection(data):
    """DuckDB views bound to the snapshot's per-view parquet files."""
    import duckdb

    con = duckdb.connect()
    current = data / "derived" / "current"
    for view in query._DERIVED:
        out = str(current / f"{view}.parquet").replace("'", "''")
        con.execute(
            f"CREATE VIEW {view} AS SELECT * FROM read_parquet('{out}')"  # noqa: S608 — fixed names, escaped path
        )
    return con


def verify(data):
    keys, excluded = store.compatible_sources(data)
    manifest = _manifest(data)
    if manifest is None:
        return {
            "ok": False,
            "unavailable": "no current snapshot; run derive",
            "coverage": {"included": len(keys), "excluded": excluded},
            "snapshot_current": False,
        }
    with contextlib.closing(query.connect(data)) as live:
        comparisons = {v: {"live": _digest(live, v)} for v in query._DERIVED}
    snapshot_ok = False
    try:
        # DuckDB can bind a read_parquet view without scanning it; damage can
        # surface at row-read time, so the whole digest pass stays inside try.
        with contextlib.closing(_snapshot_connection(data)) as snap:
            for v in query._DERIVED:
                comparisons[v]["snapshot"] = _digest(snap, v)
        snapshot_ok = True
    except Exception as exc:  # noqa: BLE001 — a corrupt snapshot falls back to live
        print(f"transcript-analytics verify: snapshot unreadable ({exc!r}); live views only", file=sys.stderr)
    return {
        "ok": snapshot_ok
        and all(v["snapshot"] == v["live"] for v in comparisons.values())
        and not excluded,
        "coverage": {"included": len(keys), "excluded": excluded},
        "snapshot_current": True,
        "views": comparisons,
    }

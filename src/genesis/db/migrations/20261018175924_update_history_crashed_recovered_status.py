"""Add 'crashed_recovered' to the update_history status CHECK constraint.

bootstrap.sh records the crash recovery of an interrupted update with
``status = 'crashed_recovered'``, but after 0024 the CHECK allowed only
('success', 'failed', 'rolled_back', 'conflicts_pending'). The INSERT raised
IntegrityError, so a recovered crash was never recorded.

SQLite cannot ALTER a CHECK constraint, so this is a full table rebuild, the
same shape as 0024: create ``update_history_new`` with the widened CHECK, copy
every row (``rowid`` included), drop the original, rename into place. Beyond
0024 it also:

- recreates every explicit index and trigger the live table carried (the
  canonical schema has none; any found is one an install added, and DROP TABLE
  would silently take it);
- refuses, before changing anything, when the live table has a column the
  rebuild target does not declare (its data would be dropped), or when an object
  named ``update_history_new`` already exists. 0024 dropped that name as a
  leftover of a failed attempt, but the runner owns the transaction and rolls a
  failed attempt back, so anything holding the name is someone else's.

Idempotent: skipped when the live DDL already allows ``'crashed_recovered'``.
No ``db.commit()`` — the runner owns the transaction.
"""

from __future__ import annotations

import aiosqlite

_SCRATCH_TABLE = "update_history_new"

_COLUMNS = (
    "id",
    "old_tag",
    "new_tag",
    "old_commit",
    "new_commit",
    "status",
    "rollback_tag",
    "failure_reason",
    "degraded_subsystems",
    "started_at",
    "completed_at",
)

_WIDE_STATUSES = "'success', 'failed', 'rolled_back', 'conflicts_pending', 'crashed_recovered'"
_NARROW_STATUSES = "'success', 'failed', 'rolled_back', 'conflicts_pending'"

_CREATE_TEMPLATE = """
    CREATE TABLE update_history_new (
        id TEXT PRIMARY KEY,
        old_tag TEXT NOT NULL,
        new_tag TEXT NOT NULL,
        old_commit TEXT NOT NULL,
        new_commit TEXT NOT NULL,
        status TEXT NOT NULL
            CHECK (status IN ({statuses})),
        rollback_tag TEXT,
        failure_reason TEXT,
        degraded_subsystems TEXT,
        started_at TEXT NOT NULL,
        completed_at TEXT NOT NULL
    )
"""


async def _live_ddl(db: aiosqlite.Connection) -> str | None:
    cursor = await db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='update_history'"
    )
    row = await cursor.fetchone()
    return (row[0] or "") if row else None


async def _rebuild(db: aiosqlite.Connection, *, statuses: str) -> None:
    cursor = await db.execute(
        "SELECT type, name FROM sqlite_master WHERE lower(name) = ? "
        "UNION ALL SELECT type, name FROM sqlite_temp_master WHERE lower(name) = ?",
        (_SCRATCH_TABLE, _SCRATCH_TABLE),
    )
    squatter = await cursor.fetchone()
    if squatter:
        raise RuntimeError(
            f"update_history rebuild: a {squatter[0]} named {squatter[1]!r} already "
            f"exists and this migration needs the name {_SCRATCH_TABLE!r} for its "
            "scratch table. It is not the migration's to drop: rename or remove it, "
            "then re-run. Nothing was changed."
        )

    cursor = await db.execute("PRAGMA table_xinfo(update_history)")
    live_cols = [r[1] for r in await cursor.fetchall()]
    unknown = [c for c in live_cols if c not in _COLUMNS]
    if unknown:
        raise RuntimeError(
            f"update_history rebuild: live column(s) {unknown} are not in the rebuild "
            "target; refusing to drop their data. Nothing was changed."
        )

    # Explicit indexes and triggers on the table (autoindexes have NULL sql and
    # come back with the PRIMARY KEY). DROP TABLE removes both, so recreate them.
    cursor = await db.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' "
        "AND tbl_name='update_history' AND sql IS NOT NULL ORDER BY rowid"
    )
    live_indexes = [r[0] for r in await cursor.fetchall()]
    cursor = await db.execute(
        "SELECT sql FROM sqlite_master WHERE type='trigger' "
        "AND tbl_name='update_history' AND sql IS NOT NULL ORDER BY rowid"
    )
    live_triggers = [r[0] for r in await cursor.fetchall()]

    await db.execute(_CREATE_TEMPLATE.format(statuses=statuses))

    collist = ", ".join(c for c in _COLUMNS if c in live_cols)
    # Identifiers are in-repo literals filtered by PRAGMA output, never user input.
    await db.execute(
        f"INSERT INTO update_history_new (rowid, {collist}) "  # noqa: S608
        f"SELECT rowid, {collist} FROM update_history"
    )

    await db.execute("DROP TABLE update_history")
    await db.execute("ALTER TABLE update_history_new RENAME TO update_history")
    for stmt in live_indexes:
        await db.execute(stmt)
    for stmt in live_triggers:
        await db.execute(stmt)


async def up(db: aiosqlite.Connection) -> None:
    ddl = await _live_ddl(db)
    if ddl is None:
        return  # Table absent — defensive guard for non-sequential edge cases
    if "'crashed_recovered'" in ddl:
        return  # Already has the expanded constraint
    await _rebuild(db, statuses=_WIDE_STATUSES)


async def down(db: aiosqlite.Connection) -> None:
    """Revert to the 0024 constraint, keeping recorded crashes as 'failed'.

    0024's down() dropped the rows its status allowed; a recorded crash is worth
    more than that, and its failure_reason ("Crashed in phase: ...") still says
    what happened, so the row is kept under the closest older status.
    """
    ddl = await _live_ddl(db)
    if ddl is None or "'crashed_recovered'" not in ddl:
        return  # Absent or already reverted
    await db.execute(
        "UPDATE update_history SET status = 'failed' WHERE status = 'crashed_recovered'"
    )
    await _rebuild(db, statuses=_NARROW_STATUSES)

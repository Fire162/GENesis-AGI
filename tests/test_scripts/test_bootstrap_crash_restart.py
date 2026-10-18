"""A crash recovery that found genesis-server stopped hands the restart to the next update (#3451).

bootstrap.sh never starts genesis-server. When the crashed update had stopped it,
the next update.sh run saw nothing running and took the operator-stop branch:
server left down, success recorded, no health check. The fix: bootstrap's crash
recovery writes ``~/.genesis/last_update_failure.json`` (update.sh's own failure
artifact, in its writer's format) ONLY when the crashed state's
``services_stopped`` names genesis-server. update.sh's recovery detection already
reads that file as "restore genesis-server and health-check it". A crash before
the stop (``fetching``, empty ``services_stopped``) writes nothing, so a server
the operator stopped on purpose stays stopped.

The recovery block and update.sh's detection block run as extracted from the
shipped scripts, in a temporary HOME with a database built by the real
migrations; the two readers of the file (the dashboard status route and the
genesis_version collector) are called for real against what bootstrap wrote.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
from pathlib import Path
from unittest.mock import patch

from tests.test_scripts.test_bootstrap_guards import (
    UPDATE,
    _assert_recovered,
    _history,
    _migrated_genesis_root,
    _recover,
    _recovery_commit_file,
    _recovery_repo,
)
from tests.test_scripts.test_update_rollback_recovery import _extract_recovery_block


def _failure_file(tmp_path: Path) -> Path:
    return tmp_path / "home" / ".genesis" / "last_update_failure.json"


def _update_sh_failure_keys() -> set[str]:
    """The keys update.sh's own writer puts in last_update_failure.json."""
    text = UPDATE.read_text()
    start = text.index("data = {", text.index("# Write failure context for CC to pick up."))
    block = text[start : text.index("}", start)]
    keys = set(re.findall(r"'(\w+)':\s+sys\.argv", block))
    assert len(keys) == 10, keys
    return keys


def _with_observations(db: Path) -> None:
    """The migration chain alone does not create ``observations`` (bootstrap's
    create_all_tables does); add it from the shipped schema for the readers."""
    import sqlite3

    from genesis.db.schema import TABLES

    con = sqlite3.connect(db)
    try:
        con.executescript(TABLES["observations"])
    finally:
        con.close()


def _crashed_merge(tmp_path: Path, **state):
    root, _, rollback = _recovery_repo(tmp_path)
    deploy = _recovery_commit_file(root, "changed.txt", "deployed\n", "deploy")
    db = _migrated_genesis_root(tmp_path, root)
    proc, state_path = _recover(
        tmp_path,
        root,
        own_head=deploy,
        deploy_head=deploy,
        old_tag="v1.0.0",
        old_commit=rollback[:7],
        started_at="2026-10-18T00:00:00+00:00",
        **state,
    )
    _assert_recovered(proc, state_path, root, rollback)
    _with_observations(db)
    return root, db, deploy, proc


def _update_detects_recovery(tmp_path: Path, root: Path) -> bool:
    """Run update.sh's shipped recovery detection with nothing running, in the
    same HOME and against the same database bootstrap just recovered."""
    harness = (
        "#!/bin/bash\nset -Eeuo pipefail\nWERE_RUNNING=()\n"
        f'GENESIS_ROOT="{root}"\nVENV_DIR="{tmp_path / "missing-venv"}"\n'
        + _extract_recovery_block(UPDATE.read_text())
        + '\nprintf "%s\\n" "${WERE_RUNNING[@]:-}"\n'
    )
    out = subprocess.run(
        ["bash", "-c", harness],
        env={**os.environ, "HOME": str(tmp_path / "home")},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert out.returncode == 0, out.stderr
    return "genesis-server" in out.stdout.split()


def test_a_crash_that_stopped_the_server_leaves_update_sh_failure_record(tmp_path):
    root, db, deploy, proc = _crashed_merge(
        tmp_path, services_stopped=["genesis-server", "genesis-bridge"]
    )

    failure = _failure_file(tmp_path)
    assert failure.is_file(), proc.stdout + proc.stderr
    data = json.loads(failure.read_text())
    assert set(data) == _update_sh_failure_keys()
    assert data["old_tag"] == "v1.0.0"
    assert data["rollback_tag"] == "rollback"
    assert data["original_branch"] == "main"
    assert data["new_commit"] == deploy[:7]
    assert data["new_tag"] == "untagged"  # the fixture has no v* tag; update.sh's fallback
    assert data["rollback_complete"] is True
    assert "phase 'merging'" in data["reason"] and "bootstrap" in data["reason"]
    assert isinstance(data["timestamp"], str) and data["timestamp"]
    # The update_history row is still the crash row (PR #3446), alongside the file.
    assert [row[0] for row in _history(db)] == ["crashed_recovered"]
    assert "WARNING" not in proc.stdout + proc.stderr


def test_the_dashboard_status_route_reports_the_bootstrap_written_failure(tmp_path, monkeypatch):
    import sqlite3

    from flask import Flask

    from tests._update_routes_sandbox import isolate_update_routes

    root, db, _, _ = _crashed_merge(tmp_path, services_stopped=["genesis-server"])
    written = json.loads(_failure_file(tmp_path).read_text())
    isolate_update_routes(monkeypatch, tmp_path / "home", root)
    from genesis.dashboard.routes import updates

    def plain_query(sql, params=()):
        con = sqlite3.connect(str(db))
        con.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in con.execute(sql, params).fetchall()]
        finally:
            con.close()

    with (
        Flask(__name__).test_request_context(),
        patch.object(updates, "_git", return_value=None),
        patch.object(updates, "_query_db", side_effect=plain_query),
    ):
        body = updates.update_status().get_json()
    assert body["last_failure"] == written
    assert body["last_update"]["status"] == "crashed_recovered"


def test_the_version_collector_records_and_archives_the_bootstrap_written_failure(
    tmp_path, monkeypatch
):
    import aiosqlite

    from genesis.learning.signals import genesis_version

    _, db, _, _ = _crashed_merge(tmp_path, services_stopped=["genesis-server"])
    failure = _failure_file(tmp_path)
    written = json.loads(failure.read_text())
    archive = tmp_path / "home" / ".genesis" / "update-failures"
    monkeypatch.setattr(genesis_version, "_FAILURE_FILE", failure)
    monkeypatch.setattr(genesis_version, "_FAILURE_ARCHIVE_DIR", archive)

    async def _consume() -> list[tuple[str, str]]:
        async with aiosqlite.connect(str(db)) as conn:
            collector = genesis_version.GenesisVersionCollector(conn)
            with patch.object(genesis_version, "update_in_progress", return_value=False):
                await collector._check_failure_file()
                # A second tick neither re-reads nor re-records it.
                await collector._check_failure_file()
            cursor = await conn.execute(
                "SELECT type, content FROM observations WHERE source = 'genesis_version'"
            )
            return list(await cursor.fetchall())

    rows = asyncio.run(_consume())
    assert len(rows) == 1 and rows[0][0] == "genesis_update_failed"
    assert json.loads(rows[0][1]) == written
    assert not failure.exists()
    archived = list(archive.glob("last_update_failure.*.json"))
    assert len(archived) == 1 and json.loads(archived[0].read_text()) == written


def test_the_next_update_restores_the_server_the_crash_stopped(tmp_path):
    root, _, _, _ = _crashed_merge(tmp_path, services_stopped=["genesis-server"])
    assert _update_detects_recovery(tmp_path, root) is True


def test_a_crash_before_the_stop_writes_nothing_and_keeps_an_operator_stop(tmp_path):
    """fetching is written before services are inspected, so services_stopped is
    empty: the server's state is the operator's, and the next update keeps it."""
    root, _, rollback = _recovery_repo(tmp_path)
    _migrated_genesis_root(tmp_path, root)
    proc, state_path = _recover(tmp_path, root, phase="fetching", services_stopped=[])

    _assert_recovered(proc, state_path, root, rollback)
    assert not _failure_file(tmp_path).exists()
    assert _update_detects_recovery(tmp_path, root) is False


def test_a_crash_that_stopped_only_the_bridge_writes_nothing(tmp_path):
    _crashed_merge(tmp_path, services_stopped=["genesis-bridge"])
    assert not _failure_file(tmp_path).exists()


def test_an_existing_failure_record_is_left_untouched(tmp_path):
    failure = _failure_file(tmp_path)
    failure.parent.mkdir(parents=True)
    original = '{"reason": "update.sh rolled back", "timestamp": "earlier"}\n'
    failure.write_text(original)

    _crashed_merge(tmp_path, services_stopped=["genesis-server"])
    assert failure.read_text() == original

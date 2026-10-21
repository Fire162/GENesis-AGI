"""Retention and selection fail closed over every physical source revision."""

import json
import shutil
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from genesis.transcript_analytics import store
from genesis.transcript_analytics.extract import TABLES

FIXTURES = Path(__file__).with_name("fixtures")


def _write(path, timestamp):
    path.write_text(json.dumps({
        "type": "assistant", "uuid": "event", "sessionId": "session",
        "timestamp": timestamp, "message": {
            "id": "message", "model": "fixture", "content": [],
            "usage": {"input_tokens": 1, "output_tokens": 1},
        },
    }) + "\n")


def _seed(tmp_path):
    projects, data = tmp_path / "projects", tmp_path / "data"
    projects.mkdir()
    path = projects / "source.jsonl"
    _write(path, "2026-02-10T00:00:00Z")
    assert store.ingest(projects, data)["rebuilt"] == 1
    return projects, data, path, store.select_sources(data)[0][0]


def _flatten(data, record):
    for table in TABLES:
        shutil.copy2(record.path(table), data / f"{table}__{record.key}.parquet")
    (record.directory.parent / "current").unlink()
    shutil.rmtree(record.directory)


@pytest.mark.parametrize("layout", ["revision", "flat"])
@pytest.mark.parametrize("retained", ["newer", "unknown", "unreadable", "expired"])
def test_prune_checks_all_retained_markers(tmp_path, layout, retained):
    projects, data, path, original = _seed(tmp_path)
    if layout == "flat":
        _flatten(data, original)
        marker = data / f"fragments__{original.key}.parquet"
    else:
        marker = original.path("fragments")
    if retained == "unreadable":
        marker.write_bytes(b"corrupt")
    elif retained != "newer":
        body = pq.ParquetFile(marker).read()
        metadata = dict(body.schema.metadata)
        metadata[b"ta.last_ts"] = (
            b"2025-01-01T00:00:00Z" if retained == "expired" else b""
        )
        pq.write_table(body.replace_schema_metadata(metadata), marker)
    _write(path, "2026-01-01T00:00:00Z")
    assert store.ingest(projects, data)["rebuilt"] == 1
    current = store.select_sources(data)[0][0]
    path.unlink()
    removed = store.prune(data, before="2026-02-01", projects=projects)
    assert removed == int(retained == "expired")
    if retained != "expired":
        assert marker.exists() and current.path("fragments").exists()
        assert store.select_sources(data)[0][0].directory == current.directory


@pytest.mark.parametrize("relative", ["../missing.jsonl", "/missing.jsonl", "escape/missing.jsonl"])
def test_prune_does_not_treat_escaping_identity_as_missing(tmp_path, relative):
    projects = tmp_path / "projects"
    projects.mkdir()
    data = tmp_path / "data"
    store.ingest(projects, data)
    outside = tmp_path / "outside"
    outside.mkdir()
    (projects / "escape").symlink_to(outside, target_is_directory=True)
    source = tmp_path / "input.jsonl"
    _write(source, "2025-01-01T00:00:00Z")
    store.build_source(source, relative, data, source.stat())
    assert store.prune(data, before="2026-01-01", projects=projects) == 0
    assert len(list(data.rglob("fragments.parquet"))) >= 1


@pytest.mark.parametrize("target", ["missing", "../elsewhere", "regular-file", "loop"])
def test_invalid_selector_never_resurrects_flat_rows(tmp_path, target):
    projects, data, path, first = _seed(tmp_path)
    _flatten(data, first)
    _write(path, "2026-03-01T00:00:00Z")
    assert store.ingest(projects, data)["rebuilt"] == 1
    selector = data / "sources" / first.key / "current"
    selector.unlink()
    if target == "regular-file":
        selector.write_text("not a selector")
    else:
        selector.symlink_to("current" if target == "loop" else target)
    accepted, excluded = store.select_sources(data)
    assert not accepted
    assert excluded == [{
        "key": first.key, "source": "", "reason": "invalid source selector",
    }]
    assert store.source_metadata(data, first.key)[1] == "invalid source selector"
    path.unlink()
    assert store.prune(data, before="2027-01-01", projects=projects) == 0


def test_absent_selector_still_accepts_flat_record(tmp_path):
    _projects, data, _path, record = _seed(tmp_path)
    _flatten(data, record)
    accepted, excluded = store.select_sources(data)
    assert not excluded and len(accepted) == 1
    assert accepted[0].directory is None


def test_flat_v1_row_timestamp_cannot_authorize_prune(tmp_path):
    projects = tmp_path / "projects"
    projects.mkdir()
    data = tmp_path / "data"
    shutil.copytree(FIXTURES / "flat-v1", data)
    store.ingest(projects, data, adopt_projects_root=True)
    record = store.select_sources(data)[0][0]
    assert store.prune(data, before="2027-01-01", projects=projects) == 0
    assert record.path("fragments").exists()
    assert store.select_sources(data)[0][0].directory is None


@pytest.mark.parametrize("fault", ["semantics", "missing", "inconsistent"])
@pytest.mark.parametrize("location", ["current", "retained"])
def test_prune_retains_every_excluded_record(tmp_path, fault, location):
    projects, data, path, original = _seed(tmp_path)
    if location == "retained":
        _write(path, "2026-01-01T00:00:00Z")
        assert store.ingest(projects, data)["rebuilt"] == 1
    table = original.path(TABLES[1])
    if fault == "missing":
        table.unlink()
    else:
        for name in TABLES if fault == "semantics" else [TABLES[1]]:
            body = pq.ParquetFile(original.path(name)).read()
            metadata = dict(body.schema.metadata)
            metadata[b"ta.extract" if fault == "semantics" else b"ta.fp"] = b"unknown"
            pq.write_table(body.replace_schema_metadata(metadata), original.path(name))
    before = {p.name: p.read_bytes() for p in original.directory.iterdir() if p.is_file()}
    path.unlink()
    assert store.prune(data, before="2027-01-01", projects=projects) == 0
    assert original.directory.is_dir()
    assert before == {p.name: p.read_bytes() for p in original.directory.iterdir() if p.is_file()}


@pytest.mark.parametrize("level", ["key", "sources"])
def test_symlinked_source_directories_never_delete_their_targets(tmp_path, level):
    projects, data, path, record = _seed(tmp_path)
    path.unlink()
    linked = record.directory.parent if level == "key" else data / "sources"
    outside = tmp_path / "outside"
    shutil.move(linked, outside)
    linked.symlink_to(outside, target_is_directory=True)
    before = {p.relative_to(outside): p.read_bytes() for p in outside.rglob("*") if p.is_file()}
    assert store.prune(data, before="2027-01-01", projects=projects) == 0
    with pytest.raises(ValueError, match="symlinked"):
        store._remove_record(data, record.key)
    assert before == {
        p.relative_to(outside): p.read_bytes() for p in outside.rglob("*") if p.is_file()
    }


@pytest.mark.parametrize("name", ["f" * 32, "not-a-record"])
def test_selected_directory_is_bound_to_its_original_record_id(tmp_path, name):
    _projects, data, _path, record = _seed(tmp_path)
    renamed = record.directory.with_name(name)
    record.directory.rename(renamed)
    current = renamed.parent / "current"
    current.unlink()
    current.symlink_to(name)
    accepted, excluded = store.select_sources(data)
    assert not accepted
    assert excluded[0]["reason"] == "record directory identity conflict"


@pytest.mark.parametrize("existing", [False, True])
def test_failed_publication_fence_restores_selection_and_is_retryable(tmp_path, monkeypatch, existing):
    if existing:
        projects, data, path, record = _seed(tmp_path)
        key = record.key
        previous = record.directory.name
    else:
        projects, data = tmp_path / "projects", tmp_path / "data"
        projects.mkdir()
        path = projects / "source.jsonl"
        key, previous = store.srckey("source.jsonl"), None
    _write(path, "2026-03-01T00:00:00Z")
    base = data / "sources" / key
    current = base / "current"
    original = store._fsync_directory
    failed = []

    def fence(directory):
        if (
            directory == base and current.is_symlink()
            and current.readlink().name != previous and not failed
        ):
            failed.append(True)
            raise OSError("injected post-selector durability failure")
        return original(directory)

    monkeypatch.setattr(store, "_fsync_directory", fence)
    assert store.ingest(projects, data)["failed"] == 1
    assert failed
    if existing:
        assert current.readlink().name == previous
    else:
        assert not current.is_symlink()
    assert store.ingest(projects, data)["rebuilt"] == 1
    assert current.readlink().name != previous


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("fault", ["file", "unreadable_tree"])
def test_failed_discovery_never_commits_projects_binding(tmp_path, monkeypatch, existing, fault):
    if existing:
        _projects, data, _path, _record = _seed(tmp_path)
    else:
        data = tmp_path / "data"
    binding = data / "projects-root.json"
    before = binding.read_bytes() if binding.exists() else None
    target = tmp_path / "new-projects"
    if fault == "file":
        target.write_text("not a directory")
    else:
        target.mkdir()

        def fail(_root):
            raise OSError("unreadable subtree")

        monkeypatch.setattr(store, "discover", fail)
    with pytest.raises((OSError, ValueError)):
        store.ingest(target, data, adopt_projects_root=True)
    assert (binding.read_bytes() if binding.exists() else None) == before

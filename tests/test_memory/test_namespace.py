"""Real SQLite/vector storage checks for external namespace ownership."""

from __future__ import annotations

import asyncio
import importlib
import threading
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams

from genesis.db.connection import get_db
from genesis.db.crud import memory as memory_crud
from genesis.db.schema import create_all_tables
from genesis.memory.namespace import DedupNamespace, _connection, reserve
from genesis.memory.store import MemoryStore


@pytest.fixture
async def harness(tmp_path, monkeypatch):
    path = tmp_path / "memory.db"
    db = await get_db(path)
    await create_all_tables(db)
    await db.commit()
    vectors = QdrantClient(":memory:")
    for name in ("episodic_memory", "knowledge_base"):
        vectors.create_collection(name, vectors_config=VectorParams(size=1024, distance=Distance.COSINE))
    provider = MagicMock()
    provider.embed = AsyncMock(return_value=[0.1] * 1024)
    provider.tracker = None
    linker = MagicMock()
    linker.auto_link = AsyncMock()
    store = MemoryStore(embedding_provider=provider, qdrant_client=vectors, db=db, linker=linker)
    monkeypatch.setattr("genesis.memory.entity_resolution.normalize_content", lambda text: text)
    try:
        yield path, db, vectors, store, provider, linker
    finally:
        vectors.close()
        await db.close()


async def write(store, content="Shared exact content", peer="muse", **kwargs):
    return await store.store_reporting_creation(
        content, "peer offer", collection="knowledge_base",
        dedup_namespace=DedupNamespace(peer, "knowledge_base"), **kwargs,
    )


async def scalar(db, sql, params=()):
    return (await (await db.execute(sql, params)).fetchone())[0]


async def test_owner_and_each_peer_remain_distinct_in_both_orders(harness):
    _, db, vectors, store, _, linker = harness
    text = "Shared exact content"
    muse, created = await write(store, text)
    other, other_created = await write(store, text, peer="other")
    owner, owner_created = await store.store_reporting_creation(text, "owner", collection="knowledge_base", origin_class="owner", auto_link=False)
    assert created and other_created and owner_created
    assert len({muse, other, owner}) == 3
    assert uuid.UUID(muse).version == 8 and uuid.UUID(owner).version == 4
    assert await write(store, text) == (muse, False)
    assert await store.store_reporting_creation(text, "owner again") == (owner, False)
    assert await scalar(db, "SELECT COUNT(*) FROM memory_fts WHERE content=?", (text,)) == 3
    points = vectors.retrieve("knowledge_base", [muse, other, owner])
    assert {p.id: p.payload["origin_class"] for p in points} == {muse: "external_untrusted", other: "external_untrusted", owner: "owner"}
    linker.auto_link.assert_not_awaited()
    owner_first, _ = await store.store_reporting_creation("Owner first", "owner", origin_class="owner", auto_link=False)
    peer_second, _ = await write(store, "Owner first")
    assert owner_first != peer_second


async def test_exact_bytes_preserved_and_collection_part_of_identity(harness, monkeypatch):
    _, db, _, store, _, _ = harness
    monkeypatch.setattr("genesis.memory.entity_resolution.normalize_content", lambda text: "normalized")
    text = "alias\r\nUnicode café  "
    first, _ = await write(store, text)
    second, _ = await write(store, text.rstrip())
    episodic, _ = await store.store_reporting_creation(text, "peer", dedup_namespace=DedupNamespace("muse", "episodic_memory"))
    assert len({first, second, episodic}) == 3
    assert await scalar(db, "SELECT content FROM memory_fts WHERE memory_id=?", (first,)) == text


@pytest.mark.parametrize("kwargs", [{"origin_class": "owner"}, {"source_subsystem": "ego"}, {"supersedes": "anything"}])
async def test_incompatible_provenance_rejected_before_effect(harness, kwargs):
    _, db, vectors, store, provider, _ = harness
    with pytest.raises(ValueError, match="incompatible"):
        await write(store, **kwargs)
    provider.embed.assert_not_awaited()
    assert await scalar(db, "SELECT COUNT(*) FROM memory_namespaces") == 0
    assert vectors.count("knowledge_base").count == 0


async def test_open_writer_transaction_rejected_without_deadlock(harness):
    _, db, _, store, _, _ = harness
    await db.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(ValueError, match="open writer"):
            await asyncio.wait_for(write(store), timeout=1)
    finally:
        await db.rollback()


async def test_namespace_lookup_failure_is_closed_ordinary_dedup_still_works(harness):
    _, db, vectors, store, provider, _ = harness
    peer, _ = await write(store)
    owner, _ = await store.store_reporting_creation("Shared exact content", "owner", auto_link=False)
    await db.execute("DROP TABLE memory_namespaces")
    await db.commit()
    provider.embed.reset_mock()
    with pytest.raises(Exception, match="no such table"):
        await write(store, "new content")
    provider.embed.assert_not_awaited()
    assert vectors.count("knowledge_base").count == 1
    assert await memory_crud.find_exact_duplicate(db, content="Shared exact content") == owner
    assert owner != peer


@pytest.mark.parametrize("seam", ["upsert", "create_metadata", "complete"])
async def test_retry_repairs_only_reserved_id_and_retains_compensation_ownership(harness, monkeypatch, seam):
    _, db, vectors, store, _, _ = harness
    owner, _ = await store.store_reporting_creation("Shared exact content", "owner", origin_class="owner", auto_link=False)
    other, _ = await write(store, peer="other")
    if seam == "complete":
        import genesis.memory.namespace as module
    else:
        module = memory_crud
    real = getattr(module, seam)
    monkeypatch.setattr(module, seam, AsyncMock(side_effect=RuntimeError("injected storage failure")))
    with pytest.raises(RuntimeError, match="injected"):
        await write(store)
    reserved = DedupNamespace("muse", "knowledge_base").memory_id("Shared exact content")
    assert await scalar(db, "SELECT status FROM memory_namespaces WHERE memory_id=?", (reserved,)) == "pending"
    monkeypatch.setattr(module, seam, real)
    assert await write(store) == (reserved, False)
    assert await scalar(db, "SELECT COUNT(*) FROM memory_fts WHERE memory_id=?", (reserved,)) == 1
    assert await scalar(db, "SELECT status FROM memory_namespaces WHERE memory_id=?", (reserved,)) == "complete"
    assert vectors.retrieve("episodic_memory", [owner])[0].payload["origin_class"] == "owner"
    assert vectors.retrieve("knowledge_base", [other])[0].payload["origin_class"] == "external_untrusted"


async def test_concurrent_reservations_single_creator_and_cancel_rollback(harness):
    path, db, _, _, _, _ = harness
    ns = DedupNamespace("muse", "knowledge_base")
    rows = await asyncio.gather(*(reserve(path, ns, "race") for _ in range(8)))
    assert len({row[0] for row in rows}) == 1
    assert sum(row[1] for row in rows) == 1
    with pytest.raises(asyncio.CancelledError):
        async with _connection(path) as private:
            await private.execute("DELETE FROM memory_namespaces")
            raise asyncio.CancelledError
    assert await scalar(db, "SELECT COUNT(*) FROM memory_namespaces") == 1


async def test_conflicting_reserved_storage_refused_without_relabel(harness):
    path, db, _, store, _, _ = harness
    ns = DedupNamespace("muse", "knowledge_base")
    mid, _, _ = await reserve(path, ns, "collision")
    await memory_crud.upsert(db, memory_id=mid, content="different content")
    with pytest.raises(ValueError, match="storage conflict"):
        await write(store, "collision")
    assert await scalar(db, "SELECT content FROM memory_fts WHERE memory_id=?", (mid,)) == "different content"


async def test_pending_embedding_retry_cleans_only_own_retry_rows(harness, monkeypatch):
    import genesis.memory.namespace as module
    from genesis.memory.embeddings import EmbeddingUnavailableError

    _, db, _, store, provider, _ = harness
    provider.embed.side_effect = EmbeddingUnavailableError("isolated provider unavailable")
    other, _ = await write(store, "other pending", peer="other")
    real_complete = module.complete
    monkeypatch.setattr(module, "complete", AsyncMock(side_effect=RuntimeError("after pending write")))
    with pytest.raises(RuntimeError, match="after pending"):
        await write(store)
    own = DedupNamespace("muse", "knowledge_base").memory_id("Shared exact content")
    assert await scalar(db, "SELECT COUNT(*) FROM pending_embeddings WHERE memory_id=?", (own,)) == 1
    monkeypatch.setattr(module, "complete", real_complete)
    provider.embed.side_effect = None
    assert await write(store) == (own, False)
    assert await scalar(db, "SELECT embedding_status FROM memory_metadata WHERE memory_id=?", (own,)) == "embedded"
    assert await scalar(db, "SELECT COUNT(*) FROM pending_embeddings WHERE memory_id=?", (own,)) == 0
    assert await scalar(db, "SELECT COUNT(*) FROM pending_embeddings WHERE memory_id=?", (other,)) == 1


async def test_embedding_recovery_keeps_namespace_unlinked_but_links_legacy(harness):
    from genesis.memory.embeddings import EmbeddingUnavailableError
    from genesis.resilience.embedding_recovery import EmbeddingRecoveryWorker

    _, db, vectors, store, provider, linker = harness
    provider.embed.side_effect = EmbeddingUnavailableError("isolated provider unavailable")
    peer, _ = await write(store)
    owner, _ = await store.store_reporting_creation("Owner recovery", "owner")
    provider.embed.side_effect = None
    worker = EmbeddingRecoveryWorker(db=db, embedding_provider=provider, qdrant_client=vectors, linker=linker, pace_per_min=0)
    assert await worker.drain_pending() == 2
    linker.auto_link.assert_awaited_once()
    assert linker.auto_link.await_args.args[0] == owner
    assert vectors.retrieve("knowledge_base", [peer])[0].payload["origin_class"] == "external_untrusted"


async def test_upgrade_schema_matches_fresh_and_runner_owns_transaction(harness):
    _, db, _, _, _, _ = harness
    fresh = await (await db.execute("PRAGMA table_info(memory_namespaces)")).fetchall()
    await db.execute("DROP TABLE memory_namespaces")
    await db.commit()
    migration = importlib.import_module("genesis.db.migrations.20261008044832_memory_namespaces")
    await db.execute("BEGIN IMMEDIATE")
    await migration.up(db)
    await migration.up(db)
    assert db.in_transaction
    upgraded = await (await db.execute("PRAGMA table_info(memory_namespaces)")).fetchall()
    assert [tuple(row) for row in fresh] == [tuple(row) for row in upgraded]
    await db.rollback()
    assert await scalar(db, "SELECT COUNT(*) FROM sqlite_master WHERE name='memory_namespaces'") == 0


def test_namespace_validation_and_uuid_determinism():
    ns = DedupNamespace("muse", "knowledge_base")
    assert ns.memory_id("exact") == DedupNamespace("muse", "knowledge_base").memory_id("exact")
    assert uuid.UUID(ns.memory_id("exact")).version == 8
    for peer, collection, origin in [("", "knowledge_base", "external_untrusted"), ("UPPER", "knowledge_base", "external_untrusted"), ("muse", "../bad", "external_untrusted"), ("muse", "knowledge_base", "owner")]:
        with pytest.raises(ValueError):
            DedupNamespace(peer, collection, origin)


async def test_eight_full_concurrent_writes_have_one_fts_row(harness):
    _, db, vectors, store, _, _ = harness
    rows = await asyncio.gather(*(write(store) for _ in range(8)))
    assert len({row[0] for row in rows}) == 1
    assert sum(row[1] for row in rows) == 1
    assert await scalar(db, "SELECT COUNT(*) FROM memory_fts") == 1
    assert vectors.count("knowledge_base").count == 1


async def test_namespaced_delete_refuses_open_writer_transaction(harness):
    _, db, _, store, _, _ = harness
    mid, _ = await write(store)
    await db.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(ValueError, match="open writer"):
            await asyncio.wait_for(store.delete(mid), 1)
    finally:
        await db.rollback()
    assert await scalar(db, "SELECT status FROM memory_namespaces WHERE memory_id=?", (mid,)) == "complete"


async def test_delete_waits_for_paused_retry_then_blocks_all_reoffers(harness, monkeypatch):
    import genesis.memory.namespace as module

    _, db, vectors, store, provider, _ = harness
    real_complete = module.complete
    monkeypatch.setattr(module, "complete", AsyncMock(side_effect=RuntimeError("partial completion")))
    with pytest.raises(RuntimeError):
        await write(store)
    monkeypatch.setattr(module, "complete", real_complete)
    started, release = asyncio.Event(), asyncio.Event()
    async def paused_embed(*args):
        started.set()
        await release.wait()
        return [0.1] * 1024
    provider.embed.side_effect = paused_embed
    retry = asyncio.create_task(write(store))
    await asyncio.wait_for(started.wait(), 5)
    mid = DedupNamespace("muse", "knowledge_base").memory_id("Shared exact content")
    deletion = asyncio.create_task(store.delete(mid))
    await asyncio.sleep(0.05)
    assert not deletion.done()
    release.set()
    assert await retry == (mid, False)
    assert not (await deletion).get("deferred")
    assert await scalar(db, "SELECT status FROM memory_namespaces WHERE memory_id=?", (mid,)) == "deleted"
    assert await scalar(db, "SELECT COUNT(*) FROM memory_fts WHERE memory_id=?", (mid,)) == 0
    with pytest.raises(ValueError, match="owner restoration"):
        await write(store)
    assert vectors.count("knowledge_base").count == 0


async def test_canceled_vector_thread_drains_before_delete_gets_lock(harness, monkeypatch):
    import genesis.memory.store as module

    _, db, vectors, store, _, _ = harness
    started, release = threading.Event(), threading.Event()
    real = module.upsert_point
    def paused_upsert(*args, **kwargs):
        started.set()
        assert release.wait(5)
        return real(*args, **kwargs)
    monkeypatch.setattr(module, "upsert_point", paused_upsert)
    writer = asyncio.create_task(write(store))
    assert await asyncio.to_thread(started.wait, 5)
    writer.cancel()
    mid = DedupNamespace("muse", "knowledge_base").memory_id("Shared exact content")
    deletion = asyncio.create_task(store.delete(mid))
    await asyncio.sleep(0.05)
    assert not writer.done() and not deletion.done()
    writer.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await writer
    assert not (await deletion).get("deferred")
    assert vectors.count("knowledge_base").count == 0
    assert await scalar(db, "SELECT status FROM memory_namespaces WHERE memory_id=?", (mid,)) == "deleted"


# ── Review round: deletion permanence, repair idempotence, link exclusion ──


async def _pending_peer(store, provider, content="Shared exact content"):
    """A namespaced memory whose embedding never landed: pending, no point."""
    from genesis.memory.embeddings import EmbeddingUnavailableError

    provider.embed.side_effect = EmbeddingUnavailableError("isolated provider unavailable")
    mid, _ = await write(store, content)
    provider.embed.side_effect = None
    return mid


async def test_degraded_delete_tombstones_namespace_and_drops_pending_job(harness):
    from genesis.memory.store import delete_without_vector_store

    _, db, vectors, store, provider, _ = harness
    mid = await _pending_peer(store, provider)
    assert await scalar(db, "SELECT COUNT(*) FROM pending_embeddings WHERE memory_id=?", (mid,)) == 1
    outcome = await delete_without_vector_store(db, mid)
    assert outcome["status"] == "partial" and outcome["fts5"]
    assert await scalar(db, "SELECT status FROM memory_namespaces WHERE memory_id=?", (mid,)) == "deleted"
    assert await scalar(db, "SELECT COUNT(*) FROM pending_embeddings WHERE memory_id=?", (mid,)) == 0
    with pytest.raises(ValueError, match="owner restoration"):
        await write(store)
    assert await scalar(db, "SELECT COUNT(*) FROM memory_fts WHERE memory_id=?", (mid,)) == 0
    assert vectors.count("knowledge_base").count == 0


async def test_degraded_delete_still_refuses_embedded_rows(harness):
    from genesis.memory.store import delete_without_vector_store

    _, db, _, store, _, _ = harness
    mid, _ = await write(store)
    assert (await delete_without_vector_store(db, mid))["status"] == "unavailable"
    assert await scalar(db, "SELECT status FROM memory_namespaces WHERE memory_id=?", (mid,)) == "complete"
    assert await scalar(db, "SELECT COUNT(*) FROM memory_fts WHERE memory_id=?", (mid,)) == 1


def test_degraded_dashboard_delete_routes_through_the_tombstoning_helper():
    from unittest.mock import patch

    from flask import Flask

    from genesis.dashboard.api import blueprint

    app = Flask(__name__)
    app.register_blueprint(blueprint)
    rt = MagicMock(is_bootstrapped=True, memory_store=None)
    helper = AsyncMock(return_value={"status": "deferred", "deferred": True})
    with (
        patch("genesis.runtime.GenesisRuntime") as runtime,
        patch("genesis.memory.store.delete_without_vector_store", helper),
    ):
        runtime.instance.return_value = rt
        resp = app.test_client().delete("/api/genesis/memory/abc")
    helper.assert_awaited_once_with(rt.db, "abc")
    assert resp.status_code == 503 and resp.get_json()["status"] == "deferred"


async def test_delete_waits_past_busy_timeout_for_cross_process_writer(harness, monkeypatch):
    import genesis.memory.namespace as module

    path, db, _, store, _, _ = harness
    mid, _ = await write(store)
    monkeypatch.setattr(module, "db_busy_timeout_ms", lambda: 50)
    holding, release = asyncio.Event(), asyncio.Event()

    async def other_writer():
        # A separate open file description: flock conflicts exactly as a
        # second process holding this ID across a slow embedding would.
        async with module.write_lock(path, mid):
            holding.set()
            await release.wait()

    holder = asyncio.create_task(other_writer())
    await asyncio.wait_for(holding.wait(), 5)
    deletion = asyncio.create_task(store.delete(mid))
    await asyncio.sleep(0.3)  # six busy timeouts
    assert not deletion.done()
    release.set()
    await holder
    assert not (await asyncio.wait_for(deletion, 5)).get("deferred")
    assert await scalar(db, "SELECT status FROM memory_namespaces WHERE memory_id=?", (mid,)) == "deleted"
    assert await scalar(db, "SELECT COUNT(*) FROM memory_fts WHERE memory_id=?", (mid,)) == 0


async def test_delete_that_cannot_get_the_lock_defers_with_durable_tombstones(harness, monkeypatch):
    import genesis.memory.namespace as module
    from genesis.memory.delete_tombstones import has_open_tombstone

    path, db, _, store, _, _ = harness
    mid, _ = await write(store)
    monkeypatch.setattr(module, "DELETE_LOCK_WAIT_S", 0.05)
    async with module.write_lock(path, mid):
        outcome = await store.delete(mid)
    assert outcome["deferred"] and outcome["tombstoned"]
    assert await scalar(db, "SELECT status FROM memory_namespaces WHERE memory_id=?", (mid,)) == "deleted"
    assert await has_open_tombstone(db, memory_id=mid)
    with pytest.raises(ValueError, match="owner restoration"):
        await write(store)
    # The reconcile lane's drain re-runs the delete and completes it.
    assert not (await store.delete(mid)).get("deferred")
    assert await scalar(db, "SELECT COUNT(*) FROM memory_fts WHERE memory_id=?", (mid,)) == 0
    assert not await has_open_tombstone(db, memory_id=mid)


async def test_repair_during_outage_keeps_one_pending_job(harness, monkeypatch):
    import genesis.memory.namespace as module
    from genesis.memory.embeddings import EmbeddingUnavailableError

    _, db, _, store, provider, _ = harness
    provider.embed.side_effect = EmbeddingUnavailableError("isolated provider unavailable")
    real_complete = module.complete
    monkeypatch.setattr(module, "complete", AsyncMock(side_effect=RuntimeError("after pending write")))
    with pytest.raises(RuntimeError):
        await write(store, tags=["first"])
    monkeypatch.setattr(module, "complete", real_complete)
    mid = DedupNamespace("muse", "knowledge_base").memory_id("Shared exact content")
    assert await write(store, tags=["second"]) == (mid, False)
    rows = await (await db.execute("SELECT tags FROM pending_embeddings WHERE memory_id=?", (mid,))).fetchall()
    assert len(rows) == 1 and "second" in rows[0][0].split(",")


async def test_repair_applies_one_snapshot_across_metadata_fts_and_vector(harness, monkeypatch):
    import genesis.memory.namespace as module

    _, db, vectors, store, _, _ = harness
    real_complete = module.complete
    monkeypatch.setattr(module, "complete", AsyncMock(side_effect=RuntimeError("partial completion")))
    with pytest.raises(RuntimeError):
        await write(store, confidence=0.9, wing="memory")
    monkeypatch.setattr(module, "complete", real_complete)
    mid, _ = await write(store, confidence=0.7, wing="routing")
    meta = await (await db.execute("SELECT confidence, wing FROM memory_metadata WHERE memory_id=?", (mid,))).fetchone()
    assert tuple(meta) == (0.7, "routing")
    tags = await scalar(db, "SELECT tags FROM memory_fts WHERE memory_id=?", (mid,))
    assert "wing:routing" in tags and "wing:memory" not in tags
    payload = vectors.retrieve("knowledge_base", [mid])[0].payload
    assert (payload["confidence"], payload["wing"]) == (0.7, "routing")


async def _owner_and_peer_episodic(store):
    owner, _ = await store.store_reporting_creation(
        "Owner episodic note", "owner", origin_class="owner", auto_link=False, wing="memory", room="a",
    )
    peer, _ = await store.store_reporting_creation(
        "Peer episodic note", "peer", wing="routing", room="b",
        dedup_namespace=DedupNamespace("muse", "episodic_memory"),
    )
    return owner, peer


async def test_link_sink_refuses_namespaced_endpoints_unless_owner_explicit(harness):
    from genesis.db.crud import memory_links

    _, db, _, store, _, _ = harness
    owner, peer = await _owner_and_peer_episodic(store)
    for source, target in ((owner, peer), (peer, owner)):
        with pytest.raises(ValueError, match="namespaced"):
            await memory_links.create(db, source_id=source, target_id=target, link_type="related_to", created_at="2026-01-01T00:00:00+00:00")
    assert await scalar(db, "SELECT COUNT(*) FROM memory_links") == 0
    await memory_links.create(db, source_id=owner, target_id=peer, link_type="succeeded_by", created_at="2026-01-01T00:00:00+00:00", allow_namespaced=True)
    assert await scalar(db, "SELECT COUNT(*) FROM memory_links") == 1


async def test_owner_auto_link_skips_peer_neighbours(harness):
    from genesis.memory.linker import MemoryLinker

    _, db, vectors, store, _, _ = harness
    owner, peer = await _owner_and_peer_episodic(store)
    linker = MemoryLinker(qdrant_client=vectors, db=db, similarity_threshold=0.5)
    assert await linker.auto_link(owner, [0.1] * 1024) == []
    assert await scalar(db, "SELECT COUNT(*) FROM memory_links") == 0


async def test_dream_cycle_never_groups_merges_or_links_peer_points(harness):
    from genesis.memory.dream_cycle import _cross_wing_scan, _scroll_and_group_sync

    _, db, vectors, store, _, _ = harness
    owner, peer = await _owner_and_peer_episodic(store)
    other, _ = await store.store_reporting_creation(
        "Second owner note", "owner", origin_class="owner", auto_link=False, wing="routing", room="b",
    )
    buckets = _scroll_and_group_sync(vectors)
    grouped = {p["id"] for points in buckets.values() for p in points}
    assert peer not in grouped and {owner, other} <= grouped
    findings = await _cross_wing_scan(qdrant=vectors, db=db, buckets=buckets, run_id="t", similarity_threshold=0.5)
    assert all(peer[:8] not in (f["memory_a"], f["memory_b"]) for f in findings)
    assert await scalar(db, "SELECT COUNT(*) FROM memory_links WHERE source_id=? OR target_id=?", (peer, peer)) == 0


async def test_entity_dedup_candidates_exclude_peer_hits(harness):
    from genesis.memory.entity_resolution import _find_dedup_candidates_sync

    _, _, vectors, store, _, _ = harness
    owner, peer = await _owner_and_peer_episodic(store)
    pairs = _find_dedup_candidates_sync(
        vectors, [{"id": owner, "payload": {}}], {owner: [0.1] * 1024},
        threshold=0.5, max_candidates_per_point=5, collection="episodic_memory",
    )
    assert all(peer not in (a["id"], b["id"]) for a, b, _ in pairs)


async def test_orphan_detection_does_not_count_peer_memories(harness):
    from genesis.memory.dream_orphan_detection import run_orphan_detection

    _, db, vectors, store, _, _ = harness
    owner, peer = await _owner_and_peer_episodic(store)
    report = await run_orphan_detection(qdrant=vectors, db=db, router=MagicMock(), store=MagicMock(), run_id="t", dry_run=True)
    assert report["orphans_found"] == 1 and report["sample_orphans"] == [owner]

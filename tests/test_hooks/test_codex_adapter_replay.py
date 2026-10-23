"""Replay receipts must account for the entire selected test lifecycle."""

import json
from types import SimpleNamespace

import pytest

from tests.test_hooks import codex_adapter_replay as bridge


@pytest.mark.parametrize(
    "phases,expected",
    [
        ([("setup", "passed"), ("call", "passed"), ("teardown", "passed")], "passed"),
        ([("setup", "skipped"), ("teardown", "passed")], "skipped"),
        ([("setup", "failed"), ("teardown", "passed")], "failed"),
        ([("setup", "passed"), ("call", "failed"), ("teardown", "passed")], "failed"),
        ([("setup", "passed"), ("call", "passed"), ("teardown", "failed")], "failed"),
        ([("setup", "passed"), ("call", "skipped"), ("teardown", "passed")], "skipped"),
        ([], "not-run"),
    ],
)
def test_receipt_keeps_every_selected_node_and_qualifies_only_complete_passes(
    monkeypatch, tmp_path, phases, expected
):
    nodeid = "tests/fixture.py::test_case"
    monkeypatch.setattr(bridge, "REPORTS", {})
    monkeypatch.setattr(bridge, "SOURCE_ITEMS", {})
    monkeypatch.setattr(bridge, "COPIED", {})
    monkeypatch.setattr(
        bridge,
        "ROWS",
        [
            {
                "nodeid": nodeid,
                "kind": "replay",
                "adapter_assertions_passed": True,
            }
        ],
    )
    path = tmp_path / "receipt.json"
    item = SimpleNamespace(
        nodeid=nodeid, location=("tests/fixture.py", 7, "test_case"), function=lambda: None
    )
    session = SimpleNamespace(items=[item], config=SimpleNamespace(getoption=lambda _: str(path)))
    # Older instrument versions had no collection seed: keep the regression
    # observable as a missing receipt node, not an AttributeError in the test.
    if hasattr(bridge, "pytest_collection_finish"):
        bridge.pytest_collection_finish(session)
    for when, outcome in phases:
        report = SimpleNamespace(
            when=when,
            outcome=outcome,
            skipped=outcome == "skipped",
            longrepr=("tests/fixture.py", 8, "Skipped: fixture") if outcome == "skipped" else None,
        )
        hook = bridge.pytest_runtest_makereport(item, None)
        next(hook)
        with pytest.raises(StopIteration):
            hook.send(SimpleNamespace(get_result=lambda report=report: report))
    bridge.pytest_sessionfinish(session, 0)
    receipt = json.loads(path.read_text())
    assert set(receipt["coverage"]) == {nodeid}
    row = receipt["coverage"][nodeid]
    assert row["direct_test_result"] == expected
    assert row["qualified_replays"] == (1 if expected == "passed" else 0)


def test_receipt_invalidates_instrument_changed_after_load(monkeypatch, tmp_path):
    monkeypatch.setattr(bridge, "REPORTS", {})
    monkeypatch.setattr(bridge, "SOURCE_ITEMS", {})
    monkeypatch.setattr(bridge, "COPIED", {})
    monkeypatch.setattr(bridge, "ROWS", [])
    monkeypatch.setattr(bridge, "sha", lambda _: "changed-after-import")
    path = tmp_path / "receipt.json"
    session = SimpleNamespace(
        items=[], config=SimpleNamespace(getoption=lambda _: str(path)), exitstatus=0
    )
    bridge.pytest_sessionfinish(session, 0)
    assert session.exitstatus != 0, "A changed instrument was still accepted as the executed source"
    assert json.loads(path.read_text()).get("instrument_unchanged", True) is False

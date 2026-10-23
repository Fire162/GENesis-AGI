"""Opt-in existing-case replay: load explicitly with pytest -p, retain a receipt.

Ordinary cases use one real child for both the original assertions and adapter
decision checks. Three named, stateless premise mappings retain a direct guard
control because the mandatory known-cwd anchor changes their old preconditions.
Commands are guard input only; the instrument never executes the action.
"""

import asyncio
import hashlib
import inspect
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.conftest import private_module
from tests.test_hooks.test_codex_validator_adapter_native import source_copy

ROOT = Path(__file__).resolve().parents[2]
NAMES = {
    "destructive_command_guard",
    "repo_routing_guard",
    "full_suite_guard",
    "tmux_kill_server_guard",
    "pipe_status_guard",
    "credential_surface_hook",
    "git_discard_guard",
    "git_push_guard",
    "secrets_env_access_guard",
    "protected_paths_guard",
    "worktree_cwd_guard",
    "capped_read_advisory",
    "rm_trash_advisory",
    "pr_close_advisory",
    "main_checkout_guard",
}
ROWS = []
REPORTS = {}
COPIED = {}
SOURCE_ITEMS = {}
INSTRUMENT_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
MAPPINGS = {
    "test_relative_without_cwd_falls_back_to_substring": "known-cwd",
    "test_prose_naming_a_supplied_removal_gets_an_honest_refusal": "compound-heredoc",
    "test_a_SINGLE_step_carrier_refusal_does_not_print_it": "compound-note",
}
COPIED_FIXTURES = {
    "test_a_missing_GUARDED_sibling_cannot_disarm_the_clean_block",
    "test_the_positive_control_an_intact_tree_allows_a_dry_run_clean",
}


def pytest_addoption(parser):
    parser.addoption("--codex-replay-receipt", help="Absolute path to the private replay receipt")


def pytest_configure(config):
    path = config.getoption("--codex-replay-receipt")
    if not path or not Path(path).is_absolute():
        raise pytest.UsageError("Explicit replay requires an absolute --codex-replay-receipt")


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def copy_fixture_runtime(source, runtime, target):
    """Retain the fixture's entire hook tree, including deliberately absent files."""
    shutil.copytree(runtime, target, symlinks=True, ignore=shutil.ignore_patterns("__pycache__"))
    hooks = target / "scripts/hooks"
    expected = {
        str(p.relative_to(source)): sha(p)
        for p in source.rglob("*")
        if p.is_file() and "__pycache__" not in p.parts
    }
    for file in hooks.rglob("*"):
        if file.is_file() and str(file.relative_to(hooks)) not in expected:
            file.unlink()
    for relative, digest in expected.items():
        file = hooks / relative
        file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / relative, file)
        assert sha(file) == digest
    assert {str(p.relative_to(hooks)) for p in hooks.rglob("*") if p.is_file()} == set(expected)
    return target, expected


@pytest.fixture(scope="session")
def existing_replay_runtime(tmp_path_factory):
    runtime = source_copy(tmp_path_factory.mktemp("existing-replay-source"))
    # The original credential fixtures use the real admission predicate and
    # scratch SQLite. Preserve those imports; never replace the fence by a stub.
    for relative in (
        "src/genesis/env.py",
        "src/genesis/db/__init__.py",
        "src/genesis/db/admission.py",
        "src/genesis/db/integrity.py",
        "config/repo_topology.yaml",
    ):
        target = runtime / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, target)
    for part in ("scripts/hooks", ".claude/hooks", "src/genesis", "config"):
        for file in (runtime / part).rglob("*"):
            if file.is_file() and "__pycache__" not in file.parts:
                relative = str(file.relative_to(runtime))
                COPIED[relative] = sha(file)
                assert COPIED[relative] == sha(ROOT / relative)
    return runtime


@pytest.fixture(autouse=True)
def existing_adapter_replay(
    request, monkeypatch, tmp_path, tmp_path_factory, existing_replay_runtime
):
    runtime = existing_replay_runtime
    owned = tmp_path_factory.getbasetemp().resolve()
    monkeypatch.syspath_prepend(str(ROOT / "scripts/hooks"))
    adapter = private_module(
        "existing_replay_adapter", ROOT / "scripts/hooks/codex_validator_adapter.py"
    )
    adapter.RUNTIME_ROOT = runtime
    original_run = subprocess.run
    reducer = adapter.child_result
    if request.node.originalname == "test_remove_own_cwd_blocked":
        # Preserve the property before the old test derives its own target.
        monkeypatch.chdir(tmp_path)

    def safe_path(value):
        return Path(value).expanduser().resolve().is_relative_to(owned)

    def run(args, *positional, **kwargs):
        argv = shlex.split(args) if isinstance(args, str) and kwargs.get("shell") else args
        if not isinstance(argv, (list, tuple)) or len(argv) < 2:
            return original_run(args, *positional, **kwargs)
        guard = Path(str(argv[1]))
        if guard.stem not in NAMES or guard.suffix != ".py":
            return original_run(args, *positional, **kwargs)
        row = {
            "nodeid": request.node.nodeid,
            "guard": guard.stem,
            "kind": "unmapped",
            "reason": "",
            "original_guard_sha256": sha(guard) if guard.is_file() else None,
        }
        ROWS.append(row)
        if len(argv) != 2:
            row["reason"] = "Claude EnterWorktree/ExitWorktree argv mode is not a Codex event"
            return original_run(args, *positional, **kwargs)
        if (
            guard.stem == "secrets_env_access_guard"
            and kwargs.get("env", {}).get("GENESIS_CC_SESSION") == "1"
        ):
            row["reason"] = (
                "Claude dispatched-session observation branch is not a Codex validator event"
            )
            return original_run(args, *positional, **kwargs)
        raw_input = kwargs.get("input")
        try:
            payload = json.loads(raw_input)
        except (TypeError, ValueError):
            row["reason"] = "malformed input is not a compatible hook payload"
            return original_run(args, *positional, **kwargs)
        if not isinstance(payload, dict) or not isinstance(payload.get("tool_input"), dict):
            row["reason"] = "non-object hook input"
            return original_run(args, *positional, **kwargs)
        if (
            payload.get("tool_name", "Bash") != "Bash"
            or payload.get("hook_event_name", "PreToolUse") != "PreToolUse"
        ):
            row["reason"] = "Claude file/read/PostToolUse shape is not a native Bash payload"
            return original_run(args, *positional, **kwargs)
        if guard.stem == "main_checkout_guard":
            row["reason"] = "main-checkout Bash half is explicitly excluded by PR1"
            return original_run(args, *positional, **kwargs)
        copied_fixture = guard.parent.resolve() != (ROOT / "scripts/hooks").resolve()
        if copied_fixture and not (
            guard.stem == "git_discard_guard" and request.node.originalname in COPIED_FIXTURES
        ):
            row["reason"] = "fixture-specific copied guard requires a named mapping"
            return original_run(args, *positional, **kwargs)
        command = payload["tool_input"].get("command")
        if not isinstance(command, str) or not command.strip():
            row["reason"] = "empty or non-string command"
            return original_run(args, *positional, **kwargs)
        cwd = Path(payload.get("cwd") or kwargs.get("cwd") or tmp_path)
        if cwd.is_absolute() and safe_path(cwd) and not cwd.exists():
            # These existing lexical-cwd fixtures never created their directories.
            # Codex anchors require them to exist; materialize only owned scratch.
            cwd.mkdir(parents=True)
            row["fixture_adaptation"] = "materialized owned lexical cwd"
        if not cwd.is_absolute() or not cwd.is_dir() or not safe_path(cwd):
            row["reason"] = "virtual or non-owned operation cwd needs fixture adaptation"
            return original_run(args, *positional, **kwargs)
        env = dict(kwargs.get("env") if kwargs.get("env") is not None else os.environ)
        if not safe_path(env.get("HOME", "/")):
            env["HOME"] = str(tmp_path)
        env.setdefault("GENESIS_HOME", str(Path(env["HOME"]) / ".genesis"))
        env.setdefault("GENESIS_DB_PATH", str(tmp_path / "absent-replay.db"))
        for key in ("GENESIS_HOME", "GENESIS_DB_PATH"):
            assert safe_path(env[key]), f"Replay refused non-scratch {key}"
        call_runtime = runtime
        if copied_fixture:
            assert safe_path(guard) and not guard.is_symlink()
            call_runtime, fixture_hashes = copy_fixture_runtime(
                guard.parent, runtime, tmp_path / "adapter-fixture-runtime"
            )
            row.update(
                fixture_source_hashes=fixture_hashes,
                missing_fixture_sources=sorted(
                    name.removeprefix("scripts/hooks/") for name in COPIED
                    if name.startswith("scripts/hooks/")
                    and name.removeprefix("scripts/hooks/") not in fixture_hashes
                ),
            )
        mapping = MAPPINGS.get(request.node.originalname)
        baseline = None
        if mapping:
            baseline = original_run(
                [str(call_runtime / ".claude/hooks/genesis-hook"), "hooks/" + guard.name],
                input=raw_input,
                capture_output=True,
                text=True,
                cwd=cwd,
                env=env,
                timeout=8,
            )
            row.update(premise_mapping=mapping, baseline_code=baseline.returncode)
        data = {
            **payload,
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "cwd": str(cwd),
            "tool_input": {
                **payload["tool_input"],
                "command": "cd " + shlex.quote(str(cwd)) + " && " + command,
            },
        }
        row.update(
            kind="replay",
            reason="",
            cwd=str(cwd),
            input_sha256=hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest(),
            copied_guard_sha256=sha(call_runtime / "scripts/hooks" / guard.name),
            copied_launcher_sha256=sha(call_runtime / ".claude/hooks/genesis-hook"),
        )
        assert row["copied_guard_sha256"] == row["original_guard_sha256"]
        captured = {}

        def observe(code, stdout, stderr):
            captured.update(code=code, stdout=stdout, stderr=stderr)
            return reducer(code, stdout, stderr)

        child = call_runtime / "scripts/hooks" / guard.name
        saved = child.read_bytes()
        fault = os.environ.get("ADAPTER_REPLAY_CHILD_FAULT")
        try:
            if fault:
                assert fault in ("empty", "deny")
                child.write_text(
                    "pass\n"
                    if fault == "empty"
                    else 'import sys; print("BLOCKED: injected child denial", file=sys.stderr); sys.exit(2)\n'
                )
                row.update(child_fault=fault, injected_guard_sha256=sha(child))
                assert row["injected_guard_sha256"] != row["copied_guard_sha256"]
            with monkeypatch.context() as scoped:
                scoped.setattr(adapter, "RUNTIME_ROOT", call_runtime)
                call = next(c for c in adapter.prepare(data) if "/" + guard.name in " ".join(c[0]))
                assert call[0][0] == str(call_runtime / ".claude/hooks/genesis-hook")
                assert call[1]["tool_input"]["command"] == data["tool_input"]["command"]
                # An exact replacement retains even intentionally minimal fixture
                # envs; run_guard owns its documented child modifications.
                scoped.setattr(os, "environ", env)
                scoped.setattr(adapter, "child_result", observe)
                try:
                    note = asyncio.run(adapter.run_guard(*call))
                except adapter.Refused as exc:
                    allowed, note = False, str(exc)
                else:
                    allowed = True
        finally:
            if fault:
                child.write_bytes(saved)
        assert captured, "Adapter did not execute the real guard/reducer boundary"
        out, err, code = captured["stdout"], captured["stderr"], captured["code"]
        specific = {}
        legacy_block = False
        if out.strip():
            decoded = json.loads(out)
            specific = decoded.get("hookSpecificOutput", {})
            legacy_block = decoded.get("decision") == "block"
        expected = (
            code == 0
            and not legacy_block
            and specific.get("permissionDecision") not in ("ask", "deny")
        )
        row.update(
            raw_code=code,
            raw_decision=specific.get("permissionDecision"),
            raw_context=bool(specific.get("additionalContext")),
            actual_allow=allowed,
            expected_allow=expected,
        )
        assert allowed == expected, "Adapter admission disagrees with actual child result"
        if allowed:
            assert specific.get("additionalContext", "") in note
            assert err.decode(errors="replace").strip() in note
        elif code == 2 and err.strip():
            assert err.decode(errors="replace").strip() in note
        elif specific.get("permissionDecision") in ("ask", "deny"):
            assert specific.get("permissionDecisionReason", "") in note
        if mapping == "known-cwd":
            assert baseline.returncode == 2 and allowed
        elif mapping == "compound-heredoc":
            assert baseline.returncode == 0 and not allowed
            assert "stdin or a list" in note
        elif mapping == "compound-note":
            marker = "the ENTIRE command was discarded"
            assert baseline.returncode == 2 and not allowed
            assert marker not in baseline.stderr and marker in note
        row["adapter_assertions_passed"] = True
        if baseline is not None:
            return baseline
        text_mode = kwargs.get("text") or kwargs.get("universal_newlines") or kwargs.get("encoding")
        result = subprocess.CompletedProcess(
            args, code, out.decode() if text_mode else out, err.decode() if text_mode else err
        )
        if kwargs.get("check"):
            result.check_returncode()
        return result

    monkeypatch.setattr(subprocess, "run", run)
    module = request.module
    if module.__name__ == "tests.test_hooks.test_full_suite_guard":
        classify = module._mod._targets_specific_test

        def argv_classifier(args):
            result = classify(args)
            if inspect.currentframe().f_back.f_code.co_filename == module.__file__:
                proc = run(
                    [sys.executable, str(ROOT / "scripts/hooks/full_suite_guard.py")],
                    input=json.dumps({"tool_input": {"command": shlex.join(["pytest", *args])}}),
                    capture_output=True,
                    text=True,
                )
                assert (proc.returncode == 0) == result
            return result

        monkeypatch.setattr(module._mod, "_targets_specific_test", argv_classifier)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    phase = {"outcome": report.outcome}
    if report.skipped:
        phase["skip_reason"] = (
            report.longrepr[2] if isinstance(report.longrepr, tuple) else "skipped"
        )
    if getattr(report, "wasxfail", None):
        phase["xfail"] = report.wasxfail
    REPORTS.setdefault(item.nodeid, {})[report.when] = phase


def pytest_collection_finish(session):
    for item in session.items:
        REPORTS.setdefault(item.nodeid, {})
        SOURCE_ITEMS[item.nodeid] = {
            "file": item.location[0],
            "line": item.location[1] + 1,
            "symbols": sorted(set(item.function.__code__.co_names)),
        }


def pytest_sessionfinish(session, exitstatus):
    coverage = {}
    reports = {}
    final_hash = sha(Path(__file__))
    unchanged = final_hash == INSTRUMENT_SHA256
    if not unchanged:
        exitstatus = session.exitstatus = pytest.ExitCode.INTERRUPTED
    for nodeid in SOURCE_ITEMS:
        phases = REPORTS.get(nodeid, {})
        outcomes = [phase["outcome"] for phase in phases.values()]
        result = (
            "failed"
            if "failed" in outcomes
            else "skipped"
            if "skipped" in outcomes
            else "not-run"
            if set(phases) != {"setup", "call", "teardown"}
            else "xpassed"
            if any(phase.get("xfail") for phase in phases.values())
            else "passed"
        )
        reports[nodeid] = result
        rows = [r for r in ROWS if r["nodeid"] == nodeid]
        replayed = sum(
            r["kind"] == "replay" and r.get("adapter_assertions_passed", False) for r in rows
        )
        status = "FULL" if rows and replayed == len(rows) else "PARTIAL" if replayed else "NO"
        coverage[nodeid] = {
            "status": status,
            "invocations": len(rows),
            "completed_replays": replayed,
            "qualified_replays": replayed if result == "passed" and unchanged else 0,
            "direct_test_result": result,
            **SOURCE_ITEMS[nodeid],
            "exclusions": [r["reason"] for r in rows if r["kind"] != "replay"]
            or (
                []
                if replayed
                else ["Internal/helper/mock or registration invariant; no subprocess hook payload"]
            ),
        }
    Path(session.config.getoption("--codex-replay-receipt")).write_text(
        json.dumps(
            {
                "exitstatus": exitstatus,
                "reports": reports,
                "phases": REPORTS,
                "coverage": coverage,
                "rows": ROWS,
                "copied_source_hashes": COPIED,
                "instrument_sha256": INSTRUMENT_SHA256,
                "instrument_final_sha256": final_hash,
                "instrument_unchanged": unchanged,
            },
            indent=2,
        )
    )

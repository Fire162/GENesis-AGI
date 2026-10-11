"""genesis.browser.engine: which Camoufox binary a launch may use, decided without
reaching camoufox's own resolution (which deletes a pre-0.5 engine and downloads a
new one when ``executable_path`` is None).

CI installs neither camoufox nor playwright, so every cell runs through seams:
``_dist_version`` / ``_dist_files`` for package metadata, ``XDG_CACHE_HOME`` for the
engine cache, and fake ``camoufox`` modules that carry upstream 0.5.7's real names
(``BrowserPin``, ``load_pin``, ``matches``, ``list_installed``, ``InstalledVersion``).
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
import types
from dataclasses import dataclass
from pathlib import Path

import pytest

from genesis.browser import engine

# ── seams ──────────────────────────────────────────────────────────────────────


@pytest.fixture
def cache(tmp_path, monkeypatch):
    """An empty camoufox cache root under a private XDG_CACHE_HOME."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("CAMOUFOX_EXECUTABLE_PATH", raising=False)
    root = engine.camoufox_install_dir()
    assert root == tmp_path / "xdg" / "camoufox"  # guard: the seam really moved it
    return root


def _versions(monkeypatch, *, camoufox="0.4.11", playwright="1.58.0", files=None):
    found = {"camoufox": camoufox, "playwright": playwright}
    monkeypatch.setattr(engine, "_dist_version", lambda name: found.get(name))
    monkeypatch.setattr(engine, "_dist_files", lambda name: files)


def _engine_dir(d: Path, *, version="135.0.1", version_json=True, ini=True, executable=True):
    d.mkdir(parents=True, exist_ok=True)
    binary = d / "camoufox-bin"
    binary.write_bytes(b"\x7fELF")
    binary.chmod(0o755 if executable else 0o644)
    if version_json:
        (d / "version.json").write_text(json.dumps({"version": version, "release": "beta.24"}))
    if ini:
        (d / "application.ini").write_text(f"[App]\nName=Camoufox\nVersion={version}\n")
    return binary


# Fakes of camoufox 0.5.7's read-only selection API (browser_pin.py, multiversion.py).


@dataclass(frozen=True)
class _BrowserPin:
    tag: str
    repo: str
    repo_name: str
    version: str
    build: str

    @property
    def spec(self) -> str:
        return f"{self.version}-{self.build}"


@dataclass
class _Version:
    build: str
    version: str | None = None


@dataclass
class _InstalledVersion:
    repo_name: str
    version: _Version
    path: Path


PIN = _BrowserPin("v156.0.1-beta.34", "daijro/camoufox", "official", "156.0.1", "beta.34")


def _fake_camoufox(monkeypatch, *, pin=PIN, installed=(), list_raises=None):
    def matches(p, repo_name, version, build):
        return (repo_name.lower(), version, build) == (p.repo_name, p.version, p.build)

    def list_installed():
        if list_raises is not None:
            raise list_raises
        return list(installed)

    pkg = types.ModuleType("camoufox")
    bp = types.ModuleType("camoufox.browser_pin")
    bp.BrowserPin, bp.load_pin, bp.matches = _BrowserPin, (lambda path=None: pin), matches
    mv = types.ModuleType("camoufox.multiversion")
    mv.list_installed = list_installed
    pkg.browser_pin, pkg.multiversion = bp, mv
    for name, mod in (
        ("camoufox", pkg),
        ("camoufox.browser_pin", bp),
        ("camoufox.multiversion", mv),
    ):
        monkeypatch.setitem(sys.modules, name, mod)


def _installed(cache, version="156.0.1", build="beta.34", repo="official", suffix="ab12cd34", **kw):
    d = cache / "browsers" / repo / f"{version}-{build}-{suffix}"
    _engine_dir(d, version=version, **kw)
    return _InstalledVersion(repo, _Version(build, version), d)


# ── packages ───────────────────────────────────────────────────────────────────


def test_no_camoufox_package(cache, monkeypatch):
    _versions(monkeypatch, camoufox=None)
    assert engine.resolve_camoufox_engine().kind == engine.NO_PACKAGE


def test_no_playwright_package(cache, monkeypatch):
    _versions(monkeypatch, playwright=None)
    assert engine.resolve_camoufox_engine().kind == engine.PLAYWRIGHT_MISSING


def test_unparseable_camoufox_version_is_invalid(cache, monkeypatch):
    _versions(monkeypatch, camoufox="dev")
    status = engine.resolve_camoufox_engine()
    assert status.kind == engine.INVALID and "dev" in status.detail


# ── pre-0.5: the legacy root engine, the binary launched today ─────────────────


def test_legacy_root_engine_is_ready(cache, monkeypatch):
    _versions(monkeypatch, camoufox="0.4.11")
    binary = _engine_dir(cache)
    status = engine.resolve_camoufox_engine()
    assert status.kind == engine.READY
    assert status.path == binary


def test_legacy_missing_engine_is_not_installed(cache, monkeypatch):
    _versions(monkeypatch, camoufox="0.4.11")
    status = engine.resolve_camoufox_engine()
    assert status.kind == engine.NOT_INSTALLED
    assert "camoufox fetch" in status.detail


def test_legacy_engine_without_exec_bit_is_invalid_and_says_retry(cache, monkeypatch):
    _versions(monkeypatch, camoufox="0.4.11")
    _engine_dir(cache, executable=False)
    status = engine.resolve_camoufox_engine()
    assert status.kind == engine.INVALID
    assert "fetch was interrupted" in status.detail


def test_engine_without_version_line_is_invalid(cache, monkeypatch):
    """Without application.ini's Version= camoufox falls back to get_active_path(),
    which writes config.json (utils.resolve_verstr)."""
    _versions(monkeypatch, camoufox="0.4.11")
    _engine_dir(cache, ini=False)
    assert engine.resolve_camoufox_engine().kind == engine.INVALID


def test_engine_without_version_json_is_invalid(cache, monkeypatch):
    """camoufox writes version.json last-but-one on install (multiversion.py:450)."""
    _versions(monkeypatch, camoufox="0.4.11")
    _engine_dir(cache, version_json=False)
    status = engine.resolve_camoufox_engine()
    assert status.kind == engine.INVALID and "version.json" in status.detail


def test_engine_with_empty_version_line_is_invalid(cache, monkeypatch):
    _versions(monkeypatch, camoufox="0.4.11")
    _engine_dir(cache)
    (cache / "application.ini").write_text("[App]\nVersion=\n")
    status = engine.resolve_camoufox_engine()
    assert status.kind == engine.INVALID and "Version" in status.detail


# ── 0.5.7+: the engine the package pin names ──────────────────────────────────


def test_pinned_engine_is_ready(cache, monkeypatch):
    _versions(monkeypatch, camoufox="0.5.7")
    iv = _installed(cache)
    _fake_camoufox(monkeypatch, installed=[iv])
    status = engine.resolve_camoufox_engine()
    assert status.kind == engine.READY
    assert status.path == iv.path / "camoufox-bin"


def test_prerelease_package_version_parses(cache, monkeypatch):
    _versions(monkeypatch, camoufox="0.5.8b1")
    iv = _installed(cache)
    _fake_camoufox(monkeypatch, installed=[iv])
    assert engine.resolve_camoufox_engine().kind == engine.READY


def test_unpinned_build_is_not_selected(cache, monkeypatch):
    _versions(monkeypatch, camoufox="0.5.7")
    other = _installed(cache, version="150.0.2", build="beta.25")
    _fake_camoufox(monkeypatch, installed=[other])
    status = engine.resolve_camoufox_engine()
    assert status.kind == engine.NOT_INSTALLED
    assert PIN.spec in status.detail


def test_first_valid_duplicate_wins(cache, monkeypatch):
    _versions(monkeypatch, camoufox="0.5.7")
    broken = _installed(cache, suffix="00000000", executable=False)
    good = _installed(cache, suffix="ffffffff")
    _fake_camoufox(monkeypatch, installed=[broken, good])
    status = engine.resolve_camoufox_engine()
    assert status.kind == engine.READY and status.path == good.path / "camoufox-bin"


def test_only_broken_pinned_engine_is_invalid(cache, monkeypatch):
    _versions(monkeypatch, camoufox="0.5.7")
    broken = _installed(cache, executable=False)
    _fake_camoufox(monkeypatch, installed=[broken])
    status = engine.resolve_camoufox_engine()
    assert status.kind == engine.INVALID and "fetch was interrupted" in status.detail


def test_package_without_pin_is_invalid(cache, monkeypatch):
    _versions(monkeypatch, camoufox="0.5.7")
    _fake_camoufox(monkeypatch, pin=None)
    status = engine.resolve_camoufox_engine()
    assert status.kind == engine.INVALID and "pin" in status.detail
    assert "tell the user" in status.detail


# ── each INVALID cause names the step that repairs it ─────────────────────────


def test_non_executable_engine_is_repaired_by_chmod_not_fetch(cache, monkeypatch):
    """camoufox writes version.json, then runs chmod -R 755 (multiversion.py:450-454);
    a fetch killed in between leaves this state, and fetch skips a directory that
    already has version.json, so fetching again never repairs it."""
    _versions(monkeypatch, camoufox="0.5.7")
    broken = _installed(cache, executable=False)
    _fake_camoufox(monkeypatch, installed=[broken])
    detail = engine.resolve_camoufox_engine().detail
    assert f"chmod -R 755 {broken.path}" in detail
    assert "camoufox fetch" not in detail


def test_corrupt_pinned_engine_is_removed_then_fetched(cache, monkeypatch):
    _versions(monkeypatch, camoufox="0.5.7")
    broken = _installed(cache, ini=False)
    _fake_camoufox(monkeypatch, installed=[broken])
    detail = engine.resolve_camoufox_engine().detail
    assert f"remove `{broken.path}`" in detail
    assert detail.index("remove") < detail.index("camoufox fetch")


def test_legacy_engine_without_exec_bit_is_repaired_by_chmod(cache, monkeypatch):
    _versions(monkeypatch, camoufox="0.4.11")
    _engine_dir(cache, executable=False)
    assert f"chmod -R 755 {cache}" in engine.resolve_camoufox_engine().detail


def test_invalid_override_names_the_override_as_the_fix(cache, monkeypatch, tmp_path):
    _versions(monkeypatch, camoufox="0.5.7")
    monkeypatch.setenv("CAMOUFOX_EXECUTABLE_PATH", str(tmp_path / "nope" / "camoufox-bin"))
    detail = engine.resolve_camoufox_engine().detail
    assert "unset CAMOUFOX_EXECUTABLE_PATH" in detail
    assert "camoufox fetch" not in detail


def test_0_5_without_browser_pin_module_is_invalid(cache, monkeypatch):
    """0.5.3-0.5.6 ship multiversion.py but no browser_pin.py (MEASURED, 0.5.6 wheel)."""
    _versions(
        monkeypatch,
        camoufox="0.5.6",
        files=["camoufox/__init__.py", "camoufox/multiversion.py", "camoufox/pkgman.py"],
    )
    status = engine.resolve_camoufox_engine()
    assert status.kind == engine.INVALID
    assert "0.5.7" in status.detail


@pytest.mark.parametrize(
    "exc",
    [
        KeyError("build"),
        OSError("permission denied"),
        ValueError("x"),
        # upstream Version() indexes the first character of each build part, so
        # any browsers/ entry whose build is "" raises this (MEASURED, 0.5.7)
        IndexError("string index out of range"),
        RuntimeError("anything else"),
    ],
)
def test_upstream_lookup_failure_is_invalid_never_raises(cache, monkeypatch, exc):
    _versions(monkeypatch, camoufox="0.5.7")
    _fake_camoufox(monkeypatch, list_raises=exc)
    status = engine.resolve_camoufox_engine()
    assert status.kind == engine.INVALID
    assert "lookup failed" in status.detail
    assert "tell the user" in status.detail


def test_import_failure_is_invalid_never_raises(cache, monkeypatch):
    _versions(monkeypatch, camoufox="0.5.7")
    monkeypatch.setitem(sys.modules, "camoufox", None)  # import raises ImportError
    status = engine.resolve_camoufox_engine()
    assert status.kind == engine.INVALID


# ── CAMOUFOX_EXECUTABLE_PATH: upstream's own override, the only one honoured ──


def test_override_is_used(cache, monkeypatch, tmp_path):
    _versions(monkeypatch, camoufox="0.5.7")
    binary = _engine_dir(tmp_path / "custom", version_json=False)
    monkeypatch.setenv("CAMOUFOX_EXECUTABLE_PATH", str(binary))
    status = engine.resolve_camoufox_engine()
    assert status.kind == engine.READY and status.path == binary


def test_invalid_override_never_falls_through(cache, monkeypatch, tmp_path):
    _versions(monkeypatch, camoufox="0.4.11")
    _engine_dir(cache)  # a perfectly good default engine
    monkeypatch.setenv("CAMOUFOX_EXECUTABLE_PATH", str(tmp_path / "nope" / "camoufox-bin"))
    status = engine.resolve_camoufox_engine()
    assert status.kind == engine.INVALID
    assert "CAMOUFOX_EXECUTABLE_PATH" in status.detail


def test_blank_override_is_unset(cache, monkeypatch):
    _versions(monkeypatch, camoufox="0.4.11")
    binary = _engine_dir(cache)
    monkeypatch.setenv("CAMOUFOX_EXECUTABLE_PATH", "   ")
    status = engine.resolve_camoufox_engine()
    assert status.kind == engine.READY and status.path == binary


def test_override_does_not_mask_missing_playwright(cache, monkeypatch, tmp_path):
    _versions(monkeypatch, playwright=None)
    binary = _engine_dir(tmp_path / "custom")
    monkeypatch.setenv("CAMOUFOX_EXECUTABLE_PATH", str(binary))
    assert engine.resolve_camoufox_engine().kind == engine.PLAYWRIGHT_MISSING


# ── the operator hint ─────────────────────────────────────────────────────────


def test_hint_marks_legacy_engine_before_fetch(cache, monkeypatch):
    """0.5's fetch deletes the whole cache when .0.5_FLAG is absent
    (__main__.py:259-263), so the flag step must come first."""
    _versions(monkeypatch, camoufox="0.5.7")
    _engine_dir(cache)  # a pre-0.5 root engine, no flag
    _fake_camoufox(monkeypatch, installed=[])
    detail = engine.resolve_camoufox_engine().detail
    assert ".0.5_FLAG" in detail
    assert detail.index(".0.5_FLAG") < detail.index("camoufox fetch")


def test_hint_has_no_flag_step_when_flag_present(cache, monkeypatch):
    _versions(monkeypatch, camoufox="0.5.7")
    _engine_dir(cache)
    (cache / ".0.5_FLAG").touch()
    _fake_camoufox(monkeypatch, installed=[])
    assert ".0.5_FLAG" not in engine.resolve_camoufox_engine().detail


def test_hint_keeps_the_download_off_cc_tmp(cache, monkeypatch):
    """The 1.3 GB download is a TMPDIR temp file; a session's TMPDIR is a small
    quota, and a missing TMPDIR falls back to /tmp."""
    _versions(monkeypatch, camoufox="0.4.11")
    detail = engine.resolve_camoufox_engine().detail
    assert "mkdir -p ~/tmp && TMPDIR=~/tmp" in detail


def test_hint_names_this_interpreter(cache, monkeypatch):
    _versions(monkeypatch, camoufox="0.4.11")
    assert f"{sys.executable} -m camoufox fetch" in engine.resolve_camoufox_engine().detail


def test_hint_is_an_operator_step_and_names_no_script(cache, monkeypatch):
    _versions(monkeypatch, camoufox="0.4.11")
    detail = engine.resolve_camoufox_engine().detail
    assert "tell the user" in detail
    assert "scripts/" not in detail


# ── module shape ──────────────────────────────────────────────────────────────


def test_module_never_imports_camoufox_at_top_level():
    """Importing any camoufox submodule imports playwright; genesis-server asks
    through a subprocess so it never does."""
    tree = ast.parse(Path(engine.__file__).read_text())
    for node in tree.body:
        if isinstance(node, ast.Import):
            assert not any(a.name.split(".")[0] == "camoufox" for a in node.names)
        if isinstance(node, ast.ImportFrom):
            assert (node.module or "").split(".")[0] != "camoufox"


def test_never_calls_camoufox_own_resolution():
    source = Path(engine.__file__).read_text()
    for name in ("camoufox_path", "get_path", "get_active_path", "launch_path", "set_active"):
        assert f"{name}(" not in source


def test_status_json_round_trip(tmp_path):
    status = engine.EngineStatus(engine.READY, "ok", tmp_path / "camoufox-bin")
    assert engine.EngineStatus.from_json(status.to_json()) == status
    blank = engine.EngineStatus(engine.NO_PACKAGE, "none")
    assert engine.EngineStatus.from_json(blank.to_json()) == blank


def test_cli_prints_one_json_line(tmp_path):
    env = {"XDG_CACHE_HOME": str(tmp_path), "PATH": "/usr/bin:/bin"}
    src = str(Path(engine.__file__).resolve().parents[2])
    env["PYTHONPATH"] = src
    out = subprocess.run(
        [sys.executable, "-m", "genesis.browser.engine", "--json"],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
        check=True,
    )
    lines = out.stdout.strip().splitlines()
    status = engine.EngineStatus.from_json(json.loads(lines[-1]))
    assert status.kind in {
        engine.READY,
        engine.NOT_INSTALLED,
        engine.INVALID,
        engine.NO_PACKAGE,
        engine.PLAYWRIGHT_MISSING,
    }


def test_operator_steps_quote_their_paths(tmp_path, monkeypatch):
    """A cache path with shell metacharacters stays one quoted word in the step."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "x; rm -rf ~"))
    monkeypatch.delenv("CAMOUFOX_EXECUTABLE_PATH", raising=False)
    _versions(monkeypatch, camoufox="0.4.11")
    root = engine.camoufox_install_dir()
    _engine_dir(root, executable=False)
    detail = engine.resolve_camoufox_engine().detail
    assert f"chmod -R 755 '{root}'" in detail

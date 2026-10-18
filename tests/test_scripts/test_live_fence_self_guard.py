"""The deploy scripts' own test-fence guard (rework of #3314, PR A).

``refuse_if_test_fenced_live`` in ``scripts/lib/live_system_guard.sh`` is the
self-guard half of the live fence: when the suite-wide fence exports
``GENESIS_TEST_FENCE_LIVE_ROOTS`` (a colon-separated list of real paths no
test may let a deploy script act on), each deploy script refuses — exit 97 —
as its first statement after resolving the root it acts on, before any side
effect. Because the check runs inside the script after bash's own expansion,
it holds in every descendant however the script was launched — the launch
forms the deleted argv/shell-string modelling could not refuse.

These tests run the REAL scripts against a FIXTURE tree: a directory holding
copies of the script and the guard library, named in
``GENESIS_TEST_FENCE_LIVE_ROOTS``. With the variable set the script must exit
97 and log the refusal; with it unset the script must take its normal path.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "scripts"
GUARD_LIB = SCRIPTS / "lib" / "live_system_guard.sh"
REFUSED = "REFUSED (test fence)"

# (script file, extra env mapping the fixture's acting root, argv)
# backup.sh/restore.sh act on ${GENESIS_DIR:-$HOME/genesis}, not their checkout.
DEPLOY_SCRIPTS = [
    ("update.sh", {}, ["--definitely-bad"]),
    ("deploy_code_only.sh", {}, ["bogus-mode"]),
    ("backup.sh", {"GENESIS_DIR": "{fixture}"}, []),
    ("restore.sh", {"GENESIS_DIR": "{fixture}"}, ["--help"]),
    ("bootstrap.sh", {}, []),
    ("deploy_candidates", {}, ["bogus-command"]),
]


def _env(home: Path, fixture: Path | None, log: Path | None, extra: dict) -> dict:
    """A child env: private HOME, fence variables controlled per test."""
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    for key, value in extra.items():
        env[key] = value.format(fixture=str(fixture), home=str(home))
    if fixture is not None:
        env["GENESIS_TEST_FENCE_LIVE_ROOTS"] = str(fixture)
    if log is not None:
        env["GENESIS_TEST_FENCE_LOG"] = str(log)
    return env


@pytest.fixture()
def fixture_tree(tmp_path: Path) -> Path:
    """scripts/<each deploy script> + scripts/lib/live_system_guard.sh."""
    tree = tmp_path / "fixture"
    (tree / "scripts" / "lib").mkdir(parents=True)
    shutil.copy2(GUARD_LIB, tree / "scripts" / "lib" / "live_system_guard.sh")
    for name, _extra, _argv in DEPLOY_SCRIPTS:
        shutil.copy2(SCRIPTS / name, tree / "scripts" / name)
    return tree


def _run(argv: list[str], env: dict, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(
        argv, env=env, capture_output=True, text=True, timeout=60, **kw
    )


# ── per-script refusal on the fixture ────────────────────────────────


@pytest.mark.parametrize(("name", "extra", "argv"), DEPLOY_SCRIPTS)
def test_script_refuses_fenced_root(fixture_tree, tmp_path, name, extra, argv):
    home = tmp_path / f"home-{name}"
    home.mkdir()
    log = tmp_path / f"hits-{name}.log"
    env = _env(home, fixture_tree, log, extra)
    proc = _run(["bash", str(fixture_tree / "scripts" / name), *argv], env)
    assert proc.returncode == 97, (
        f"{name} must exit 97 on a fenced root:\n{proc.stdout}\n{proc.stderr}"
    )
    assert REFUSED in proc.stderr
    assert REFUSED in log.read_text()


@pytest.mark.parametrize(("name", "extra", "argv"), DEPLOY_SCRIPTS)
def test_script_unset_takes_normal_path(fixture_tree, tmp_path, name, extra, argv):
    """Variable unset: the guard is a no-op, the script takes its normal path —
    observable as ANY outcome other than the fence's refusal."""
    home = tmp_path / f"home-normal-{name}"
    home.mkdir()
    env = _env(home, None, None, extra)
    proc = _run(["bash", str(fixture_tree / "scripts" / name), *argv], env)
    assert proc.returncode != 97
    assert REFUSED not in proc.stderr


def test_empty_variable_takes_normal_path(fixture_tree, tmp_path):
    home = tmp_path / "home-empty"
    home.mkdir()
    env = _env(home, None, None, {})
    env["GENESIS_TEST_FENCE_LIVE_ROOTS"] = ""
    proc = _run(["bash", str(fixture_tree / "scripts" / "update.sh"), "--bad"], env)
    assert proc.returncode == 1  # the usage error, not the fence
    assert "unknown argument" in proc.stderr


def test_refusal_precedes_update_self_copy(fixture_tree, tmp_path):
    """update.sh's guard sits before the copy-to-temp block: a fenced run must
    leave no genesis-update-* temp copy under HOME."""
    home = tmp_path / "home-nocopy"
    (home / "tmp").mkdir(parents=True)
    env = _env(home, fixture_tree, None, {})
    proc = _run(["bash", str(fixture_tree / "scripts" / "update.sh")], env)
    assert proc.returncode == 97
    assert not list((home / "tmp").iterdir()), "the self-copy is a side effect"


# ── the five launch forms, refused by the script itself ──────────────


@pytest.mark.parametrize(
    "form",
    [
        "plain",
        "nested-c",
        "bash-env",
        "rcfile",
        "here-string",
    ],
)
def test_five_launch_forms_refused(fixture_tree, tmp_path, form):
    """Every launch form in the rework spec's table is refused BY THE SCRIPT
    (exit 97), where the old argv/shell-string judge allowed three of them."""
    home = tmp_path / f"home-{form}"
    home.mkdir()
    log = tmp_path / f"hits-{form}.log"
    script = fixture_tree / "scripts" / "update.sh"
    env = _env(home, fixture_tree, log, {})
    if form == "plain":
        argv = ["bash", str(script)]
    elif form == "nested-c":
        argv = ["bash", "-c", f"bash {script}"]
    elif form == "bash-env":
        argv = ["bash", "-c", "true"]
        env["BASH_ENV"] = str(script)
    elif form == "rcfile":
        argv = ["bash", "--rcfile", str(script), "-i"]
    else:  # here-string
        argv = ["bash", "-c", "bash <<< $'bash " + str(script) + "'"]
    proc = _run(argv, env, stdin=subprocess.DEVNULL)
    assert proc.returncode == 97, (
        f"launch form {form} was not refused:\n{proc.stdout}\n{proc.stderr}"
    )
    assert REFUSED in log.read_text()


# ── the guard function's own semantics ───────────────────────────────


def _guard(home: Path, roots: str | None, acting: str, extra_env: dict | None = None):
    env = _env(home, None, None, {})
    if roots is not None:
        env["GENESIS_TEST_FENCE_LIVE_ROOTS"] = roots
    if extra_env:
        env.update(extra_env)
    return _run(
        [
            "bash",
            "-c",
            f'. "{GUARD_LIB}"; refuse_if_test_fenced_live "$1"',
            "guard",
            acting,
        ],
        env,
    )


def test_guard_unset_and_empty_allow(tmp_path):
    home = tmp_path / "h"
    home.mkdir()
    assert _guard(home, None, str(tmp_path)).returncode == 0
    assert _guard(home, "", str(tmp_path)).returncode == 0


def test_guard_root_equal_under_and_outside(tmp_path):
    root = tmp_path / "live"
    root.mkdir()
    home = tmp_path / "h"
    home.mkdir()
    inside = _guard(home, str(root), str(root / "scripts"))
    assert inside.returncode == 1
    assert "under or equal" not in inside.stderr and REFUSED in inside.stderr
    outside = _guard(home, str(root), str(tmp_path / "elsewhere"))
    assert outside.returncode == 0


def test_guard_matches_home_and_genesis_home(tmp_path):
    root = tmp_path / "live"
    home = root / "home"  # HOME sits under the live root
    home.mkdir(parents=True)
    proc = _guard(home, str(root), str(tmp_path / "unrelated"))
    assert proc.returncode == 1, proc.stderr
    other = tmp_path / "h2"
    other.mkdir()
    proc = _guard(other, str(root), str(tmp_path / "unrelated"), {"GENESIS_HOME": str(root / ".genesis")})
    assert proc.returncode == 1, proc.stderr


def test_guard_symlinked_and_nonexistent_candidate(tmp_path):
    root = tmp_path / "live"
    (root / "sub").mkdir(parents=True)
    link = tmp_path / "link"
    link.symlink_to(root / "sub")
    home = tmp_path / "h"
    home.mkdir()
    assert _guard(home, str(root), str(link)).returncode == 1
    assert _guard(home, str(root), str(root / "not" / "there" / "yet")).returncode == 1


def test_guard_appends_to_log(tmp_path):
    root = tmp_path / "live"
    root.mkdir()
    home = tmp_path / "h"
    home.mkdir()
    log = tmp_path / "hits.log"
    env = _env(home, None, log, {})
    env["GENESIS_TEST_FENCE_LIVE_ROOTS"] = str(root)
    proc = _run(
        [
            "bash",
            "-c",
            f'. "{GUARD_LIB}"; refuse_if_test_fenced_live "{root}"',
        ],
        env,
    )
    assert proc.returncode == 1
    assert REFUSED in log.read_text()

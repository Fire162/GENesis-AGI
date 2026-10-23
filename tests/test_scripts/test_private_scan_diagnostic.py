"""One-off CI diagnosis keeps all match details inside authenticated encryption."""

import base64
import hashlib
import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/ci/private_scan_diagnostic.py"


def run(*args, **kwargs):
    return subprocess.run(args, capture_output=True, **kwargs)


@pytest.fixture(scope="module")
def recipient(tmp_path_factory):
    root = tmp_path_factory.mktemp("diagnostic-recipient")
    cert, key = root / "recipient.pem", root / "private.pem"
    p = run("openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
            "-subj", "/CN=synthetic-diagnostic", "-keyout", str(key), "-out", str(cert))
    assert p.returncode == 0
    return cert, key


@pytest.fixture
def history(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in [("init",), ("config", "user.name", "Fixture"),
                 ("config", "user.email", "fixture@example.com"),
                 ("commit", "--allow-empty", "-m", "initial")]:
        assert run("git", *args, cwd=repo).returncode == 0
    base = run("git", "rev-parse", "HEAD", cwd=repo).stdout.decode().strip()
    (repo / "example.txt").write_text("fixture-secret\nfixture-secret\npublic\nnel\u0085fixture-unicode\n")
    assert run("git", "add", "example.txt", cwd=repo).returncode == 0
    assert run("git", "commit", "-m", "fixture-note", cwd=repo).returncode == 0
    head = run("git", "rev-parse", "HEAD", cwd=repo).stdout.decode().strip()
    return repo, base, head


def diagnose(history, recipient, patterns):
    repo, base, head = history
    cert, _ = recipient
    env = dict(os.environ, PRIV=patterns)
    if os.environ.get("TA_DIAGNOSTIC_BASELINE") == "1":
        path = repo / "patterns.txt"
        path.write_text(patterns)
        return run("python3", str(ROOT / "scripts/ci/private_pattern_scan.py"),
                   "--patterns", str(path), input=b"fixture-secret\nfixture-note\n", cwd=repo)
    return run("python3", str(SCRIPT), "--base", base, "--head", head,
               "--certificate", str(cert), env=env, cwd=repo)


def ciphertext(output):
    lines = output.decode().splitlines()
    assert lines[0] == "BEGIN ENCRYPTED PRIVATE SCAN DIAGNOSTIC"
    assert lines[-1] == "END ENCRYPTED PRIVATE SCAN DIAGNOSTIC"
    return base64.b64decode("".join(lines[1:-1]), validate=True)


def decrypt(body, recipient):
    cert, key = recipient
    return run("openssl", "cms", "-decrypt", "-binary", "-inform", "DER",
               "-recip", str(cert), "-inkey", str(key), input=body)


@pytest.mark.parametrize("patterns,count", [
    ("fixture-secret\nfixture-note", 3), ("never-in-fixture", 0),
    ("# ignored\n\nfixture-unicode\n", 1),
])
def test_only_encrypted_matching_details_leave_process(history, recipient, patterns, count):
    p = diagnose(history, recipient, patterns)
    blob = ciphertext(p.stdout)
    assert p.returncode == 0 and not p.stderr
    for secret in ("fixture-secret", "fixture-note", "fixture-unicode"):
        assert secret.encode() not in p.stdout
    plain = decrypt(blob, recipient)
    assert plain.returncode == 0
    report = json.loads(plain.stdout)
    assert report["matching_lines"] == count == len(report["matches"])
    assert report["base"] == history[1] and report["head"] == history[2]
    spec = importlib.util.spec_from_file_location("diagnostic_added_lines", ROOT / "scripts/ci/leak_scan_added_lines.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    scan = ("range", history[1] + ".." + history[2])
    stream = module.added_lines(scan, cwd=str(history[0])) + "\n" + module.commit_messages(scan, cwd=str(history[0]))
    stream = stream.replace("\0", "").rstrip("\n") + "\n\n"
    assert report["haystack_sha256"] == hashlib.sha256(stream.encode()).hexdigest()
    for match in report["matches"]:
        line = stream.splitlines()[match["stream_line"] - 1]
        assert match["line_sha256"] == hashlib.sha256(line.encode()).hexdigest()
        assert "line_text" not in match and match["patterns"]


def test_altered_ciphertext_is_rejected(history, recipient):
    blob = bytearray(ciphertext(diagnose(history, recipient, "fixture-secret").stdout))
    blob[-1] ^= 1
    assert decrypt(bytes(blob), recipient).returncode != 0


@pytest.mark.parametrize("failure", ["pattern", "certificate", "history"])
def test_failure_never_prints_private_pattern_or_subprocess_buffers(history, recipient, failure):
    if failure == "certificate":
        recipient = (recipient[0].with_name("missing.pem"), recipient[1])
    elif failure == "history":
        history = (history[0], "0" * 40, history[2])
    patterns = "(fixture-secret" if failure == "pattern" else "fixture-secret"
    p = diagnose(history, recipient, patterns)
    assert p.returncode == 3 and not p.stdout
    assert b"fixture-secret" not in p.stderr
    assert p.stderr.startswith(b"Encrypted diagnosis unavailable (")


def test_existing_workflow_constraints_are_unchanged():
    original = yaml.safe_load(run("git", "show", "HEAD:.github/workflows/branch-leak-scan.yml", cwd=ROOT).stdout)
    current = yaml.safe_load((ROOT / ".github/workflows/branch-leak-scan.yml").read_text())
    steps = current["jobs"]["branch-leak-scan"]["steps"]
    diagnostic = steps.pop()
    assert current == original
    assert "always()" in diagnostic["if"]
    assert "refs/heads/chore/transcript-private-scan-diagnostic-20261022" in diagnostic["if"]
    assert "github.repository == 'WingedGuardian/GENesis-AGI'" in diagnostic["if"]
    assert diagnostic["env"] == {"PRIV": "${{ secrets.GENESIS_PRIVATE_PATTERNS }}"}

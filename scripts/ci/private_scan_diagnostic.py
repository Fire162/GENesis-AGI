#!/usr/bin/env python3
"""Temporary diagnosis: only authenticated ciphertext may leave this process."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import subprocess
import sys

import leak_scan_added_lines as added
import private_pattern_scan as scanner


def report(base: str, head: str) -> dict:
    raw_patterns = os.environ.get("PRIV", "")
    patterns = scanner.load_patterns(raw_patterns)
    spec = ("range", base + ".." + head)
    stream = added.added_lines(spec) + "\n" + added.commit_messages(spec)
    # Exact branch-push Bash command-substitution + printf framing.
    stream = stream.replace("\0", "").rstrip("\n") + "\n\n"
    matches = []
    for number, line in enumerate(stream.splitlines(), 1):
        matching = [{"active_index": i, "regex": p.pattern}
                    for i, p in enumerate(patterns, 1) if p.search(line)]
        if matching:
            matches.append({"stream_line": number,
                            "line_sha256": hashlib.sha256(line.encode()).hexdigest(),
                            "patterns": matching})
    return {"version": 1, "base": base, "head": head, "raw_patterns": raw_patterns,
            "haystack_sha256": hashlib.sha256(stream.encode()).hexdigest(),
            "matching_lines": len(matches), "matches": matches}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", required=True)
    ap.add_argument("--head", required=True)
    ap.add_argument("--certificate", required=True)
    args = ap.parse_args()
    try:
        body = json.dumps(report(args.base, args.head), separators=(",", ":")).encode()
        encrypted = subprocess.run(
            ["openssl", "cms", "-encrypt", "-binary", "-aes-256-gcm", "-outform", "DER",
             "-recip", args.certificate, "-keyopt", "rsa_padding_mode:oaep",
             "-keyopt", "rsa_oaep_md:sha256"],
            input=body, capture_output=True, check=True,
        ).stdout
        encoded = base64.b64encode(encrypted).decode("ascii")
    except Exception as exc:
        # Subprocess buffers, regexes and reports can contain private values.
        print(f"Encrypted diagnosis unavailable ({type(exc).__name__})", file=sys.stderr)
        return 3
    print("BEGIN ENCRYPTED PRIVATE SCAN DIAGNOSTIC")
    for start in range(0, len(encoded), 4000):
        print(encoded[start:start + 4000])
    print("END ENCRYPTED PRIVATE SCAN DIAGNOSTIC")
    return 0


if __name__ == "__main__":
    sys.exit(main())

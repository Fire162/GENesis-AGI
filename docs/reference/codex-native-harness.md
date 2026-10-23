# Native Codex hook harness

Run the opt-in integration file on a Unix host with Codex and the Genesis Python dependencies installed:

```bash
GENESIS_CODEX_NATIVE_TESTS=1 GENESIS_PYTEST_LOCK_WAIT=1 \
  python -m pytest tests/test_hooks/test_codex_native.py -q \
  -o tmp_path_retention_policy=all \
  --basetemp "$HOME/tmp/codex-native-harness-$$"
```

`GENESIS_CODEX_CLI_BIN` and `GENESIS_CODEX_APP_SERVER_BIN` can select different absolute binary paths. Both default to the installed `codex`. An opted-in run fails when a required binary is missing; ordinary test runs skip this file.

Each case creates an isolated home and workspace, starts a localhost Responses fixture provider, and requests one shell, patch, or fake MCP action. No production Genesis server, database, MCP configuration, credentials, or paid model is used. The fixture hook records its real native input and permits or denies the action. Tests check the actual file sink, including positive controls for each denial case.

The 20 cases cross both clients with both decisions and five call paths: direct shell, direct patch, nested code-mode shell, nested code-mode patch, and nested code-mode MCP. The model name selects native tool behavior; all responses come from the local fixture provider.

Results apply to the recorded binaries and `gpt-6.1-sol` tool metadata. Analytics and plugins are disabled in the isolated config; the tests also reject evidence of a plugin-clone attempt. This is configuration-based suppression, not an OS network sandbox.

Activate the Genesis Python environment first. Both the unique `--basetemp` and retention override are required to retain passing and failing evidence: the repository otherwise removes successful cases and its automatic per-process temporary directory. Pytest clears an explicit base directory at startup, so use a new path for each run.

Hook trust is scoped to the vetted fixture: `hooks/list` supplies its exact key and current hash, which are persisted only in the isolated configuration. No hook-trust bypass flag is used. This confirms trust of the hook definition, not integrity of the referenced script bytes: a separate isolated Codex 0.161.0 probe kept the same current hash and trusted status after adding a harmless script comment. Production readiness must verify guard contents separately. `native-evidence.json` in each pytest temporary directory records binary versions, hook discovery, fixture requests, native events, and stderr. The fake MCP is marked required so startup must succeed before the initial tool catalog is built.

The app-server path drives initialization, an ephemeral thread, and a turn over stdio. It exercises the native backend; it does not certify desktop folder selection, the desktop trust UI, or production validator policy. Those require separate readiness checks. This baseline file does not cover hook crashes, timeouts, later stdin input, or arbitrary indirect execution.

The separate opt-in `tests/test_hooks/test_codex_validator_adapter_native.py`
provides the qualification harness for the inactive interactive adapter on
Codex 0.163.0. It copies the guard
source and records its hashes, reuses the installed Python interpreter without
changing it, and exercises actual scratch receipts on both clients. It adds
child ask, crash, missing-script, malformed-output and timeout cases, advisory
delivery, relative patches and code-mode MCP admission. Raw direct MCP function
calls rejected as unsupported before any hook are transport controls, not
adapter denial evidence. The outer-timeout pair uses the same delayed denial:
the short deadline must let its harmless receipt execute, while the normal
deadline must refuse it. It checks hook startup and the effective trusted timeout;
CLI JSON does not expose the timeout message. This documents the accepted native
fail-open. These are
child failures caught by the adapter, not evidence that a failed outer native
hook is safe. Keep the unique base directory and retention options above when
running this file too. No production validator profile is activated.

The direct-shell positive case reads a real scratch body file containing prose
that mentions worktree removal. It requires that content to appear in the next
provider request and checks the separate action receipt. This qualifies actual
file-to-tool-output flow on each pinned client. The unit adapter tests separately
pin the known compound-heredoc refusal, the file-operand alternative and refusal
of a real removal after a data body; their supplied action commands never execute.

## Existing guard compatibility replay

Load `tests.test_hooks.codex_adapter_replay` explicitly as a pytest plugin to run
existing compatible Bash cases through the adapter and copied `genesis-hook`.
For ordinary cases, the original assertions and adapter decision/context checks
use the same child execution, preserving advisory deduplication. Explicit named
mappings retain direct controls for the missing-cwd fallback, the single-step
discard note and the compound-heredoc limitation. Lexical-cwd fixtures are
materialized only under the owned test directory, and the self-cwd test derives
its target after entering that directory. The copied public topology fallback
and imported source dependencies are hashed alongside the actual guard copies.
The discard guard's copied-tree cases retain their complete fixture hook tree,
including each intentionally missing helper. Each has a private runtime and a
receipt manifest; they are replayed without replacing their fault premise by
an intact copy.

For example, after activating the Genesis Python environment:

```bash
GENESIS_PYTEST_LOCK_WAIT=1 python -m genesis.hostmetrics run \
  --name codex-guard-replay --ram 1 --cpu 100 -- python -m pytest -q \
  -p tests.test_hooks.codex_adapter_replay \
  --codex-replay-receipt "$HOME/tmp/codex-guard-replay-$$.json" \
  tests/test_hooks/test_capped_read_advisory.py \
  tests/test_hooks/test_credential_surface_hook.py \
  tests/test_hooks/test_destructive_command_guard.py \
  tests/test_hooks/test_full_suite_guard.py \
  tests/test_hooks/test_git_discard_guard.py \
  tests/test_hooks/test_git_push_guard_repo.py \
  tests/test_hooks/test_main_checkout_guard.py \
  tests/test_hooks/test_pipe_status_guard.py \
  tests/test_hooks/test_pr_close_advisory.py \
  tests/test_hooks/test_protected_paths_guard.py \
  tests/test_hooks/test_push_guard_sqlite_write.py \
  tests/test_hooks/test_push_publish_ask_switch.py \
  tests/test_hooks/test_repo_routing_guard.py \
  tests/test_hooks/test_rm_trash_advisory.py \
  tests/test_hooks/test_secrets_env_access_guard.py \
  tests/test_hooks/test_tmux_kill_server_guard.py \
  tests/test_hooks/test_worktree_guard.py \
  -o tmp_path_retention_policy=all \
  --basetemp "$HOME/tmp/codex-guard-replay-$$"
```

The private receipt lists every test's source location, direct result, replay
count and `FULL`/`PARTIAL`/`NO` invocation coverage. A failed test has zero qualified
replays. Every unreplayed invocation has a reason; tests without a subprocess
payload retain their direct evidence and referenced source symbols. Internal
helper/mocked-main tests, Claude-only lifecycle/read/file event shapes, malformed
or empty non-native payloads and the explicitly excluded main-checkout Bash half
are not relabelled as adapter replays. Review the complete exception list rather
than adding direct-suite passes to the replay count. Receipts contain local
fixture paths and belong outside the repository. This is subprocess compatibility
qualification, separate from the pinned native-client harness above.

"""What the graph census can PROVE about another process: code source, kill
switch and database scope.

The census (``graph_census.py``) may start the FalkorDB cutover clock only on
evidence, so every rule here answers "unknown" rather than guessing. Each rule
is a pure function over one process's ``/proc`` facts (``ProcView``) and files
on disk; ``graph_census`` gathers the facts and records the verdicts.

**Import source, for an interpreter that does not insert its own ``src``**
(``interpreter_source``). CPython builds ``sys.path`` for
``<venv>/bin/python /abs/script.py`` as: the script's real directory,
``PYTHONPATH`` entries, the stdlib (from ``pyvenv.cfg``'s ``home``), then the
venv's site-packages and whatever its ``.pth`` files add or run; ``site`` also
imports ``sitecustomize``/``usercustomize`` from that path. The venv is
identified by the directory of ``argv[0]``, never by the binary: every venv's
``python`` is a symlink chain to the same system interpreter (MEASURED on a
live install, 2026-10-20), so the binary's realpath proves nothing. A name the
startup imports (``genesis``, a customize module, anything a ``.pth`` line or
the system ``sitecustomize`` imports) found in a ``PYTHONPATH`` entry or beside
the script would shadow the real one, so it makes the source unknown.

Stated residuals: the venv's own ``.pth`` files are trusted as they read NOW,
with no change-time rule, because every update reruns ``pip install -e`` and
rewrites them (owner ruling, 2026-10-20: accepted). The window this leaves: a
``pip install -e`` from a worktree into main's venv, a server started then, and
an update that rewrites the install back to main before the census reads it
would read as main. A standing procedure already forbids that install; a ``PYTHONPATH`` entry is proven only by its change time
(``st_ctime``, which userspace cannot set) being earlier than the process start;
the stdlib directory is trusted (root-owned on a packaged interpreter).

**Kill switch** (``secrets_values`` + ``switch_off``): what the server runs
with, not the startup loader alone. The memory lifespan's router reloads the
secrets file with ``override=True``, so a key the file carries beats the
exec-time value (owner ruling, 2026-10-20). The file counts only if it exists
and neither it nor any symlink on its path changed since the process started;
a missing file is UNKNOWN, never "enabled", and so is a startup value whose
file cannot be proven, since that file can override it.

**Database scope** (``db_paths`` + ``db_scope``): the primary database is fixed
when the server script is imported (exec env, else ``<root>/data``); a
``GENESIS_DB_PATH`` in its proven file becomes a second one after the router
reload. A server is foreign only when every database it may use differs from
the live one by file identity ``(st_dev, st_ino)``, so a hardlink or bind mount
of the live database is never mistaken for a scratch one. Caveat: on overlayfs the
inode can differ for one file across layers; an unknown or missing path is
never foreign. A foreign process can still reach the live graph ENGINE (its
socket comes from ``GENESIS_HOME``, not the database); its traversals serve no
live memory, so they are not live traffic, by the same reasoning that excludes
replay.

No reason code carries an environment value or a path from another process.
"""

from __future__ import annotations

import ast
import contextlib
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

#: A row's ``source`` when ``interpreter_source`` proved the main checkout.
SOURCE_MAIN = "main"
#: The only environment keys the census keeps from another process.
PROOF_ENV_KEYS = (
    "HOME",
    "PYTHONPATH",
    "PYTHONHOME",
    "PYTHONPLATLIBDIR",
    "SECRETS_PATH",
    "GENESIS_REPO_ROOT",
    "GENESIS_DB_PATH",
    "GENESIS_GRAPH_TELEMETRY_DISABLED",
)
_SWITCH = "GENESIS_GRAPH_TELEMETRY_DISABLED"
#: setuptools' ``distutils-precedence.pth``, byte for byte after strip.
_DISTUTILS_LINE = (
    "import os; var = 'SETUPTOOLS_USE_DISTUTILS'; enabled = os.environ.get(var, 'local')"
    " == 'local'; enabled and __import__('_distutils_hack').add_shim();"
)
_FINDER_LINE = re.compile(r"import (__editable___\w+_finder); \1\.install\(\)")
#: Interpreter options that consume the next argument (``python -X dev script``).
_PY_OPTS_WITH_VALUE = frozenset({"-X", "-W", "-Q"})
_CUSTOMIZE = ("sitecustomize", "usercustomize")


@dataclass(frozen=True)
class ProcView:
    """One process as ``/proc`` shows it. ``env`` holds ``PROOF_ENV_KEYS`` only."""

    pid: int
    argv: tuple[str, ...]
    env: dict[str, str]
    start: float
    exe: str | None
    mnt_ns: str | None


def expand_home(value: str, env: dict[str, str]) -> Path | None:
    """``value`` as THAT process would expand it: ``~`` uses its own ``HOME``.
    None when it cannot be resolved from here (a relative path, or ``~`` with
    no ``HOME`` in its environment)."""
    if value.startswith("~"):
        home = env.get("HOME")
        if not home or not (value == "~" or value.startswith("~/")):
            return None
        value = home + value[1:]
    path = Path(value)
    return path if path.is_absolute() else None


def _shadows(directory: Path, names: set[str]) -> bool:
    """Whether ``directory`` holds something importable under one of ``names``."""
    try:
        entries = os.listdir(directory)
    except OSError:
        return True
    for entry in entries:
        stem = entry.split(".", 1)[0]
        # __pycache__ and dist-info style names are never import targets.
        if stem not in names or not stem.isidentifier() or stem == "__pycache__":
            continue
        if (
            entry == stem
            or entry.endswith((".py", ".pyc"))
            or (entry.endswith(".so") and entry.startswith(stem + "."))
        ):
            return True
    return False


def _finder_mapping(site: Path, module: str) -> dict | None:
    try:
        tree = ast.parse((site / f"{module}.py").read_text())
    except (OSError, SyntaxError, ValueError):
        return None
    for node in tree.body:
        target = (
            node.target
            if isinstance(node, ast.AnnAssign)
            else (
                node.targets[0] if isinstance(node, ast.Assign) and len(node.targets) == 1 else None
            )
        )
        if isinstance(target, ast.Name) and target.id == "MAPPING" and node.value is not None:
            try:
                value = ast.literal_eval(node.value)
            except ValueError:
                return None
            return value if isinstance(value, dict) else None
    return None


def _pth_verdict(site: Path, name: str, main_src: str, imports: set[str]) -> bool:
    """Whether one ``.pth`` file is a known shape that cannot load other genesis
    code. Adds every module it imports to ``imports``."""
    try:
        lines = (site / name).read_text().splitlines()
    except (OSError, ValueError):
        return False
    # site.addpackage: a comment only when the RAW line starts with "#", a blank
    # line by strip(), an import line by its raw prefix (exec'd whole), and any
    # other line is a path joined to the site dir after rstrip().
    for raw in lines:
        if raw.startswith("#") or not raw.strip():
            continue
        line = raw.strip()
        if raw.startswith(("import ", "import\t")) and line == _DISTUTILS_LINE:
            imports.add("_distutils_hack")
            continue
        finder = _FINDER_LINE.fullmatch(line) if raw.startswith("import ") else None
        if finder:
            module = finder.group(1)
            mapping = _finder_mapping(site, module)
            if mapping is None:
                return False
            target = mapping.get("genesis")
            if target is not None and os.path.realpath(target) != os.path.join(main_src, "genesis"):
                return False
            imports.add(module)
            continue
        if raw.startswith(("import ", "import\t")):
            return False
        if os.path.realpath(os.path.join(site, raw.rstrip())) != main_src:
            return False
    return True


def _script(argv: tuple[str, ...]) -> str | None:
    """The script a ``python [opts] script`` command runs; None for ``-m``/``-c``."""
    i = 1
    while i < len(argv) and argv[i].startswith("-"):
        if argv[i] in ("-m", "-c"):
            return None
        i += 2 if argv[i] in _PY_OPTS_WITH_VALUE else 1
    return argv[i] if i < len(argv) else None


def interpreter_source(
    proc: ProcView, *, main_root: Path, own_mnt_ns: str | None, script: Path | None = None
) -> list[str]:
    """``[]`` when the process provably loads genesis from ``main_root``'s src,
    else the reason codes. See the module docstring for the rule. ``script``,
    when given, is the entry script it must run: the script's own code (its
    secrets loader, its startup) is part of what decides the switch and DB."""
    if own_mnt_ns is None or proc.mnt_ns != own_mnt_ns:
        return ["other_mount_ns"]  # its paths are not ours to resolve
    argv0 = proc.argv[0] if proc.argv else ""
    if not os.path.isabs(argv0):
        return ["argv0_not_absolute"]
    venv = main_root / ".venv"
    if os.path.realpath(os.path.dirname(argv0)) != os.path.realpath(venv / "bin"):
        return ["not_main_venv"]
    reasons: list[str] = []
    cfg: dict[str, str] = {}
    try:
        for line in (venv / "pyvenv.cfg").read_text().splitlines():
            key, sep, value = line.partition("=")
            if sep:
                cfg[key.strip()] = value.strip()
    except OSError:
        pass
    executable = cfg.get("executable")
    if cfg.get("include-system-site-packages") != "false" or not executable or not cfg.get("home"):
        reasons.append("venv_cfg_unproven")
    elif proc.exe is None or os.path.realpath(proc.exe) != os.path.realpath(executable):
        reasons.append("exe_mismatch")
    if "PYTHONHOME" in proc.env or "PYTHONPLATLIBDIR" in proc.env:
        reasons.append("python_home_set")

    sites = sorted((venv / "lib").glob("python*/site-packages"))
    imports: set[str] = {"genesis", *_CUSTOMIZE}
    if len(sites) != 1:
        reasons.append("site_packages_ambiguous")
    else:
        site = sites[0]
        main_src = os.path.realpath(main_root / "src")
        try:
            names = sorted(
                n for n in os.listdir(site) if n.endswith(".pth") and not n.startswith(".")
            )
        except OSError:
            names = None
            reasons.append("site_packages_ambiguous")
        for name in names or []:
            if not _pth_verdict(site, name, main_src, imports):
                reasons.append(f"unknown_pth:{name}")
        # A path-line .pth puts main src AFTER site-packages on sys.path (and a
        # finder install runs after PathFinder), so a genesis there wins.
        if _shadows(site, {"genesis"}):
            reasons.append("site_packages_shadow")
        if _shadows(site, set(_CUSTOMIZE)):
            reasons.append("customize_module")

    # The system sitecustomize ``site`` imports (Debian's runs an apport hook):
    # whatever it imports by name could be shadowed through PYTHONPATH.
    home, version = cfg.get("home"), cfg.get("version", "")
    major_minor = ".".join(version.split(".")[:2])
    if home and major_minor:
        stdlib_custom = Path(home).parent / "lib" / f"python{major_minor}" / "sitecustomize.py"
        if stdlib_custom.exists():
            try:
                tree = ast.parse(stdlib_custom.read_text())
            except (OSError, SyntaxError, ValueError):
                reasons.append("customize_unproven")
            else:
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        imports.update(a.name.split(".")[0] for a in node.names)
                    elif isinstance(node, ast.ImportFrom) and node.module:
                        imports.add(node.module.split(".")[0])

    # A PYTHONPATH entry precedes the stdlib and site-packages for EVERY import,
    # not just the startup's: the server's own loader (dotenv, argparse, ...)
    # could be replaced. So it may hold no stdlib or installed top-level name.
    # Left: an optional import of a package that is not installed.
    reachable = set(imports) | set(sys.stdlib_module_names)
    if len(sites) == 1:
        # Unlistable is already "site_packages_ambiguous" above, so the row is dirty.
        with contextlib.suppress(OSError):
            reachable.update(n.split(".", 1)[0] for n in os.listdir(sites[0]))
    raw_path = proc.env.get("PYTHONPATH")
    if raw_path is not None:
        for entry in raw_path.split(os.pathsep):
            path = Path(entry)
            try:
                ok = (
                    entry != ""  # an empty entry is the cwd at start, unknowable now
                    and path.is_absolute()
                    and path.is_dir()
                    and _unchanged_since(path, proc.start)
                    and not _shadows(path, reachable)
                )
            except OSError:
                ok = False
            if not ok:
                reasons.append("pythonpath_unproven")
                break

    entry = _script(proc.argv)
    if entry is None or not os.path.isabs(entry):
        reasons.append("script_not_absolute")
    else:
        if script is not None and os.path.realpath(entry) != os.path.realpath(script):
            reasons.append("script_not_main")
        # sys.path[0] precedes everything else on the path. The script is now
        # proven to be main's own, so its directory is main's code; only names
        # that would replace the startup's imports matter here. (site imports
        # the customize modules before it is added, so those err safe.)
        if _shadows(Path(os.path.dirname(os.path.realpath(entry))), imports):
            reasons.append("script_dir_shadow")
    return reasons


def secrets_file(env: dict[str, str], *, main_root: Path) -> Path | None:
    """The secrets file that process loads, chosen as ``genesis.env.secrets_path``
    chooses it: ``SECRETS_PATH``, else ``GENESIS_REPO_ROOT``'s, else main's."""
    if env.get("SECRETS_PATH"):
        return expand_home(env["SECRETS_PATH"], env)
    if env.get("GENESIS_REPO_ROOT"):
        root = expand_home(env["GENESIS_REPO_ROOT"], env)
        return None if root is None else root / "secrets.env"
    return main_root / "secrets.env"


def _unchanged_since(path: Path, start: float) -> bool:
    """Neither ``path`` nor any symlink on the way to it changed after ``start``."""
    probe = Path(path.anchor)
    for part in path.parts[1:]:
        probe = probe / part
        if probe.is_symlink() and probe.lstat().st_ctime >= start:
            return False
    return path.stat().st_ctime < start


def secrets_values(path: Path | None, start: float) -> dict[str, str] | None:
    """The file's values as the process loaded them, or None when that cannot be
    proven: missing, unreadable, or changed (it or a symlink on its path) since
    the process started."""
    if path is None:
        return None
    try:
        if not _unchanged_since(path, start):
            return None
        from dotenv import dotenv_values

        # Raw: interpolating ${VAR} here would use the census's environment, not
        # that process's. A value that needs it reads as unknown below.
        return {k: v for k, v in dotenv_values(path, interpolate=False).items() if v is not None}
    except Exception:
        return None


def switch_off(env: dict[str, str], file_vals: dict[str, str] | None) -> bool | None:
    """The kill switch a memory server actually runs with, or None.

    Two loads decide it. At startup the MCP loader copies a file value only for a
    key not already set (``scripts/genesis_mcp_server.py``); then the memory
    lifespan's router reloads the same file with ``override=True``
    (``routing/standalone.py``), so a key the file carries wins from there on, and
    telemetry reads the switch on every call. So: unprovable file -> unknown;
    the two phases disagree -> unknown; else their common value."""
    if file_vals is None:
        return None
    file_value = file_vals.get(_SWITCH)
    if file_value is not None and "${" in file_value:
        return None
    at_start = env.get(_SWITCH, file_value or None)
    running = file_value if file_value is not None else env.get(_SWITCH)
    start_off, run_off = at_start == "1", running == "1"
    return run_off if start_off == run_off else None


def db_paths(
    env: dict[str, str], file_vals: dict[str, str] | None, *, main_root: Path
) -> list[Path | None]:
    """Every database a memory server may use, its primary first; None for one
    that cannot be resolved from here.

    The primary connection's path (``_DEFAULT_DB``) is fixed when the server
    script is imported, BEFORE any secrets load: the exec-time
    ``GENESIS_DB_PATH``, else ``<root>/data/genesis.db``. The lifespan's router
    later reloads the file with ``override=True``, so a ``GENESIS_DB_PATH`` the
    file carries becomes what any later ``genesis_db_path()`` call resolves: a
    second database. An unprovable file leaves that second one unknown.

    One resolver serves both, mirroring ``genesis.env.genesis_db_path`` and
    ``repo_root`` over the environment each phase sees: the launch environment,
    then that environment with every key the file sets laid over it (the reload
    sets empty values too, and a file may move ``GENESIS_REPO_ROOT`` or ``HOME``)."""
    primary = _resolve_db(env, main_root=main_root)
    if file_vals is None:
        return [primary, None]
    after_reload = _resolve_db({**env, **file_vals}, main_root=main_root)
    return [primary] if after_reload == primary else [primary, after_reload]


def _resolve_db(env: dict[str, str], *, main_root: Path) -> Path | None:
    """``genesis_db_path()`` as a process with ``env`` resolves it, or None."""
    if any("${" in env.get(k, "") for k in ("GENESIS_DB_PATH", "GENESIS_REPO_ROOT", "HOME")):
        return None
    value = env.get("GENESIS_DB_PATH")
    if value:
        return expand_home(value, env)
    if env.get("GENESIS_REPO_ROOT"):
        root = expand_home(env["GENESIS_REPO_ROOT"], env)
        return None if root is None else root / "data" / "genesis.db"
    return main_root / "data" / "genesis.db"


def db_scope(paths: list[Path | None], live: Path) -> tuple[bool, list[int] | None]:
    """``(foreign, [st_dev, st_ino] of the primary)``. Foreign only when EVERY
    database the process may use stats and differs from the live one; anything
    unknown or missing counts as the live database."""
    try:
        ours = live.stat()
    except OSError:
        return False, None
    idents: list[list[int] | None] = []
    for path in paths:
        try:
            st = path.stat() if path is not None else None
        except OSError:
            st = None
        idents.append(None if st is None else [st.st_dev, st.st_ino])
    live_ident = [ours.st_dev, ours.st_ino]
    foreign = bool(idents) and all(i is not None and i != live_ident for i in idents)
    return foreign, idents[0] if idents else None

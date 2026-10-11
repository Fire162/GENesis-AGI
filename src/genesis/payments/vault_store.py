"""The encrypted card vault on disk.

Layout of ``~/.genesis/payment_vault/`` (directory 0700, every file 0600):

- ``recipient.txt``: the vault's age X25519 public key. Adding a card needs
  only this, so ``vault add`` never asks for the passphrase.
- ``identity.age``: the matching secret key, encrypted under the owner's
  passphrase (age scrypt). Its presence marks the vault initialised, so
  ``init`` writes it last and never over an existing one.
- ``cards.db`` (``user_version`` 1): one row per card. The number, expiry and
  security code exist only inside ``blob``, age-encrypted to
  ``recipient.txt``; the row keeps a label, brand, last four digits and the
  merchant scope in the clear, for ``list``. Retiring a card clears its blob
  with ``secure_delete`` on, so the ciphertext does not stay in a free page
  of the file. That is best effort: SQLite's rollback journal and the
  filesystem may keep old ciphertext, so a retired virtual card is also
  closed with its issuer.

Only virtual cards go in: every card is scoped to one merchant, or marked
single-use, or both. Decryption of cards is not here; the vault service that
unlocks the identity and fills a card comes later.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import secrets
import sqlite3
import stat
import unicodedata
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path

from genesis.env import genesis_home
from genesis.security.card_shapes import card_brand, contains_card_number

IDENTITY = "identity.age"
RECIPIENT = "recipient.txt"
CARDS = "cards.db"
SCHEMA_VERSION = 1
BLOB_VERSION = 1

# zxcvbn's top score: "very unguessable" (guesses >= 10^10).
MIN_PASSPHRASE_SCORE = 4
# zxcvbn refuses input over 72 characters by default; longer passphrases are
# scored whole. Its cost grows faster than linearly: MEASURED on a repetitive
# phrase, 0.9 s at 256 characters and 127 s at 1024. 256 is past any
# diceware passphrase (20 words is about 140).
MAX_PASSPHRASE_LENGTH = 256
MAX_LABEL_LENGTH = 100

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cards (
    card_ref    TEXT PRIMARY KEY,
    label       TEXT NOT NULL,
    brand       TEXT NOT NULL,
    last4       TEXT NOT NULL,
    merchant    TEXT,
    single_use  INTEGER NOT NULL CHECK (single_use IN (0, 1)),
    status      TEXT NOT NULL CHECK (status IN ('active', 'retired')),
    created_at  TEXT NOT NULL,
    retired_at  TEXT,
    blob        BLOB,
    CHECK (merchant IS NOT NULL OR single_use = 1),
    CHECK ((status = 'active') = (blob IS NOT NULL))
)
"""

_EXPIRY = re.compile(r"(0[1-9]|1[0-2])\s*/\s*([0-9]{2}|[0-9]{4})")
_HOST = re.compile(
    r"(?=.{1,253}\Z)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+([a-z]{2,63}|xn--[a-z0-9-]{1,59})"
)
CARD_REF = re.compile(r"card_[0-9a-f]{12}")


class VaultError(Exception):
    """A vault operation was refused. The message never carries a card value."""


def vault_dir() -> Path:
    return genesis_home() / "payment_vault"


def passphrase_score(passphrase: str) -> int:
    from zxcvbn import zxcvbn

    if len(passphrase) > MAX_PASSPHRASE_LENGTH:
        raise VaultError(f"the passphrase is longer than {MAX_PASSPHRASE_LENGTH} characters")
    return int(zxcvbn(passphrase, max_length=max(72, len(passphrase)))["score"])


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _ensure_dir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise VaultError(f"{path} is not a directory you own")
    if stat.S_IMODE(info.st_mode) != 0o700:
        os.chmod(path, 0o700)


def _write_new(path: Path, data: bytes) -> None:
    """Write ``data`` to a file that must not exist yet, created 0600."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@contextmanager
def _dir_lock(path: Path):
    """Hold an exclusive lock on the vault directory (one init at a time)."""
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _connect(path: Path) -> sqlite3.Connection:
    db = path / CARDS
    try:
        info = os.lstat(db)
    except FileNotFoundError:
        info = None
    except OSError as exc:
        raise VaultError(f"cards.db cannot be checked ({exc.strerror})") from None
    if info is not None and not stat.S_ISREG(info.st_mode):
        raise VaultError("cards.db is not a regular file (a symlink or other); refusing to open it")
    if info is None:
        try:
            os.close(os.open(db, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600))
        except OSError as exc:
            raise VaultError(f"cards.db cannot be created ({exc.strerror})") from None
    conn = sqlite3.connect(db)
    try:
        conn.execute("PRAGMA secure_delete = ON")
        conn.execute(_SCHEMA)
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version == 0:
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            conn.commit()
        elif version != SCHEMA_VERSION:
            raise VaultError(
                f"cards.db has format version {version}; this Genesis reads {SCHEMA_VERSION}"
            )
    except sqlite3.OperationalError as exc:  # locked, disk I/O: likely transient
        conn.close()
        raise VaultError(f"cards.db could not be opened ({exc})") from None
    except sqlite3.DatabaseError:
        conn.close()
        raise VaultError("cards.db is unreadable (not a database, or damaged)") from None
    except VaultError:
        conn.close()
        raise
    return conn


def _card_count(path: Path) -> int:
    if not os.path.lexists(path / CARDS):
        return 0
    with closing(_connect(path)) as conn:
        return conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0]


def is_initialised(path: Path | None = None) -> bool:
    path = path or vault_dir()
    return (path / IDENTITY).is_file() and (path / RECIPIENT).is_file()


def check_can_init(path: Path | None = None) -> None:
    """Refuse init before anything is asked for or written.

    Refuses when ``identity.age`` exists in any form (file, directory,
    symlink), and when ``cards.db`` already holds cards: a new key would
    leave them sealed to the old one and unreadable.
    """
    path = path or vault_dir()
    if os.path.lexists(path / IDENTITY):
        if is_initialised(path):
            raise VaultError("the vault is already initialised")
        raise VaultError(
            "the vault is incomplete (identity.age is there but recipient.txt is missing or not "
            "a file); restore it from backup or move the vault directory aside"
        )
    count = _card_count(path) if path.is_dir() else 0
    if count:
        raise VaultError(
            f"cards.db holds {count} card(s) sealed to an earlier key; move the vault directory aside first"
        )


def init_vault(passphrase: str, path: Path | None = None) -> str:
    """Create the vault; return the recipient (public key).

    Refuses as ``check_can_init`` does, and a passphrase below
    MIN_PASSPHRASE_SCORE. A run killed midway leaves no ``identity.age`` and
    no cards, so the vault still reads as uninitialised and a rerun starts
    over. Concurrent inits are serialised by a directory lock, and the
    identity is linked into place, so one can never replace another's.
    """
    from pyrage import passphrase as age_passphrase
    from pyrage import x25519

    path = path or vault_dir()
    check_can_init(path)
    if passphrase_score(passphrase) < MIN_PASSPHRASE_SCORE:
        raise VaultError("the passphrase is too guessable (needs zxcvbn score 4)")
    _ensure_dir(path)
    with _dir_lock(path):
        check_can_init(path)  # another init may have finished while we waited
        identity = x25519.Identity.generate()
        recipient = str(identity.to_public())
        sealed = age_passphrase.encrypt(str(identity).encode(), passphrase)
        staged = {RECIPIENT: recipient.encode() + b"\n", IDENTITY: sealed}
        for name in staged:  # leftovers of a killed run hold nothing secret
            (path / f".{name}.tmp").unlink(missing_ok=True)
        for name, data in staged.items():
            _write_new(path / f".{name}.tmp", data)
        with closing(_connect(path)):
            pass
        os.replace(path / f".{RECIPIENT}.tmp", path / RECIPIENT)
        # identity.age last, and linked rather than renamed: link fails if
        # the target exists, so an identity is never overwritten.
        os.link(path / f".{IDENTITY}.tmp", path / IDENTITY)
        (path / f".{IDENTITY}.tmp").unlink()
        _fsync_dir(path)
    return recipient


def parse_card(number: str, expiry: str, cvc: str) -> dict:
    """Validate and normalise card entry. Errors never echo the input."""
    digits = re.sub(r"[ \-  ]", "", number)
    brand = card_brand(digits) if digits.isascii() and digits.isdigit() else None
    if brand is None:
        raise VaultError("that is not a valid card number (checksum, brand or length)")
    m = _EXPIRY.fullmatch(expiry.strip())
    if not m:
        raise VaultError("the expiry must be MM/YY or MM/YYYY")
    month, year = int(m.group(1)), int(m.group(2))
    year += 2000 if year < 100 else 0
    now = datetime.now(UTC)
    if (year, month) < (now.year, now.month):
        raise VaultError("that card has expired")
    code = cvc.strip()
    if not (code.isascii() and code.isdigit() and len(code) == (4 if brand == "amex" else 3)):
        raise VaultError("the security code must be 3 digits (4 for Amex)")
    return {"number": digits, "exp_month": month, "exp_year": year, "cvc": code, "brand": brand}


def normalise_merchant(merchant: str) -> str:
    host = merchant.strip().lower().rstrip(".")
    if not _HOST.fullmatch(host):
        raise VaultError("the merchant scope must be a bare host name, like shop.example.com")
    return host


def check_entry(label: str, merchant: str | None, single_use: bool) -> str | None:
    """Check a card's clear fields before any secret is asked for; return the
    normalised merchant. Virtual cards only: a merchant scope, single-use, or both."""
    if merchant is None and not single_use:
        raise VaultError("a card needs a merchant scope (--merchant) or --single-use")
    if (
        not label.strip()
        or len(label) > MAX_LABEL_LENGTH
        or any(unicodedata.category(ch).startswith("C") for ch in label)
    ):
        raise VaultError(f"the label must be 1-{MAX_LABEL_LENGTH} printable characters")
    # Both are stored in the clear and printed by `list`.
    if contains_card_number(label) or (merchant and contains_card_number(merchant)):
        raise VaultError("the label or merchant looks like it holds a card number")
    return normalise_merchant(merchant) if merchant is not None else None


def check_ready(path: Path | None = None) -> None:
    """Refuse to add to a vault that is not initialised or not private."""
    path = path or vault_dir()
    if not is_initialised(path):
        if os.path.lexists(path / IDENTITY):
            check_can_init(path)  # raises the "incomplete" message
        raise VaultError("the vault is not initialised; run `python -m genesis vault init`")
    loose = [name for name, info in vault_status(path)["files"].items() if not info["private"]]
    if loose:
        raise VaultError(f"not private: {', '.join(loose)}; fix the modes (see `vault status`)")


def add_card(
    card: dict,
    *,
    label: str,
    merchant: str | None,
    single_use: bool,
    name: str = "",
    postal_code: str = "",
    path: Path | None = None,
) -> str:
    """Encrypt ``card`` (from ``parse_card``) to the vault's public key; return its ref."""
    from pyrage import RecipientError, encrypt, x25519

    path = path or vault_dir()
    check_ready(path)
    merchant = check_entry(label, merchant, single_use)
    try:
        recipient = x25519.Recipient.from_str((path / RECIPIENT).read_text().strip())
    except RecipientError:
        raise VaultError("recipient.txt does not hold a valid age public key") from None
    secret = {"v": BLOB_VERSION}
    secret.update({k: card[k] for k in ("number", "exp_month", "exp_year", "cvc")})
    secret.update(name=name, postal_code=postal_code)
    blob = encrypt(json.dumps(secret).encode(), [recipient])
    ref = "card_" + secrets.token_hex(6)
    with closing(_connect(path)) as conn, conn:
        conn.execute(
            "INSERT INTO cards (card_ref, label, brand, last4, merchant, single_use, status,"
            " created_at, blob) VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?)",
            (
                ref,
                label,
                card["brand"],
                card["number"][-4:],
                merchant,
                int(single_use),
                _now(),
                blob,
            ),
        )
    return ref


def list_cards(path: Path | None = None) -> list[dict]:
    """Every card's clear columns (never the blob, never the expiry)."""
    path = path or vault_dir()
    if not os.path.lexists(path / CARDS):
        return []
    with closing(_connect(path)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT card_ref, label, brand, last4, merchant, single_use, status, created_at,"
            " retired_at FROM cards ORDER BY created_at, card_ref"
        ).fetchall()
    return [dict(r) for r in rows]


def check_active_card(card_ref: str, path: Path | None = None) -> None:
    """Refuse a malformed or unknown ref, without echoing what was typed."""
    path = path or vault_dir()
    if not CARD_REF.fullmatch(card_ref):
        raise VaultError(
            "that is not a card ref (card_ followed by 12 hex digits; see `vault list`)"
        )
    if not any(c["card_ref"] == card_ref and c["status"] == "active" for c in list_cards(path)):
        raise VaultError(f"no active card {card_ref}")


def verify_passphrase(passphrase: str, path: Path | None = None) -> None:
    from pyrage import DecryptError
    from pyrage import passphrase as age_passphrase

    path = path or vault_dir()
    if not is_initialised(path):
        raise VaultError("the vault is not initialised")
    try:
        age_passphrase.decrypt((path / IDENTITY).read_bytes(), passphrase)
    except DecryptError:
        raise VaultError("wrong passphrase (or identity.age is damaged)") from None


def retire_card(card_ref: str, passphrase: str, path: Path | None = None) -> None:
    """Mark a card retired and clear its encrypted blob. Needs the passphrase."""
    path = path or vault_dir()
    check_ready(path)
    check_active_card(card_ref, path)
    verify_passphrase(passphrase, path)
    with closing(_connect(path)) as conn, conn:
        cur = conn.execute(
            "UPDATE cards SET status = 'retired', retired_at = ?, blob = NULL"
            " WHERE card_ref = ? AND status = 'active'",
            (_now(), card_ref),
        )
        if cur.rowcount != 1:
            raise VaultError(f"no active card {card_ref}")


def vault_status(path: Path | None = None) -> dict:
    """Whether the vault exists, its card counts, and whether its modes are private.

    ``error`` is set (and ``cards`` is None) when cards.db cannot be read.
    """
    path = path or vault_dir()
    files = {}
    for name in ("", IDENTITY, RECIPIENT, CARDS):
        target = path / name
        try:
            mode = stat.S_IMODE(os.lstat(target).st_mode)
        except FileNotFoundError:
            continue
        files[name or "."] = {"mode": oct(mode), "private": not mode & 0o077}
    counts: dict | None = {"active": 0, "retired": 0}
    error = None
    try:
        for card in list_cards(path):
            counts[card["status"]] += 1
    except VaultError as exc:
        counts, error = None, str(exc)
    return {
        "path": str(path),
        "initialised": is_initialised(path),
        "cards": counts,
        "files": files,
        "error": error,
    }

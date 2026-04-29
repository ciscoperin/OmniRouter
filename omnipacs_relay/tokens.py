"""Bearer-token store for the OmniPACS Relay.

A token is opaque, cryptographically random (32 bytes URL-safe). We store
each token under a short human-readable label so the dashboard can show
"who's using which token" without revealing the secret.

Persisted to ``<spool>/tokens.json`` with mode 0600. The plaintext token
is kept on disk because we need to validate inbound headers in constant
time — there's no out-of-band way to recover it. At-rest encryption is
explicitly out of scope for v1 (filesystem permissions only).

Tokens may also be seeded at startup from an env var (``OMNI_RELAY_TOKENS``,
comma- or whitespace-separated) so a Docker run can ship with a known
bootstrap token.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from .config import SPOOL_DIR

log = logging.getLogger("omnipacs_relay.tokens")


# Length constraints — tokens are URL-safe random bytes; 32 bytes →
# ~43 base64 chars. Anything obviously off is rejected fast.
MIN_TOKEN_LEN = 16
MAX_TOKEN_LEN = 4096

# Label constraints — short, ASCII, no spaces, fits in a UI badge.
LABEL_MAX_LEN = 32


@dataclass
class TokenRecord:
    label: str
    token: str
    created_ts: float
    last_used_ts: float | None = None

    def public_view(self) -> dict:
        """Public dict that NEVER includes the token itself."""
        return {
            "label": self.label,
            "created_ts": self.created_ts,
            "last_used_ts": self.last_used_ts,
        }


class TokenStore:
    """Thread-safe store of bearer tokens with JSON persistence."""

    def __init__(self, path: Path | None = None) -> None:
        self._lock = threading.RLock()
        # Resolution order: explicit arg → OMNI_RELAY_TOKEN_FILE env →
        # default <spool>/tokens.json. The env knob lets operators put
        # the token store on a separate, more tightly-permissioned
        # volume from the PHI spool.
        if path is not None:
            self._path = path
        else:
            env_path = os.environ.get("OMNI_RELAY_TOKEN_FILE", "").strip()
            self._path = Path(env_path) if env_path else (SPOOL_DIR / "tokens.json")
        # token-string -> TokenRecord. Looked up on the hot path.
        self._by_token: dict[str, TokenRecord] = {}
        # label -> TokenRecord. Looked up by the admin UI.
        self._by_label: dict[str, TokenRecord] = {}
        self._load_from_disk()
        self._seed_from_env()

    # -------- persistence ------------------------------------------------
    def _load_from_disk(self) -> None:
        try:
            if not self._path.exists():
                return
            data = json.loads(self._path.read_text())
            entries = data.get("tokens", []) if isinstance(data, dict) else []
            for raw in entries:
                if not isinstance(raw, dict):
                    continue
                label = str(raw.get("label", "")).strip()
                token = str(raw.get("token", ""))
                if not label or not token:
                    continue
                rec = TokenRecord(
                    label=label,
                    token=token,
                    created_ts=float(raw.get("created_ts") or time.time()),
                    last_used_ts=raw.get("last_used_ts"),
                )
                self._by_token[token] = rec
                self._by_label[label] = rec
            if self._by_token:
                log.info("Loaded %d bearer token(s) from %s", len(self._by_token), self._path)
        except Exception:
            log.exception("Could not load tokens from %s", self._path)

    def _save_to_disk(self) -> None:
        try:
            # When OMNI_RELAY_TOKEN_FILE points outside SPOOL_DIR we need
            # to make sure that target's parent exists too.
            self._path.parent.mkdir(parents=True, exist_ok=True)
            SPOOL_DIR.mkdir(parents=True, exist_ok=True)
            payload = {
                "tokens": [
                    {
                        "label": rec.label,
                        "token": rec.token,
                        "created_ts": rec.created_ts,
                        "last_used_ts": rec.last_used_ts,
                    }
                    for rec in self._by_label.values()
                ]
            }
            tmp = self._path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, indent=2))
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass
            tmp.replace(self._path)
            try:
                os.chmod(self._path, 0o600)
            except OSError:
                pass
        except Exception:
            log.exception("Could not persist tokens to %s", self._path)

    # -------- env seeding ------------------------------------------------
    def _seed_from_env(self) -> None:
        """Honour OMNI_RELAY_TOKENS and OMNI_RELAY_TOKEN for bootstrap."""
        raw = os.environ.get("OMNI_RELAY_TOKENS") or os.environ.get(
            "OMNI_RELAY_TOKEN"
        )
        if not raw:
            return
        seeded = 0
        for piece in raw.replace(",", " ").split():
            tok = piece.strip()
            if len(tok) < MIN_TOKEN_LEN or len(tok) > MAX_TOKEN_LEN:
                continue
            if tok in self._by_token:
                continue
            label = f"env-{seeded + 1}"
            # Avoid collision with already-loaded labels
            n = seeded + 1
            while label in self._by_label:
                n += 1
                label = f"env-{n}"
            rec = TokenRecord(
                label=label,
                token=tok,
                created_ts=time.time(),
                last_used_ts=None,
            )
            self._by_token[tok] = rec
            self._by_label[label] = rec
            seeded += 1
        if seeded:
            log.info("Seeded %d bearer token(s) from environment", seeded)
            self._save_to_disk()

    # -------- public API -------------------------------------------------
    def issue(self, label: str | None = None) -> tuple[str, TokenRecord]:
        """Generate a new random token and persist it. Returns (raw_token, record).

        The raw token is shown to the operator exactly once on creation;
        afterwards only the public view (label + timestamps) is exposed.
        """
        with self._lock:
            label = self._sanitize_label(label)
            tok = secrets.token_urlsafe(32)
            rec = TokenRecord(
                label=label,
                token=tok,
                created_ts=time.time(),
                last_used_ts=None,
            )
            self._by_token[tok] = rec
            self._by_label[label] = rec
            self._save_to_disk()
            log.info("Issued new bearer token (label=%s)", label)
            return tok, rec

    def revoke(self, label: str) -> bool:
        with self._lock:
            rec = self._by_label.pop(label, None)
            if rec is None:
                return False
            self._by_token.pop(rec.token, None)
            self._save_to_disk()
            log.info("Revoked bearer token (label=%s)", label)
            return True

    def validate(self, presented: str) -> TokenRecord | None:
        """Look up a presented token in constant time per registered token.

        Returns the matching record (and bumps its last_used_ts) or None.
        """
        with self._lock:
            # Constant-time-ish: compare against every registered token so
            # attackers can't time-distinguish "no such token" from
            # "wrong token". For very large token sets this would be O(N)
            # but the relay's token set is tiny (one per remote site).
            matched: TokenRecord | None = None
            for tok, rec in self._by_token.items():
                if hmac.compare_digest(tok, presented):
                    matched = rec
            if matched is not None:
                matched.last_used_ts = time.time()
                # Persist last-used so the dashboard survives restarts.
                self._save_to_disk()
            return matched

    def list_public(self) -> list[dict]:
        """Public summary for the dashboard — never includes raw tokens."""
        with self._lock:
            return [rec.public_view() for rec in self._by_label.values()]

    def count(self) -> int:
        with self._lock:
            return len(self._by_label)

    # -------- internals --------------------------------------------------
    def _sanitize_label(self, label: str | None) -> str:
        if label is None:
            label = ""
        label = label.strip()
        if not label:
            label = self._auto_label()
        if len(label) > LABEL_MAX_LEN:
            raise ValueError(f"label must be ≤ {LABEL_MAX_LEN} chars")
        # Permit a sane ASCII subset to keep the UI tidy.
        for ch in label:
            if not (ch.isalnum() or ch in "-_."):
                raise ValueError(
                    "label may only contain letters, digits, '-', '_', '.'"
                )
        if label in self._by_label:
            raise ValueError(f"label {label!r} is already in use")
        return label

    def _auto_label(self) -> str:
        n = 1
        while f"site-{n}" in self._by_label:
            n += 1
        return f"site-{n}"


def labels_of(records: Iterable[TokenRecord]) -> list[str]:
    return [r.label for r in records]


# Module-level singleton used by the web layer.
token_store = TokenStore()

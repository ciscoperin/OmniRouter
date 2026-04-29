"""Durable on-disk spool.

Each accepted DICOM instance is written with the pattern::

    <inbox>/<StudyInstanceUID>/<SOPInstanceUID>.dcm

We always write to a ``.tmp`` sibling first, fsync it, then atomically
rename into place. Only after the rename succeeds does the STOW handler
return an ack. This guarantees no acked instance is ever lost across an
unclean restart.

Quarantine — instances that failed to forward MAX_RETRIES times — moves
the file under ``<quarantine>/<StudyInstanceUID>/<SOPInstanceUID>.dcm``
and writes a sibling ``.error`` text file with the last error.

The spool is intentionally filesystem-backed (no DB) so an operator can
inspect it with ``ls``/``find`` and so it works on any Linux box without
provisioning.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from pydicom import dcmread
from pydicom.dataset import Dataset

from .config import INBOX_DIR, QUARANTINE_DIR, SPOOL_DIR

log = logging.getLogger("omnipacs_relay.spool")


@dataclass(frozen=True)
class SpoolEntry:
    """One spooled instance ready to be forwarded."""

    study_uid: str
    sop_uid: str
    path: Path

    def read_dataset(self) -> Dataset:
        return dcmread(str(self.path))


class Spool:
    """Filesystem spool for accepted instances."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stats = {
            "received": 0,
            "forwarded": 0,
            "failed": 0,
            "quarantined": 0,
        }
        self._last_forward_ts: float | None = None
        self._ensure_dirs()

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------
    def _ensure_dirs(self) -> None:
        SPOOL_DIR.mkdir(parents=True, exist_ok=True)
        INBOX_DIR.mkdir(parents=True, exist_ok=True)
        QUARANTINE_DIR.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(SPOOL_DIR, 0o700)
        except OSError:
            pass

    # ------------------------------------------------------------------
    # Write path (called from STOW handler)
    # ------------------------------------------------------------------
    def write_instance(
        self,
        study_uid: str,
        sop_uid: str,
        dicom_bytes: bytes,
    ) -> Path:
        """Write one DICOM instance atomically and durably.

        Returns the final path. Raises OSError on filesystem failure —
        caller should treat that as a STOW failure for this instance.
        """
        if not study_uid or not sop_uid:
            raise ValueError("study_uid and sop_uid are required")
        # Prevent path traversal — UIDs should only ever be dotted
        # numbers per PS3.5, but defend in depth anyway.
        safe_study = _safe_uid(study_uid)
        safe_sop = _safe_uid(sop_uid)

        study_dir = INBOX_DIR / safe_study
        study_dir.mkdir(parents=True, exist_ok=True)
        final = study_dir / f"{safe_sop}.dcm"
        tmp = study_dir / f"{safe_sop}.dcm.tmp.{os.getpid()}.{threading.get_ident()}"

        # Write + fsync the file, then fsync the directory entry, then rename.
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, dicom_bytes)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, final)
        # Best-effort fsync on the parent directory so the rename is durable.
        try:
            dir_fd = os.open(str(study_dir), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass

        with self._lock:
            self._stats["received"] += 1

        return final

    # ------------------------------------------------------------------
    # Read path (called from forwarder worker)
    # ------------------------------------------------------------------
    def iter_pending(self) -> Iterator[SpoolEntry]:
        """Yield every spooled instance currently in the inbox.

        Each call walks the disk fresh so newly-written files show up on
        the next pass. Files removed by the caller (after successful
        forward) simply don't appear next time.
        """
        if not INBOX_DIR.exists():
            return
        for study_dir in sorted(INBOX_DIR.iterdir()):
            if not study_dir.is_dir():
                continue
            for f in sorted(study_dir.glob("*.dcm")):
                # Skip in-flight tempfiles defensively.
                if f.name.endswith(".tmp"):
                    continue
                sop = f.stem
                yield SpoolEntry(
                    study_uid=study_dir.name,
                    sop_uid=sop,
                    path=f,
                )

    # ------------------------------------------------------------------
    # Outcome reporting (called from forwarder worker)
    # ------------------------------------------------------------------
    def mark_forwarded(self, entry: SpoolEntry) -> None:
        try:
            entry.path.unlink(missing_ok=True)
            # Best-effort prune of the now-empty study dir.
            try:
                entry.path.parent.rmdir()
            except OSError:
                pass
        except OSError:
            log.exception("Failed to remove spool file %s", entry.path)
        with self._lock:
            self._stats["forwarded"] += 1
            self._last_forward_ts = time.time()

    def mark_failed(self) -> None:
        with self._lock:
            self._stats["failed"] += 1

    def discard_pending(self, entry: SpoolEntry) -> None:
        """Drop a pending instance from the inbox without quarantining.

        Used by sync STOW mode after a fail-fast failure: the caller
        (OmniRouter) will retry the request itself, so we mustn't keep
        retrying asynchronously and create duplicate deliveries.
        """
        try:
            entry.path.unlink(missing_ok=True)
            try:
                entry.path.parent.rmdir()
            except OSError:
                pass
        except OSError:
            log.exception("Failed to discard spool file %s", entry.path)

    def quarantine(self, entry: SpoolEntry, reason: str) -> Path:
        """Move an instance from inbox/ to quarantine/ and record reason."""
        safe_study = _safe_uid(entry.study_uid)
        safe_sop = _safe_uid(entry.sop_uid)
        dest_dir = QUARANTINE_DIR / safe_study
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"{safe_sop}.dcm"
        try:
            os.replace(entry.path, dest)
        except OSError:
            log.exception("Could not move %s to quarantine", entry.path)
            return entry.path
        # Record the reason alongside the file.
        try:
            (dest_dir / f"{safe_sop}.error").write_text(
                f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}  {reason}\n"
            )
        except OSError:
            pass
        # Prune now-empty inbox study dir.
        try:
            entry.path.parent.rmdir()
        except OSError:
            pass
        with self._lock:
            self._stats["quarantined"] += 1
        log.warning(
            "Quarantined study=%s sop=%s reason=%s",
            entry.study_uid, entry.sop_uid, reason,
        )
        return dest

    # ------------------------------------------------------------------
    # Quarantine management (called from admin UI)
    # ------------------------------------------------------------------
    def list_quarantine(self) -> list[dict]:
        out: list[dict] = []
        if not QUARANTINE_DIR.exists():
            return out
        for study_dir in sorted(QUARANTINE_DIR.iterdir()):
            if not study_dir.is_dir():
                continue
            for f in sorted(study_dir.glob("*.dcm")):
                err_path = study_dir / f"{f.stem}.error"
                err = ""
                if err_path.exists():
                    try:
                        err = err_path.read_text().strip().splitlines()[-1]
                    except OSError:
                        pass
                out.append({
                    "study_uid": study_dir.name,
                    "sop_uid": f.stem,
                    "size_bytes": f.stat().st_size,
                    "quarantined_ts": f.stat().st_mtime,
                    "last_error": err,
                })
        return out

    def requeue_quarantine(self, study_uid: str, sop_uid: str) -> bool:
        """Move one quarantined instance back into the inbox for retry."""
        safe_study = _safe_uid(study_uid)
        safe_sop = _safe_uid(sop_uid)
        src = QUARANTINE_DIR / safe_study / f"{safe_sop}.dcm"
        if not src.exists():
            return False
        dest_dir = INBOX_DIR / safe_study
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"{safe_sop}.dcm"
        try:
            os.replace(src, dest)
        except OSError:
            log.exception("Could not requeue %s/%s", study_uid, sop_uid)
            return False
        # Remove the .error sidecar.
        try:
            (QUARANTINE_DIR / safe_study / f"{safe_sop}.error").unlink(missing_ok=True)
        except OSError:
            pass
        # Best-effort prune of the empty quarantine dir.
        try:
            (QUARANTINE_DIR / safe_study).rmdir()
        except OSError:
            pass
        with self._lock:
            # Move it back from "quarantined" bucket so the dashboard
            # number reflects the requeue.
            if self._stats["quarantined"] > 0:
                self._stats["quarantined"] -= 1
        log.info("Requeued from quarantine: study=%s sop=%s", study_uid, sop_uid)
        return True

    def requeue_all_quarantine(self) -> int:
        """Requeue every quarantined instance. Returns count moved."""
        count = 0
        for item in self.list_quarantine():
            if self.requeue_quarantine(item["study_uid"], item["sop_uid"]):
                count += 1
        return count

    # ------------------------------------------------------------------
    # Stats / introspection (called from admin UI)
    # ------------------------------------------------------------------
    def queue_depth(self) -> int:
        """Cheap count of inbox files. Walks dirs but doesn't read content."""
        if not INBOX_DIR.exists():
            return 0
        n = 0
        for study_dir in INBOX_DIR.iterdir():
            if study_dir.is_dir():
                for f in study_dir.iterdir():
                    if f.suffix == ".dcm":
                        n += 1
        return n

    def stats(self) -> dict:
        with self._lock:
            return {
                **self._stats,
                "queue_depth": self.queue_depth(),
                "last_forward_ts": self._last_forward_ts,
            }


def _safe_uid(uid: str) -> str:
    """Defensive sanitiser — UIDs must be dotted ASCII numerics per PS3.5,
    but we strip anything that could escape the spool directory."""
    cleaned = uid.replace("/", "_").replace("\\", "_").replace("..", "_")
    cleaned = cleaned.strip()
    if not cleaned:
        raise ValueError("UID is empty after sanitisation")
    if len(cleaned) > 200:
        raise ValueError("UID is too long")
    return cleaned


# Module-level singleton used by the web layer + worker.
spool = Spool()

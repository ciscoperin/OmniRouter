"""Egress strategies for forwarding cached studies.

Three concrete forwarders implement a single ``Forwarder.forward`` interface:
    - ``DicomForwarder``    — DIMSE C-STORE (plain or TLS)
    - ``DicomWebForwarder`` — DICOMweb STOW-RS over HTTPS

The router calls :func:`make_forwarder` with the current ``Destination`` and
hands it the cached datasets for one study. Each forwarder returns a
:class:`ForwardResult` describing how many instances succeeded / failed.
"""

from __future__ import annotations

import logging
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import Iterable

import httpx
from pydicom.dataset import Dataset
from pydicom.uid import ExplicitVRLittleEndian, ImplicitVRLittleEndian
from pynetdicom import AE

from .config import LOCAL_AET, Destination
from .tls_util import build_client_ssl_context

log = logging.getLogger("omnirouter.forwarders")


@dataclass
class ForwardResult:
    forwarded: int = 0
    failed: int = 0
    note: str = ""
    succeeded_assoc: bool = True  # False when the *association* itself failed
    error_messages: list[str] = field(default_factory=list)


class Forwarder(ABC):
    """Abstract egress strategy."""

    def __init__(self, dest: Destination) -> None:
        self.dest = dest

    @abstractmethod
    def forward(
        self,
        study_uid: str,
        datasets: list[tuple[Path, Dataset]],
    ) -> ForwardResult:
        """Send all datasets for one study; return per-instance counts."""

    @abstractmethod
    def describe(self) -> str:
        """Short human-readable description used for log lines."""


# ---------------------------------------------------------------------------
# DIMSE C-STORE (plain or TLS)
# ---------------------------------------------------------------------------
class DicomForwarder(Forwarder):
    """Classic DIMSE forwarder — pynetdicom association + C-STORE per file."""

    def describe(self) -> str:
        proto = "TLS" if self.dest.mode == "dicom_tls" else "plain"
        return f"{self.dest.aet}@{self.dest.host}:{self.dest.port} ({proto})"

    def forward(
        self,
        study_uid: str,
        datasets: list[tuple[Path, Dataset]],
    ) -> ForwardResult:
        result = ForwardResult()
        if not datasets:
            return result

        ae = AE(ae_title=LOCAL_AET)
        ae.requested_contexts = []

        # Add storage contexts based on the actual SOP classes in the study.
        added_contexts: set[str] = set()
        for _, ds in datasets:
            if ds.SOPClassUID not in added_contexts:
                ae.add_requested_context(
                    ds.SOPClassUID,
                    [ExplicitVRLittleEndian, ImplicitVRLittleEndian],
                )
                added_contexts.add(ds.SOPClassUID)

        tls_args = None
        if self.dest.mode == "dicom_tls":
            try:
                ctx = build_client_ssl_context()
                tls_args = (ctx, self.dest.host)
            except Exception as exc:
                log.exception("Failed to build TLS context")
                result.succeeded_assoc = False
                result.failed = len(datasets)
                result.error_messages.append(f"TLS context error: {exc}")
                return result

        try:
            assoc = ae.associate(
                self.dest.host,
                self.dest.port,
                ae_title=self.dest.aet,
                tls_args=tls_args,
            )
        except Exception as exc:
            log.exception("Association attempt raised")
            result.succeeded_assoc = False
            result.failed = len(datasets)
            result.error_messages.append(f"Association error: {exc}")
            return result

        if not assoc.is_established:
            log.error(
                "Association rejected/failed: %s@%s:%s",
                self.dest.aet,
                self.dest.host,
                self.dest.port,
            )
            result.succeeded_assoc = False
            result.failed = len(datasets)
            result.error_messages.append("Association rejected")
            return result

        try:
            for f, ds in datasets:
                status = assoc.send_c_store(ds)
                if status and getattr(status, "Status", 0xFFFF) == 0x0000:
                    result.forwarded += 1
                else:
                    code = getattr(status, "Status", "n/a")
                    log.error("C-STORE for %s returned status %s", f.name, code)
                    result.failed += 1
                    result.error_messages.append(
                        f"{f.name}: C-STORE status {code}"
                    )
        finally:
            try:
                assoc.release()
            except Exception:
                pass

        return result


# ---------------------------------------------------------------------------
# DICOMweb STOW-RS (HTTPS multipart/related)
# ---------------------------------------------------------------------------
class DicomWebForwarder(Forwarder):
    """STOW-RS forwarder — POSTs all instances as multipart/related."""

    # Per-request timeout (connect, read, write, pool). Generous to allow
    # large studies; aggressive enough that an unresponsive relay surfaces.
    REQUEST_TIMEOUT = httpx.Timeout(connect=10.0, read=180.0, write=120.0, pool=10.0)

    # Bounded per-batch retry: the entire STOW-RS POST is retried once on
    # transient network errors or 5xx responses (so each study gets at most
    # MAX_ATTEMPTS attempts total). This is *not* per-instance retry — the
    # multipart body always contains every instance in the study. 4xx
    # responses are surfaced immediately without retry; per-instance
    # success/failure is then read from the PS3.18 response sequences
    # (00081199 ReferencedSOPSequence / 00081198 FailedSOPSequence).
    MAX_ATTEMPTS = 2

    # Custom header to signal the OmniPACS Relay's delivery semantics.
    DELIVERY_HEADER = "X-OmniPACS-Delivery"

    def describe(self) -> str:
        return f"STOW-RS → {self.dest.base_url} ({self.dest.delivery_mode})"

    def forward(
        self,
        study_uid: str,
        datasets: list[tuple[Path, Dataset]],
    ) -> ForwardResult:
        result = ForwardResult()
        if not datasets:
            return result

        url = f"{self.dest.base_url.rstrip('/')}/studies/{study_uid}"
        boundary = f"omnirouter-{uuid.uuid4().hex}"
        body = _build_multipart_body(datasets, boundary)
        headers = {
            "Content-Type": (
                f'multipart/related; type="application/dicom"; boundary={boundary}'
            ),
            "Accept": "application/dicom+json",
            "Authorization": f"Bearer {self.dest.bearer_token}",
            self.DELIVERY_HEADER: self.dest.delivery_mode,
            "User-Agent": "OmniPACS-OmniRouter/1.0",
        }

        last_error: str | None = None
        for attempt in range(1, self.MAX_ATTEMPTS + 1):
            t0 = time.monotonic()
            try:
                with httpx.Client(
                    verify=self.dest.verify_tls,
                    timeout=self.REQUEST_TIMEOUT,
                    follow_redirects=False,
                ) as client:
                    resp = client.post(url, content=body, headers=headers)
            except httpx.HTTPError as exc:
                elapsed = time.monotonic() - t0
                last_error = f"{type(exc).__name__}: {exc}"
                log.warning(
                    "STOW-RS POST failed (attempt %d/%d) after %.2fs: %s",
                    attempt,
                    self.MAX_ATTEMPTS,
                    elapsed,
                    last_error,
                )
                if attempt < self.MAX_ATTEMPTS:
                    time.sleep(min(2.0 * attempt, 5.0))
                    continue
                result.succeeded_assoc = False
                result.failed = len(datasets)
                result.error_messages.append(last_error)
                return result

            elapsed = time.monotonic() - t0

            # 2xx success
            if 200 <= resp.status_code < 300:
                forwarded, failed = _parse_stow_response(resp, len(datasets))
                result.forwarded = forwarded
                result.failed = failed
                log.info(
                    "STOW-RS POST %s → HTTP %d in %.2fs (forwarded=%d, failed=%d)",
                    _safe_url_for_log(url),
                    resp.status_code,
                    elapsed,
                    forwarded,
                    failed,
                )
                return result

            # 4xx — auth / bad request — don't retry, surface clearly.
            if 400 <= resp.status_code < 500:
                msg = f"HTTP {resp.status_code}: {_safe_resp_body(resp)}"
                log.error(
                    "STOW-RS POST %s rejected after %.2fs: %s",
                    _safe_url_for_log(url),
                    elapsed,
                    msg,
                )
                result.succeeded_assoc = False
                result.failed = len(datasets)
                result.error_messages.append(msg)
                return result

            # 5xx — retry once
            last_error = f"HTTP {resp.status_code}: {_safe_resp_body(resp)}"
            log.warning(
                "STOW-RS POST %s → HTTP %d after %.2fs (attempt %d/%d): %s",
                _safe_url_for_log(url),
                resp.status_code,
                elapsed,
                attempt,
                self.MAX_ATTEMPTS,
                last_error,
            )
            if attempt < self.MAX_ATTEMPTS:
                time.sleep(min(2.0 * attempt, 5.0))
                continue

            result.succeeded_assoc = False
            result.failed = len(datasets)
            result.error_messages.append(last_error or "STOW-RS POST failed")
            return result

        # Should not reach here.
        result.failed = len(datasets)
        result.succeeded_assoc = False
        return result


def _build_multipart_body(
    datasets: Iterable[tuple[Path, Dataset]],
    boundary: str,
) -> bytes:
    """Build a ``multipart/related; type=application/dicom`` body."""
    crlf = b"\r\n"
    boundary_b = boundary.encode("ascii")
    buf = BytesIO()
    for f, ds in datasets:
        # Encode the dataset back to a DICOM file in memory.
        part = BytesIO()
        ds.save_as(part, write_like_original=False)
        part_bytes = part.getvalue()

        buf.write(b"--" + boundary_b + crlf)
        buf.write(b"Content-Type: application/dicom" + crlf)
        buf.write(crlf)
        buf.write(part_bytes)
        buf.write(crlf)
    buf.write(b"--" + boundary_b + b"--" + crlf)
    return buf.getvalue()


def _parse_stow_response(
    resp: httpx.Response,
    expected_count: int,
) -> tuple[int, int]:
    """Return (forwarded, failed) from a STOW-RS response.

    For DICOM PS3.18 sync (200) responses, the body must be DICOM JSON
    with ``00081199`` (ReferencedSOPSequence, successes) and/or
    ``00081198`` (FailedSOPSequence, failures). We parse those literally
    — anything we can't parse is conservatively counted as **failed** so
    a malformed/non-conforming server doesn't silently look like success.

    For 202 (async accept) we count the whole batch as forwarded — the
    SCP has accepted responsibility for delivery.
    """
    content_type = resp.headers.get("content-type", "").lower()

    if resp.status_code == 202:
        # Asynchronous accept — server took ownership of all instances.
        return expected_count, 0

    # Sync (200-class). Per PS3.18 the response is DICOM JSON.
    if "json" not in content_type:
        log.warning(
            "STOW-RS sync response has non-JSON Content-Type %r; "
            "cannot verify per-instance status — counting %d instance(s) as failed",
            content_type or "<missing>",
            expected_count,
        )
        return 0, expected_count

    try:
        body = resp.json()
    except Exception:
        log.warning(
            "STOW-RS sync response JSON parse failed — "
            "counting %d instance(s) as failed",
            expected_count,
        )
        return 0, expected_count

    if isinstance(body, list):
        body = body[0] if body else {}
    if not isinstance(body, dict):
        log.warning(
            "STOW-RS sync response has unexpected JSON shape (%s) — "
            "counting %d instance(s) as failed",
            type(body).__name__,
            expected_count,
        )
        return 0, expected_count

    succeeded = body.get("00081199", {}).get("Value", []) or []
    failed = body.get("00081198", {}).get("Value", []) or []
    s_count = len(succeeded) if isinstance(succeeded, list) else 0
    f_count = len(failed) if isinstance(failed, list) else 0

    # Neither sequence present — server returned 200 with an empty/unknown
    # JSON body. Per PS3.18 a sync 200 response should report at least one
    # of these sequences. Treat as failed so it surfaces on the dashboard.
    if s_count == 0 and f_count == 0:
        log.warning(
            "STOW-RS 200 response had no ReferencedSOPSequence (00081199) "
            "or FailedSOPSequence (00081198) — counting %d instance(s) as failed",
            expected_count,
        )
        return 0, expected_count
    return s_count, f_count


def _safe_url_for_log(url: str) -> str:
    """Truncate very long URLs for cleaner log lines."""
    if len(url) <= 120:
        return url
    return url[:60] + "…" + url[-50:]


def _safe_resp_body(resp: httpx.Response) -> str:
    """Return a short, single-line body excerpt for logging."""
    try:
        text = resp.text
    except Exception:
        return "<unreadable body>"
    if not text:
        return "<empty body>"
    text = " ".join(text.split())  # collapse whitespace
    if len(text) > 200:
        text = text[:200] + "…"
    return text


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------
def make_forwarder(dest: Destination) -> Forwarder:
    """Pick the right forwarder for the destination's mode."""
    if dest.mode == "dicomweb":
        return DicomWebForwarder(dest)
    # ``dicom`` and ``dicom_tls`` both use the DIMSE forwarder.
    return DicomForwarder(dest)

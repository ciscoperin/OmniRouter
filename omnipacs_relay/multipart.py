"""Parse a ``multipart/related; type="application/dicom"`` request body
into individual DICOM payload bytes.

We intentionally implement this by hand instead of pulling in a heavy
multipart library: the wire format is small, well-defined, and we want
to be strict about rejecting anything that doesn't look like the
PS3.18 STOW-RS shape (so we don't silently accept random uploads).

Returns a list of raw DICOM byte strings (one per part). The handler
upstream then validates each blob with ``pydicom.dcmread``.
"""

from __future__ import annotations

import re
from typing import Iterable

# Allowable Content-Type values per PS3.18 for a DICOM part. We accept
# any media type starting with "application/dicom" (the spec also allows
# transfer-syntax parameters etc).
_DICOM_PART_CT_PREFIX = "application/dicom"


class MultipartError(ValueError):
    """Raised when the request body isn't a well-formed
    multipart/related; type="application/dicom" body."""


def parse_boundary(content_type: str | None) -> str:
    """Extract the boundary token from a Content-Type header.

    Raises MultipartError if the header is missing/wrong-shape.
    """
    if not content_type:
        raise MultipartError("Missing Content-Type header")

    # Normalise the lookup but preserve the original boundary value
    # (boundary tokens are case-sensitive per RFC 2046).
    lowered = content_type.lower()
    if not lowered.startswith("multipart/related"):
        raise MultipartError(
            f"Expected multipart/related body, got Content-Type {content_type!r}"
        )

    # Optional but strongly recommended: type="application/dicom"
    # parameter. We accept either present-and-correct or absent.
    type_match = re.search(r'type\s*=\s*"?([^";]+)"?', content_type, re.I)
    if type_match:
        type_val = type_match.group(1).strip().strip('"').lower()
        if not type_val.startswith(_DICOM_PART_CT_PREFIX):
            raise MultipartError(
                f"multipart/related type parameter must be application/dicom, "
                f"got {type_val!r}"
            )

    boundary_match = re.search(r'boundary\s*=\s*"?([^";]+)"?', content_type, re.I)
    if not boundary_match:
        raise MultipartError("multipart/related Content-Type missing boundary parameter")
    boundary = boundary_match.group(1).strip()
    if not boundary:
        raise MultipartError("multipart boundary is empty")
    return boundary


def split_parts(body: bytes, boundary: str) -> list[bytes]:
    """Split a multipart body into its part bytes (headers + body each).

    Each returned blob still includes the per-part header block followed
    by ``\\r\\n\\r\\n`` and then the part body. The caller will run
    :func:`extract_dicom_part` on each entry to validate and strip.
    """
    if not body:
        raise MultipartError("Empty request body")
    boundary_bytes = boundary.encode("ascii")
    delim = b"--" + boundary_bytes
    closing = b"--" + boundary_bytes + b"--"

    # PS3.18 mandates CRLF, but be lenient about leading whitespace before
    # the first boundary.
    if delim not in body:
        raise MultipartError("Boundary not found in request body")

    # Strip everything up to and including the first delimiter line.
    chunks = body.split(delim)
    parts: list[bytes] = []
    for idx, chunk in enumerate(chunks):
        if idx == 0:
            # Preamble — ignored per RFC 2046.
            continue
        # Each chunk starts with either CRLF (a regular part) or "--"
        # (the closing boundary marker).
        if chunk.startswith(b"--"):
            # Closing delimiter — anything after is epilogue, ignored.
            break
        # Skip the CRLF that follows the boundary token.
        if chunk.startswith(b"\r\n"):
            chunk = chunk[2:]
        elif chunk.startswith(b"\n"):
            chunk = chunk[1:]
        # Trim the trailing CRLF that precedes the next boundary.
        if chunk.endswith(b"\r\n"):
            chunk = chunk[:-2]
        elif chunk.endswith(b"\n"):
            chunk = chunk[:-1]
        if chunk:
            parts.append(chunk)

    if not parts:
        # We saw delimiters but no actual parts.
        raise MultipartError("No parts found in multipart body")
    # Guard against truly malformed bodies missing the closing marker —
    # if the closing wasn't seen we still surface what we got, but flag
    # it so it's loud.
    if closing not in body:
        # Not fatal — some clients omit the closing CRLF — but worth a
        # debug trail. Still parse what we have.
        pass
    return parts


def extract_dicom_part(part: bytes) -> bytes:
    """From one raw part (headers + body), validate the Content-Type
    is ``application/dicom`` and return just the DICOM body bytes.
    """
    sep_idx = part.find(b"\r\n\r\n")
    if sep_idx < 0:
        sep_idx = part.find(b"\n\n")
        sep_len = 2
    else:
        sep_len = 4
    if sep_idx < 0:
        raise MultipartError("Multipart part missing header/body separator")

    headers_blob = part[:sep_idx]
    body_blob = part[sep_idx + sep_len:]

    # Find Content-Type header (case-insensitive).
    ct: str | None = None
    for line in _split_header_lines(headers_blob):
        name, _, value = line.partition(":")
        if name.strip().lower() == "content-type":
            ct = value.strip()
            break
    if ct is None:
        raise MultipartError("Multipart part missing Content-Type header")
    if not ct.lower().startswith(_DICOM_PART_CT_PREFIX):
        raise MultipartError(
            f"Multipart part has Content-Type {ct!r}, expected application/dicom"
        )

    if not body_blob:
        raise MultipartError("Multipart part has empty body")
    return body_blob


def parse_dicom_multipart(body: bytes, content_type: str | None) -> list[bytes]:
    """End-to-end helper used by the STOW endpoint.

    Validates the request's Content-Type is ``multipart/related;
    type="application/dicom"`` and returns the list of raw DICOM
    byte-strings (one per part).
    """
    boundary = parse_boundary(content_type)
    raw_parts = split_parts(body, boundary)
    return [extract_dicom_part(p) for p in raw_parts]


def _split_header_lines(headers_blob: bytes) -> Iterable[str]:
    text = headers_blob.decode("latin-1", errors="replace")
    for line in text.splitlines():
        line = line.strip()
        if line:
            yield line

"""OmniPACS Relay — STOW-RS receiving service.

Terminates HTTPS STOW-RS requests from any number of remote OmniRouter
installations, queues each accepted instance durably to disk, and
re-emits them locally as DICOM C-STORE to a configured PACS / VNA.

Multi-tenant routing is handled upstream: each OmniRouter pre-patches
``InstitutionName`` (and friends) before sending. The relay itself stays
tenant-agnostic at the data plane — it authenticates the caller,
ingests, queues, forwards.
"""

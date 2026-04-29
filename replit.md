# Workspace

## Overview

pnpm workspace monorepo using TypeScript. Each package manages its own dependencies.

## Stack

- **Monorepo tool**: pnpm workspaces
- **Node.js version**: 24
- **Package manager**: pnpm
- **TypeScript version**: 5.9
- **API framework**: Express 5
- **Database**: PostgreSQL + Drizzle ORM
- **Validation**: Zod (`zod/v4`), `drizzle-zod`
- **API codegen**: Orval (from OpenAPI spec)
- **Build**: esbuild (CJS bundle)

## Key Commands

- `pnpm run typecheck` — full typecheck across all packages
- `pnpm run build` — typecheck + build all packages
- `pnpm --filter @workspace/api-spec run codegen` — regenerate API hooks and Zod schemas from OpenAPI spec
- `pnpm --filter @workspace/db run push` — push DB schema changes (dev only)
- `pnpm --filter @workspace/api-server run dev` — run API server locally

See the `pnpm-workspace` skill for workspace structure, TypeScript setup, and package details.

## OmniRouter (Python DICOM Router)

Located at `omnirouter/`. Standalone Python application — not a JS workspace package.

- DICOM SCP listens on `0.0.0.0:7775` AE `OMNI` (fixed per spec)
- Forwards each received study using one of three egress modes selected from the UI's **Configuration** menu:
  - `dicom`     — plain DIMSE C-STORE
  - `dicom_tls` — DIMSE C-STORE over TLS (`pynetdicom`)
  - `dicomweb` — DICOMweb STOW-RS over HTTPS (`httpx`, multipart/related, Bearer auth, custom `X-OmniPACS-Delivery: sync|async` header)
- Destination is persisted to `omnicache/destination.json` (mode + DIMSE host/port/aet + DICOMweb base_url/bearer_token/verify_tls/delivery_mode). File is `chmod 600`. Legacy files with `use_tls` are auto-migrated to `mode`.
- Egress strategy lives in `omnirouter/forwarders.py` (`Forwarder` ABC + `DicomForwarder` + `DicomWebForwarder`); `omnirouter/router.py` calls `make_forwarder(dest).forward(...)` per study.
- FastAPI web UI on `$PORT` (default 5000) with WebSocket live log streaming. The `GET /api/destination` endpoint never echoes the bearer token — it returns `bearer_configured: bool` instead.
- Cache directory: `./omnicache/`
- Run locally: `python -m omnirouter.main` (workflow: **OmniRouter**)
- Windows Service hosting via `pywin32` (`omnirouter/service_windows.py`) or NSSM — see `omnirouter/README.md`
- Outbound destination configurable via env vars: DIMSE — `OMNI_DEST_HOST`, `OMNI_DEST_PORT`, `OMNI_DEST_AET`, `OMNI_DEST_MODE` (or legacy `OMNI_DEST_TLS=true|false`); DICOMweb — `OMNI_DICOMWEB_URL`, `OMNI_DICOMWEB_TOKEN`, `OMNI_DICOMWEB_VERIFY`, `OMNI_DELIVERY_MODE`.
- Self-signed TLS client cert auto-generated under `omnicache/tls/` on first run; override with `OMNI_TLS_CERT` / `OMNI_TLS_KEY` / `OMNI_TLS_CA`.
- Dependencies: `httpx` for STOW-RS client (added April 2026 alongside the third egress mode).

## OmniPACS Relay (Python STOW-RS receiver)

Located at `omnipacs_relay/` — peer Python package to `omnirouter/`, not a JS workspace package.

- Runs on `$PORT` (default 5001; the Replit workflow exports `PORT=8000`). Workflow: **OmniPACSRelay**.
- HTTPS STOW-RS endpoints `POST /studies` and `POST /studies/{StudyInstanceUID}` per DICOM PS3.18 — multipart/related body, Bearer auth, custom `X-OmniPACS-Delivery: sync|async` header.
- Sync delivery returns 200 + PS3.18 response (`00081199` / `00081198` sequences) after end-to-end forward; async returns 202 immediately after fsync.
- Per-instance durable spool at `omnirelay_spool/inbox/<study>/<sop>.dcm` (atomic write+fsync before ack). Quarantine at `omnirelay_spool/quarantine/...` after 4 failed attempts (with `.error` sidecar).
- Background forwarder thread drains spool and re-emits as DICOM C-STORE to a runtime-editable local PACS target persisted to `omnirelay_spool/local_target.json` (chmod 600).
- Bearer tokens persisted to `omnirelay_spool/tokens.json` (chmod 600). Tokens are 32-byte URL-safe random strings, validated in constant time, last-used tracked. Issue/revoke from the dashboard; new token shown exactly once. Bootstrap via `OMNI_RELAY_TOKENS` env (comma-separated).
- FastAPI dashboard at `/` with WebSocket live log streaming, OmniPACS-branded (#1E0325 / #9A1DBD, Outfit/Open Sans). Admin JSON API at `/api/*`; unauthenticated `/healthz` for load-balancer probes.
- Optional inbound TLS via `OMNI_RELAY_TLS_CERT` / `OMNI_RELAY_TLS_KEY`; otherwise plain HTTP behind a reverse proxy (Replit dev preview adds TLS automatically).
- Operator deliverables: `omnipacs_relay/Dockerfile`, `omnipacs_relay/omnipacs-relay.service` (systemd unit), `omnipacs_relay/README.md`.
- Smoke test (run from repo root): `python -m omnipacs_relay.tests.smoke_e2e` — covers auth-401, sync STOW, async STOW, last-used tracking against an in-process pynetdicom storage SCP.
- Run locally: `PORT=8000 python -m omnipacs_relay.main`. Spool directory `omnirelay_spool/` is gitignored (contains PHI + tokens).

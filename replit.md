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

- DICOM SCP listens on `0.0.0.0:7776` AE `OMNI` (fixed per spec)
- Forwards received studies to a configurable PACS over DICOM TLS (`pynetdicom`)
- FastAPI web UI on `$PORT` (default 5000) with WebSocket live log streaming
- Cache directory: `./omnicache/`
- Run locally: `python -m omnirouter.main` (workflow: **OmniRouter**)
- Windows Service hosting via `pywin32` (`omnirouter/service_windows.py`) or NSSM — see `omnirouter/README.md`
- Outbound destination configured via `OMNI_DEST_*` env vars
- Self-signed TLS client cert auto-generated under `omnicache/tls/` on first run; override with `OMNI_TLS_CERT` / `OMNI_TLS_KEY` / `OMNI_TLS_CA`

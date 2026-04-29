# OmniPACS Relay

A small, opinionated **DICOMweb STOW-RS receiving service** that terminates
HTTPS uploads from any number of remote OmniRouter installations,
durably spools each accepted DICOM instance to disk, and re-emits them
locally as DICOM C-STORE to the operator-configured PACS / VNA.

The relay is intentionally tenant-agnostic at the data plane — each
remote OmniRouter pre-patches `InstitutionName` (and friends) before
forwarding, so the relay only authenticates the caller, ingests, and
forwards. No per-tenant routing logic lives here.

```
   Remote OmniRouter ──HTTPS STOW-RS──▶ OmniPACS Relay ──C-STORE──▶ Local PACS
   (one per site)        bearer auth        on-disk spool      LAN-side
```

---

## Features

| Feature | Notes |
| --- | --- |
| **STOW-RS** | `POST /studies` and `POST /studies/{StudyInstanceUID}` per PS3.18 |
| **Bearer auth** | Per-token last-used tracking, 401 with `WWW-Authenticate: Bearer realm="omnipacs-relay"` |
| **Sync delivery** | Default. Returns `200` + PS3.18 `00081199` / `00081198` payload after end-to-end forward |
| **Async delivery** | Per-request override via `X-OmniPACS-Delivery: async`. Returns `202 {"accepted": N}` after fsync |
| **Durable spool** | Atomic write + fsync per instance before ack |
| **Retry + quarantine** | 4 attempts then quarantine; per-instance `.error` sidecar with cause |
| **Token UI** | Issue / revoke / view-last-used; new tokens shown exactly once |
| **Health endpoint** | `GET /healthz` (no auth, for load-balancer probes) |
| **Live ops dashboard** | OmniPACS-branded SPA at `/` with WebSocket log stream |

---

## Wire contract

Every request:

```
POST /studies                       (or /studies/<StudyInstanceUID>)
Authorization: Bearer <token>
Content-Type: multipart/related; type="application/dicom"; boundary=<rand>
Accept: application/dicom+json
X-OmniPACS-Delivery: sync           (or "async" — header is optional)

--<boundary>
Content-Type: application/dicom

<binary DICOM instance>
--<boundary>
Content-Type: application/dicom

<binary DICOM instance>
--<boundary>--
```

Sync response (HTTP 200, `Content-Type: application/dicom+json`):

```json
{
  "00081199": { "vr": "SQ", "Value": [
    {
      "00081150": { "vr": "UI", "Value": ["1.2.840.10008.5.1.4.1.1.2"] },
      "00081155": { "vr": "UI", "Value": ["1.2.3.4.5.6.7.8"] }
    }
  ] }
}
```

Failures are reported in `00081198` with a `00081197` (`FailureReason`)
US value per PS3.4 STOW-RS table CC.2.3-1.

Async response (HTTP 202):

```json
{ "accepted": 12, "rejected": 0 }
```

Auth failures return `401`:

```
HTTP/1.1 401 Unauthorized
WWW-Authenticate: Bearer realm="omnipacs-relay"
Content-Type: application/json

{"detail": "Bearer token required"}
```

---

## Configuration

All configuration is environment-driven. Defaults are sensible for a
single-machine install.

| Var | Default | Purpose |
| --- | --- | --- |
| `PORT` | `5001` | TCP port the HTTP(S) server binds to |
| `OMNI_RELAY_AET` | `OMNIRELAY` | AE Title used as the SCU when forwarding C-STORE |
| `OMNI_RELAY_SPOOL` | `omnirelay_spool` (cwd) | Spool root. Holds DICOM PHI + tokens — never put on a shared volume |
| `OMNI_RELAY_TARGET_HOST` | `127.0.0.1` | Local PACS / VNA host or IP |
| `OMNI_RELAY_TARGET_PORT` | `11112` | Local PACS DICOM port |
| `OMNI_RELAY_TARGET_AET` | `LOCAL_PACS` | Local PACS AE Title |
| `OMNI_RELAY_DEFAULT_DELIVERY` | `sync` | Default delivery mode when the inbound `X-OmniPACS-Delivery` header is missing |
| `OMNI_RELAY_TOKENS` | _(unset)_ | Comma- or space-separated bootstrap tokens; seeded into the store on first run, persisted afterwards |
| `OMNI_RELAY_TOKEN_FILE` | `<spool>/tokens.json` | Override the on-disk path of the token store (chmod 600 JSON). Useful when the token store should live on a more tightly-permissioned volume than the PHI spool |
| `OMNI_RELAY_TLS_CERT` | _(unset)_ | PEM cert path. Set together with `OMNI_RELAY_TLS_KEY` for operator-supplied HTTPS credentials |
| `OMNI_RELAY_TLS_KEY` | _(unset)_ | PEM key path |
| `OMNI_RELAY_DISABLE_TLS` | _(unset)_ | Set to `1` to opt out of TLS entirely (only when a reverse proxy in front terminates HTTPS for you) |

**TLS is on by default.** The relay always serves STOW-RS over HTTPS:

* If `OMNI_RELAY_TLS_CERT` / `OMNI_RELAY_TLS_KEY` are set, those
  operator-supplied credentials are used.
* Otherwise the relay generates a self-signed cert under
  `$OMNI_RELAY_SPOOL/tls/` on first boot and reuses it across restarts.
  Remote OmniRouter installs that talk to a self-signed dev cert should
  set `verify_tls = false` on their destination — the OmniRouter UI
  exposes that toggle.
* If a reverse proxy in front already terminates TLS (Caddy, nginx,
  HAProxy, or a cloud LB), set `OMNI_RELAY_DISABLE_TLS=1` and the
  relay will bind plain HTTP. The Replit dev preview is one such proxy.

The local target and tokens are **runtime-editable from the
dashboard** and persisted with mode `0600` to:

```
$OMNI_RELAY_SPOOL/local_target.json
$OMNI_RELAY_SPOOL/tokens.json
```

---

## Spool layout

```
$OMNI_RELAY_SPOOL/
├── inbox/
│   └── <StudyInstanceUID>/
│       └── <SOPInstanceUID>.dcm
├── quarantine/
│   └── <StudyInstanceUID>/
│       ├── <SOPInstanceUID>.dcm
│       └── <SOPInstanceUID>.error      # last failure reason
├── tokens.json                          # chmod 600
└── local_target.json                    # chmod 600
```

Each accepted instance is written to `<sop>.dcm.tmp.<pid>.<tid>`,
fsynced, then atomically renamed into place. The directory entry is
fsynced too. Only after the rename succeeds is the STOW caller acked.
This means **no acked instance is ever lost across an unclean
restart**.

After `MAX_ATTEMPTS = 4` consecutive forward failures the instance is
moved to `quarantine/` with a sidecar `.error` describing the last
cause. Quarantined instances can be requeued individually or in bulk
from the dashboard (or by `POST /api/quarantine/.../requeue`).

---

## Production deployment

### Docker

```
docker build -f omnipacs_relay/Dockerfile -t omnipacs-relay:1.0.0 .
docker run -d --name omnirelay --restart unless-stopped \
    -p 8443:8443 \
    -v /var/lib/omnirelay/spool:/spool \
    -e OMNI_RELAY_SPOOL=/spool \
    -e PORT=8443 \
    -e OMNI_RELAY_TARGET_HOST=10.0.0.5 \
    -e OMNI_RELAY_TARGET_PORT=11112 \
    -e OMNI_RELAY_TARGET_AET=LOCAL_PACS \
    -e OMNI_RELAY_TOKENS="$(uuidgen)" \
    -e OMNI_RELAY_TLS_CERT=/spool/tls/cert.pem \
    -e OMNI_RELAY_TLS_KEY=/spool/tls/key.pem \
    omnipacs-relay:1.0.0
```

### systemd

The unit at `omnipacs_relay/omnipacs-relay.service` installs the relay
as `omnipacs-relay.service`. Operator config goes in
`/etc/omnirelay/omnirelay.env`:

```
PORT=8443
OMNI_RELAY_TARGET_HOST=10.0.0.5
OMNI_RELAY_TARGET_PORT=11112
OMNI_RELAY_TARGET_AET=LOCAL_PACS
OMNI_RELAY_TOKENS=00112233-4455-6677-8899-aabbccddeeff
OMNI_RELAY_TLS_CERT=/etc/omnirelay/tls/cert.pem
OMNI_RELAY_TLS_KEY=/etc/omnirelay/tls/key.pem
```

Then:

```
sudo systemctl daemon-reload
sudo systemctl enable --now omnipacs-relay
sudo journalctl -u omnipacs-relay -f
```

The unit hardens the process (`ProtectSystem=strict`, `ProtectHome=true`,
`NoNewPrivileges=true`) and only allows writes under
`/var/lib/omnirelay`.

---

## Issuing your first token

A fresh install has zero tokens; the dashboard at `/` is unauthenticated
on the admin endpoints (FastAPI docs and `/api/*`). To onboard a remote
OmniRouter:

1. Open the dashboard.
2. Click **Issue New** in the **Bearer Tokens** card.
3. Give the token a label that identifies the remote site (e.g.
   `clinic-portland`).
4. Copy the token from the modal — **it is shown once only**.
5. On the remote OmniRouter, paste the token into the **Configuration →
   DICOM over HTTPS → Bearer Token** field.

For a Docker / systemd install you can skip the dashboard step by
setting `OMNI_RELAY_TOKENS=<token>` in the environment file.
The token is auto-imported on first boot, given the label `env-1`,
and persisted in `tokens.json` (chmod 600) so subsequent boots don't
need the env var.

---

## Admin API

Every admin endpoint is JSON. They are **not** bearer-authenticated by
default — the assumption is that the dashboard sits on a private
management network or behind a reverse proxy with its own access
control. If you expose the dashboard over the internet, put it behind
mTLS / basic auth at the proxy.

| Verb | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/status` | Service summary + spool counters |
| `GET` | `/api/local-target` | Current local PACS target |
| `PUT` | `/api/local-target` | Update local PACS target |
| `POST` | `/api/forwarder/start` | Start the worker |
| `POST` | `/api/forwarder/stop` | Stop the worker |
| `GET` | `/api/tokens` | List token labels + last-used |
| `POST` | `/api/tokens` | Issue a new token (returns the raw token once) |
| `DELETE` | `/api/tokens/{label}` | Revoke a token |
| `GET` | `/api/quarantine` | List quarantined instances |
| `POST` | `/api/quarantine/{study}/{sop}/requeue` | Requeue one instance |
| `POST` | `/api/quarantine/requeue-all` | Requeue everything |
| `GET` | `/api/logs` | Snapshot of recent log entries |
| `POST` | `/api/logs/clear` | Clear the in-memory log ring |
| `WS` | `/ws/logs` | Live log stream (used by the dashboard) |

---

## End-to-end smoke test

A small smoke test script lives at
`omnipacs_relay/tests/smoke_e2e.py`. It exercises the relay's full
ingest → spool → forward → C-STORE pipeline by:

1. Spinning up an in-process pynetdicom storage SCP (the "local PACS")
   on a free port.
2. Pointing the relay at that SCP via `PUT /api/local-target`.
3. Issuing a fresh bearer token via `POST /api/tokens`.
4. Posting STOW-RS multipart bodies directly into the relay using the
   exact wire contract OmniRouter uses (multipart/related,
   `Authorization: Bearer …`, `X-OmniPACS-Delivery: sync|async`).
5. Asserting:
   * Sync STOW returns 200 + a PS3.18 `00081199` payload referencing the
     forwarded SOP, and the SOP arrives at the SCP.
   * Async STOW returns 202 immediately, and the SOP arrives at the SCP
     within the deadline.
   * Unauthenticated requests return 401 + `WWW-Authenticate: Bearer …`.
   * Per-token `last_used_ts` is updated by a successful auth.

The OmniRouter → relay HTTPS leg is exercised by OmniRouter's own STOW
client (covered by its task #1 work); this script focuses on
end-to-end relay behaviour with a deterministic SCP target.

Run it from the repo root once the `OmniPACSRelay` workflow is up:

```
python -m omnipacs_relay.tests.smoke_e2e
```

---

## Security notes

* **At-rest secrets** — `tokens.json` and `local_target.json` are
  written with mode `0600`. They are **not** encrypted at rest. Treat
  the spool directory like any other PHI store: full-disk encryption
  + restrictive filesystem permissions.
* **Token format** — tokens are 32 random bytes (`secrets.token_urlsafe(32)`),
  ~43 base64url characters. They are validated in constant time against
  every registered token.
* **TLS** — production deployments **must** terminate TLS, either via
  the relay's built-in `OMNI_RELAY_TLS_CERT` / `OMNI_RELAY_TLS_KEY`
  options or via a reverse proxy. Bearer tokens cannot be sent over
  plain HTTP.
* **PHI in logs** — the live log stream redacts bearer tokens
  (only the token's *label* is logged) and does not echo DICOM pixel
  data. SOP / Study UIDs do appear in log lines; treat the log feed
  as PHI-adjacent.

---

## Versions

* OmniPACS Relay — **v1.0.0**
* Wire-compatible with OmniRouter ≥ **v1.0.2**

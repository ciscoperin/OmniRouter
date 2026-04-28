# OmniRouter

A small DICOM router with a built-in web UI. The router listens for
incoming DICOM associations on the local machine and forwards every
received study to a remote PACS over DICOM TLS.

| Item              | Value (fixed in v1)             |
| ----------------- | ------------------------------- |
| Listening address | local machine IP (auto-detected) |
| Listening port    | **7775**                        |
| Local AE Title    | **OMNI**                        |
| Cache directory   | `./omnicache`                   |
| Web UI port       | `5000` (or `$PORT`)             |

The destination (host / port / AE / transfer mode) is editable from the
**Configuration** menu in the web UI and is persisted to
`omnicache/destination.json`. Initial defaults can also be supplied via
environment variables — see *Configuration* below.

## Run interactively (any OS, for testing)

```bash
python -m pip install -r omnirouter/requirements.txt
python -m omnirouter.main
```

Then open <http://localhost:5000/>.

## Install as a Windows Service

Two supported approaches.

### 1. pywin32

```powershell
pip install pywin32
python -m omnirouter.service_windows install
python -m omnirouter.service_windows start
```

Manage the service afterwards with `services.msc` or `sc.exe`.
To remove:

```powershell
python -m omnirouter.service_windows stop
python -m omnirouter.service_windows remove
```

### 2. NSSM (recommended for non-developer machines)

```powershell
nssm install OmniRouter "C:\Python311\python.exe" "-m" "omnirouter.main"
nssm set OmniRouter AppDirectory "C:\Path\To\OmniRouter"
nssm set OmniRouter AppEnvironmentExtra ^
  "OMNI_DEST_HOST=pacs.example.org" ^
  "OMNI_DEST_PORT=2762" ^
  "OMNI_DEST_AET=REMOTE_PACS" ^
  "OMNI_DEST_TLS=true"
nssm start OmniRouter
```

## Configuration

All configuration is via environment variables. The listener side is
locked per spec; only the outbound destination is configurable in v1.

| Variable           | Default              | Notes                                       |
| ------------------ | -------------------- | ------------------------------------------- |
| `PORT`             | `5000`               | Web UI port                                 |
| `OMNI_CACHE_DIR`   | `omnicache`          | Local DICOM cache                           |
| `OMNI_DEST_HOST`   | `wan.example.com`    | Destination PACS hostname / IP              |
| `OMNI_DEST_PORT`   | `11112`              | Destination DICOM TLS port                  |
| `OMNI_DEST_AET`    | `REMOTE_PACS`        | Destination AE Title                        |
| `OMNI_DEST_TLS`    | `true`               | Use DICOM TLS for outbound C-STORE          |
| `OMNI_TLS_CERT`    | (auto self-signed)   | Path to client certificate (PEM)            |
| `OMNI_TLS_KEY`     | (auto self-signed)   | Path to client key (PEM)                    |
| `OMNI_TLS_CA`      | (none)               | Path to CA bundle for peer verification     |
| `OMNI_TLS_VERIFY`  | `false`              | Verify the remote certificate (set `true` once `OMNI_TLS_CA` is provided) |

When no certificate is supplied, OmniRouter generates a self-signed
client cert under `omnicache/tls/` on first run. This is suitable for
v1 / dev — for production, point `OMNI_TLS_CERT`/`OMNI_TLS_KEY` at
issued credentials and enable `OMNI_TLS_VERIFY` with `OMNI_TLS_CA`.

## Web UI

The UI mirrors the original OmniRouter layout:

* **Stop / Start** the listener
* Live status panel (listening address, port, AET, cache dir)
* Destination summary (with transfer-mode state)
* **Configuration** menu — edit destination Host / Port / AE Title and
  switch between **Regular DICOM** and **DICOM TLS** transfer at runtime.
  Changes persist to `omnicache/destination.json` and apply to the next
  outbound association immediately (no restart).
* Live log stream (INFO / WARN / ERROR colorised)
* Counters (received, forwarded, failures, studies in flight)
* **Clear Log** button

The stream uses a WebSocket so the page updates the moment a record is
emitted by `pynetdicom` or by the router itself.

## Verifying the listener

From any DICOM client on the same machine you can verify with `echoscu`
(part of `dcmtk`):

```bash
echoscu -aec OMNI -aet TESTER 127.0.0.1 7775
```

…and you should see a `C-ECHO received` line in the UI log.

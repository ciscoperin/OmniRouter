// OmniRouter web UI — live status + log streaming via WebSocket.

const els = {
  logArea: document.getElementById("log-area"),
  listenAddress: document.getElementById("listen-address"),
  listenPort: document.getElementById("listen-port"),
  localAet: document.getElementById("local-aet"),
  cacheDir: document.getElementById("cache-dir"),
  destSummary: document.getElementById("dest-summary"),
  toggleBtn: document.getElementById("toggle-listener"),
  clearBtn: document.getElementById("clear-log"),
  connState: document.getElementById("conn-state"),
  statRecv: document.getElementById("stat-recv"),
  statFwd: document.getElementById("stat-fwd"),
  statFail: document.getElementById("stat-fail"),
  statFlight: document.getElementById("stat-flight"),
  // Configuration modal
  menuConfig: document.getElementById("menu-configuration"),
  configOverlay: document.getElementById("config-overlay"),
  configClose: document.getElementById("config-close"),
  configCancel: document.getElementById("config-cancel"),
  configForm: document.getElementById("config-form"),
  configSave: document.getElementById("config-save"),
  configError: document.getElementById("config-error"),
  destHost: document.getElementById("dest-host"),
  destPort: document.getElementById("dest-port"),
  destAet: document.getElementById("dest-aet"),
  destBaseUrl: document.getElementById("dest-base-url"),
  destBearer: document.getElementById("dest-bearer"),
  destDelivery: document.getElementById("dest-delivery"),
  destVerifyTls: document.getElementById("dest-verify-tls"),
  bearerToggle: document.getElementById("bearer-toggle"),
  bearerStatus: document.getElementById("bearer-status"),
  sectionDimse: document.getElementById("section-dimse"),
  sectionDicomWeb: document.getElementById("section-dicomweb"),
  openConfigBtn: document.getElementById("open-config"),
  connStateLabel: document.querySelector("#conn-state .conn-label"),
};

let isRunning = true;
let ws = null;
let reconnectTimer = null;

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

function appendLogLine(entry) {
  const line = document.createElement("div");
  const lvl = String(entry.level || "info").toLowerCase();
  line.className = `log-line ${lvl}`;
  line.innerHTML =
    `<span class="ts">${escapeHtml(entry.ts)}</span>` +
    `<span class="lvl">${escapeHtml(entry.level)}</span>` +
    `<span class="msg">${escapeHtml(entry.message)}</span>`;
  els.logArea.appendChild(line);

  // Cap DOM size so the page stays responsive over long runs.
  while (els.logArea.childElementCount > 1500) {
    els.logArea.removeChild(els.logArea.firstChild);
  }

  // Keep auto-scrolled to bottom unless the user has scrolled up.
  const nearBottom =
    els.logArea.scrollHeight - els.logArea.scrollTop - els.logArea.clientHeight <
    80;
  if (nearBottom) {
    els.logArea.scrollTop = els.logArea.scrollHeight;
  }
}

function replaceLog(entries) {
  els.logArea.innerHTML = "";
  for (const e of entries) appendLogLine(e);
}

function setRunning(running) {
  isRunning = running;
  els.toggleBtn.textContent = running ? "Stop" : "Start";
  els.toggleBtn.classList.toggle("btn-stop", running);
  els.toggleBtn.classList.toggle("btn-start", !running);
}

function setConn(state, label) {
  els.connState.classList.toggle("is-up", state === "up");
  els.connState.classList.toggle("is-down", state === "down");
  if (els.connStateLabel) els.connStateLabel.textContent = label;
}

function describeDestination(d) {
  if (d.mode === "dicomweb") {
    const verify = d.verify_tls ? "verified" : "no peer verify";
    return `STOW-RS → ${d.base_url || "(unconfigured)"} (${d.delivery_mode}, ${verify})`;
  }
  const proto =
    d.mode === "dicom_tls"
      ? `TLS${d.verify_peer ? ", peer verified" : ", peer not verified"}`
      : "plain";
  return `${d.aet}@${d.host}:${d.port}  (${proto})`;
}

async function fetchStatus() {
  try {
    const r = await fetch("/api/status");
    if (!r.ok) return;
    const s = await r.json();
    els.listenAddress.textContent = `${s.listening_address}`;
    els.listenPort.textContent = `${s.listening_port}`;
    els.localAet.textContent = s.local_aet;
    els.cacheDir.textContent = s.cache_dir;
    els.destSummary.textContent = describeDestination(s.destination);

    els.statRecv.textContent = s.router.instances_received;
    els.statFwd.textContent = s.router.instances_forwarded;
    els.statFail.textContent = s.router.forward_failures;
    els.statFlight.textContent = s.router.studies_in_flight;
    setRunning(s.router.running);
  } catch (e) {
    /* ignore — websocket will reflect connection state */
  }
}

function connectWs() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws/logs`);

  ws.addEventListener("open", () => {
    setConn("up", "Live");
  });

  ws.addEventListener("message", (ev) => {
    const data = JSON.parse(ev.data);
    if (data.type === "snapshot") {
      replaceLog(data.entries);
    } else if (data.type === "entry") {
      appendLogLine(data.entry);
    }
  });

  ws.addEventListener("close", () => {
    setConn("down", "Reconnecting…");
    if (!reconnectTimer) {
      reconnectTimer = setTimeout(() => {
        reconnectTimer = null;
        connectWs();
      }, 1500);
    }
  });

  ws.addEventListener("error", () => {
    try {
      ws.close();
    } catch (_) {}
  });
}

els.clearBtn.addEventListener("click", async () => {
  await fetch("/api/logs/clear", { method: "POST" });
  els.logArea.innerHTML = "";
});

// --- Configuration modal --------------------------------------------------
function setMode(mode) {
  const isWeb = mode === "dicomweb";
  els.sectionDimse.hidden = isWeb;
  els.sectionDicomWeb.hidden = !isWeb;
  // Keep tabbing sane: disabled inputs aren't required when hidden.
  for (const input of els.sectionDimse.querySelectorAll("input")) {
    input.disabled = isWeb;
  }
  for (const input of els.sectionDicomWeb.querySelectorAll("input, select")) {
    input.disabled = !isWeb;
  }
  // Keep verify-tls / delivery sane defaults if still empty.
  if (isWeb && !els.destDelivery.value) {
    els.destDelivery.value = "sync";
  }
}

function openConfigModal() {
  fetch("/api/destination")
    .then((r) => r.json())
    .then((d) => {
      els.destHost.value = d.host || "";
      els.destPort.value = d.port || "";
      els.destAet.value = d.aet || "";
      els.destBaseUrl.value = d.base_url || "";
      els.destBearer.value = "";
      els.destBearer.type = "password";
      els.bearerToggle.textContent = "Show";
      els.destDelivery.value = d.delivery_mode || "sync";
      els.destVerifyTls.checked = d.verify_tls !== false;

      if (d.bearer_configured) {
        els.bearerStatus.hidden = false;
        els.destBearer.placeholder =
          "Leave blank to keep current token, or paste a new one";
      } else {
        els.bearerStatus.hidden = true;
        els.destBearer.placeholder = "paste bearer token";
      }

      const radio = els.configForm.querySelector(
        `input[name="mode"][value="${d.mode}"]`,
      );
      if (radio) radio.checked = true;
      setMode(d.mode);

      els.configError.hidden = true;
      els.configError.textContent = "";
      els.configOverlay.hidden = false;
      // Focus the first visible input.
      if (d.mode === "dicomweb") {
        els.destBaseUrl.focus();
      } else {
        els.destHost.focus();
      }
    })
    .catch(() => {
      els.configError.textContent = "Could not load current configuration.";
      els.configError.hidden = false;
      els.configOverlay.hidden = false;
    });
}

function closeConfigModal() {
  els.configOverlay.hidden = true;
}

els.menuConfig.addEventListener("click", openConfigModal);
if (els.openConfigBtn)
  els.openConfigBtn.addEventListener("click", openConfigModal);
els.configClose.addEventListener("click", closeConfigModal);
els.configCancel.addEventListener("click", closeConfigModal);
els.configOverlay.addEventListener("click", (e) => {
  if (e.target === els.configOverlay) closeConfigModal();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !els.configOverlay.hidden) closeConfigModal();
});

// Mode-radio click → swap the visible field set.
els.configForm.addEventListener("change", (e) => {
  if (e.target && e.target.name === "mode") {
    setMode(e.target.value);
  }
});

els.bearerToggle.addEventListener("click", () => {
  const showing = els.destBearer.type === "text";
  els.destBearer.type = showing ? "password" : "text";
  els.bearerToggle.textContent = showing ? "Show" : "Hide";
});

els.configForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const formData = new FormData(els.configForm);
  const mode = formData.get("mode");
  if (mode !== "dicom" && mode !== "dicom_tls" && mode !== "dicomweb") {
    els.configError.textContent = "Please select a transfer mode.";
    els.configError.hidden = false;
    return;
  }

  let payload;
  if (mode === "dicomweb") {
    const baseUrl = String(formData.get("base_url") || "").trim();
    if (!baseUrl) {
      els.configError.textContent = "STOW-RS Base URL is required.";
      els.configError.hidden = false;
      return;
    }
    if (!/^https:\/\//i.test(baseUrl)) {
      els.configError.textContent =
        "STOW-RS Base URL must start with https:// — bearer tokens cannot be sent over plain HTTP.";
      els.configError.hidden = false;
      return;
    }
    const tokenRaw = String(formData.get("bearer_token") || "");
    payload = {
      mode: "dicomweb",
      base_url: baseUrl,
      // null = keep existing token; non-empty string = replace.
      bearer_token: tokenRaw.length > 0 ? tokenRaw : null,
      verify_tls: formData.get("verify_tls") === "on",
      delivery_mode: String(formData.get("delivery_mode") || "sync"),
    };
  } else {
    const host = String(formData.get("host") || "").trim();
    const port = Number(formData.get("port"));
    const aet = String(formData.get("aet") || "").trim();
    if (!host) {
      els.configError.textContent = "Destination Host is required.";
      els.configError.hidden = false;
      return;
    }
    if (!Number.isInteger(port) || port < 1 || port > 65535) {
      els.configError.textContent = "Port must be an integer between 1 and 65535.";
      els.configError.hidden = false;
      return;
    }
    if (!aet) {
      els.configError.textContent = "AE Title is required.";
      els.configError.hidden = false;
      return;
    }
    payload = { mode, host, port, aet };
  }

  els.configSave.disabled = true;
  els.configError.hidden = true;
  try {
    const r = await fetch("/api/destination", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!r.ok) {
      let msg = `Save failed (${r.status})`;
      try {
        const data = await r.json();
        if (data && data.detail) {
          msg = Array.isArray(data.detail)
            ? data.detail.map((d) => d.msg || JSON.stringify(d)).join(", ")
            : String(data.detail);
        }
      } catch (_) {}
      els.configError.textContent = msg;
      els.configError.hidden = false;
      return;
    }
    closeConfigModal();
    fetchStatus();
  } catch (err) {
    els.configError.textContent = `Network error: ${err}`;
    els.configError.hidden = false;
  } finally {
    els.configSave.disabled = false;
  }
});

els.toggleBtn.addEventListener("click", async () => {
  const url = isRunning ? "/api/listener/stop" : "/api/listener/start";
  els.toggleBtn.disabled = true;
  try {
    await fetch(url, { method: "POST" });
    await fetchStatus();
  } finally {
    els.toggleBtn.disabled = false;
  }
});

fetchStatus();
connectWs();
setInterval(fetchStatus, 2000);

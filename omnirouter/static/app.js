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
  line.className = `log-line level-${entry.level}`;
  line.innerHTML =
    `<span class="ts">${escapeHtml(entry.ts)}</span>` +
    `<span class="lvl">${escapeHtml(entry.level)}</span>` +
    `<span class="msg"> - ${escapeHtml(entry.message)}</span>`;
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
  els.toggleBtn.classList.toggle("is-stopped", !running);
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
    const d = s.destination;
    els.destSummary.textContent =
      `${d.aet}@${d.host}:${d.port}` +
      (d.use_tls
        ? `  (TLS${d.verify_peer ? ", peer verified" : ", peer not verified"})`
        : "  (plain)");

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
    els.connState.textContent = "Live";
    els.connState.classList.add("is-up");
    els.connState.classList.remove("is-down");
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
    els.connState.textContent = "Disconnected — reconnecting…";
    els.connState.classList.remove("is-up");
    els.connState.classList.add("is-down");
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

// OmniPACS Relay dashboard — live status + log streaming via WebSocket.

const els = {
  logArea: document.getElementById("log-area"),
  publicHost: document.getElementById("public-host"),
  publicPort: document.getElementById("public-port"),
  stowEndpoint: document.getElementById("stow-endpoint"),
  spoolPath: document.getElementById("spool-path"),
  targetSummary: document.getElementById("target-summary"),
  toggleBtn: document.getElementById("toggle-forwarder"),
  clearBtn: document.getElementById("clear-log"),
  connState: document.getElementById("conn-state"),
  connStateLabel: document.querySelector("#conn-state .conn-label"),
  // Stats
  statRecv: document.getElementById("stat-recv"),
  statFwd: document.getElementById("stat-fwd"),
  statFail: document.getElementById("stat-fail"),
  statQueue: document.getElementById("stat-queue"),
  statQuar: document.getElementById("stat-quar"),
  statTokens: document.getElementById("stat-tokens"),
  // Local Target modal
  menuTarget: document.getElementById("menu-target"),
  openTargetBtn: document.getElementById("open-target"),
  targetOverlay: document.getElementById("target-overlay"),
  targetClose: document.getElementById("target-close"),
  targetCancel: document.getElementById("target-cancel"),
  targetForm: document.getElementById("target-form"),
  targetSave: document.getElementById("target-save"),
  targetError: document.getElementById("target-error"),
  targetHost: document.getElementById("target-host"),
  targetPort: document.getElementById("target-port"),
  targetAet: document.getElementById("target-aet"),
  targetDelivery: document.getElementById("target-delivery"),
  // Issue Token modal
  openIssueBtn: document.getElementById("open-issue-token"),
  issueOverlay: document.getElementById("issue-overlay"),
  issueClose: document.getElementById("issue-close"),
  issueCancel: document.getElementById("issue-cancel"),
  issueForm: document.getElementById("issue-form"),
  issueSave: document.getElementById("issue-save"),
  issueLabel: document.getElementById("issue-label"),
  issueError: document.getElementById("issue-error"),
  issueResult: document.getElementById("issue-result"),
  issueToken: document.getElementById("issue-token"),
  issueCopy: document.getElementById("issue-copy"),
  // Tables
  tokenRows: document.getElementById("token-rows"),
  quarRows: document.getElementById("quar-rows"),
  requeueAllBtn: document.getElementById("requeue-all"),
};

let isRunning = true;
let ws = null;
let reconnectTimer = null;

// --- Logging ----------------------------------------------------------------
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

  while (els.logArea.childElementCount > 1500) {
    els.logArea.removeChild(els.logArea.firstChild);
  }
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

function setConn(state, label) {
  els.connState.classList.toggle("is-up", state === "up");
  els.connState.classList.toggle("is-down", state === "down");
  if (els.connStateLabel) els.connStateLabel.textContent = label;
}

function setRunning(running) {
  isRunning = running;
  els.toggleBtn.textContent = running ? "Stop" : "Start";
  els.toggleBtn.classList.toggle("btn-stop", running);
  els.toggleBtn.classList.toggle("btn-start", !running);
}

function describeTarget(t) {
  return `${t.aet}@${t.host}:${t.port}  (default: ${t.default_delivery_mode})`;
}

function formatTs(ts) {
  if (!ts) return "—";
  try {
    const d = new Date(ts * 1000);
    return d.toLocaleString();
  } catch (e) {
    return "—";
  }
}

// --- Status polling ---------------------------------------------------------
async function fetchStatus() {
  try {
    const r = await fetch("/api/status");
    if (!r.ok) return;
    const s = await r.json();
    els.publicHost.textContent = s.public_host;
    els.publicPort.textContent = s.public_port;
    els.stowEndpoint.textContent = `POST  /studies  (or /studies/<UID>)`;
    els.spoolPath.textContent = s.spool_path;
    els.targetSummary.textContent = describeTarget(s.local_target);
    els.statRecv.textContent = s.spool.received;
    els.statFwd.textContent = s.spool.forwarded;
    els.statFail.textContent = s.spool.failed;
    els.statQueue.textContent = s.spool.queue_depth;
    els.statQuar.textContent = s.spool.quarantined;
    els.statTokens.textContent = s.token_count;
    setRunning(s.forwarder_running);
  } catch (e) {
    /* ws handles connectivity */
  }
}

// --- Tokens ----------------------------------------------------------------
async function fetchTokens() {
  try {
    const r = await fetch("/api/tokens");
    if (!r.ok) return;
    const d = await r.json();
    if (!d.tokens || d.tokens.length === 0) {
      els.tokenRows.innerHTML =
        '<tr class="empty"><td colspan="4">No tokens issued yet.</td></tr>';
      return;
    }
    els.tokenRows.innerHTML = d.tokens.map((t) => `
      <tr>
        <td class="mono">${escapeHtml(t.label)}</td>
        <td>${formatTs(t.created_ts)}</td>
        <td>${formatTs(t.last_used_ts)}</td>
        <td class="col-actions">
          <button class="btn-revoke" data-label="${escapeHtml(t.label)}">
            Revoke
          </button>
        </td>
      </tr>
    `).join("");
    for (const btn of els.tokenRows.querySelectorAll(".btn-revoke")) {
      btn.addEventListener("click", () => revokeToken(btn.dataset.label));
    }
  } catch (e) {}
}

async function revokeToken(label) {
  if (!confirm(`Revoke token "${label}"? Remote callers using it will get 401.`)) {
    return;
  }
  await fetch(`/api/tokens/${encodeURIComponent(label)}`, { method: "DELETE" });
  await fetchTokens();
  await fetchStatus();
}

// --- Quarantine ------------------------------------------------------------
async function fetchQuarantine() {
  try {
    const r = await fetch("/api/quarantine");
    if (!r.ok) return;
    const d = await r.json();
    if (!d.items || d.items.length === 0) {
      els.quarRows.innerHTML =
        '<tr class="empty"><td colspan="4">Nothing quarantined.</td></tr>';
      return;
    }
    els.quarRows.innerHTML = d.items.map((q) => `
      <tr>
        <td class="mono">${escapeHtml(q.study_uid)}</td>
        <td class="mono">${escapeHtml(q.sop_uid)}</td>
        <td>${escapeHtml(q.last_error || "")}</td>
        <td class="col-actions">
          <button class="btn-requeue"
                  data-study="${escapeHtml(q.study_uid)}"
                  data-sop="${escapeHtml(q.sop_uid)}">
            Requeue
          </button>
        </td>
      </tr>
    `).join("");
    for (const btn of els.quarRows.querySelectorAll(".btn-requeue")) {
      btn.addEventListener("click", async () => {
        await fetch(
          `/api/quarantine/${encodeURIComponent(btn.dataset.study)}/${encodeURIComponent(btn.dataset.sop)}/requeue`,
          { method: "POST" },
        );
        await Promise.all([fetchQuarantine(), fetchStatus()]);
      });
    }
  } catch (e) {}
}

els.requeueAllBtn.addEventListener("click", async () => {
  if (!confirm("Requeue every quarantined instance for another forward attempt?")) {
    return;
  }
  await fetch("/api/quarantine/requeue-all", { method: "POST" });
  await Promise.all([fetchQuarantine(), fetchStatus()]);
});

// --- WebSocket logs --------------------------------------------------------
function connectWs() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws/logs`);

  ws.addEventListener("open", () => setConn("up", "Live"));
  ws.addEventListener("message", (ev) => {
    const data = JSON.parse(ev.data);
    if (data.type === "snapshot") replaceLog(data.entries);
    else if (data.type === "entry") appendLogLine(data.entry);
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
    try { ws.close(); } catch (_) {}
  });
}

els.clearBtn.addEventListener("click", async () => {
  await fetch("/api/logs/clear", { method: "POST" });
  els.logArea.innerHTML = "";
});

// --- Local Target modal ----------------------------------------------------
function openTargetModal() {
  fetch("/api/local-target")
    .then((r) => r.json())
    .then((t) => {
      els.targetHost.value = t.host || "";
      els.targetPort.value = t.port || "";
      els.targetAet.value = t.aet || "";
      els.targetDelivery.value = t.default_delivery_mode || "sync";
      els.targetError.hidden = true;
      els.targetError.textContent = "";
      els.targetOverlay.hidden = false;
      els.targetHost.focus();
    });
}

function closeTargetModal() {
  els.targetOverlay.hidden = true;
}

els.menuTarget.addEventListener("click", openTargetModal);
els.openTargetBtn.addEventListener("click", openTargetModal);
els.targetClose.addEventListener("click", closeTargetModal);
els.targetCancel.addEventListener("click", closeTargetModal);
els.targetOverlay.addEventListener("click", (e) => {
  if (e.target === els.targetOverlay) closeTargetModal();
});

els.targetForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const host = String(els.targetHost.value || "").trim();
  const port = Number(els.targetPort.value);
  const aet = String(els.targetAet.value || "").trim();
  const dm = String(els.targetDelivery.value || "sync");
  if (!host) {
    els.targetError.textContent = "Target host is required.";
    els.targetError.hidden = false;
    return;
  }
  if (!Number.isInteger(port) || port < 1 || port > 65535) {
    els.targetError.textContent = "Port must be 1..65535.";
    els.targetError.hidden = false;
    return;
  }
  if (!aet) {
    els.targetError.textContent = "AE Title is required.";
    els.targetError.hidden = false;
    return;
  }
  els.targetSave.disabled = true;
  try {
    const r = await fetch("/api/local-target", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        host, port, aet, default_delivery_mode: dm,
      }),
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
      els.targetError.textContent = msg;
      els.targetError.hidden = false;
      return;
    }
    closeTargetModal();
    await fetchStatus();
  } finally {
    els.targetSave.disabled = false;
  }
});

// --- Issue Token modal -----------------------------------------------------
function openIssueModal() {
  els.issueLabel.value = "";
  els.issueError.hidden = true;
  els.issueError.textContent = "";
  els.issueResult.hidden = true;
  els.issueToken.textContent = "";
  els.issueSave.disabled = false;
  els.issueSave.hidden = false;
  els.issueOverlay.hidden = false;
  els.issueLabel.focus();
}

function closeIssueModal() {
  els.issueOverlay.hidden = true;
}

els.openIssueBtn.addEventListener("click", openIssueModal);
els.issueClose.addEventListener("click", closeIssueModal);
els.issueCancel.addEventListener("click", closeIssueModal);
els.issueOverlay.addEventListener("click", (e) => {
  if (e.target === els.issueOverlay) closeIssueModal();
});

els.issueForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  els.issueSave.disabled = true;
  els.issueError.hidden = true;
  try {
    const label = String(els.issueLabel.value || "").trim();
    const r = await fetch("/api/tokens", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ label: label || null }),
    });
    if (!r.ok) {
      let msg = `Issue failed (${r.status})`;
      try {
        const data = await r.json();
        if (data && data.detail) {
          msg = Array.isArray(data.detail)
            ? data.detail.map((d) => d.msg || JSON.stringify(d)).join(", ")
            : String(data.detail);
        }
      } catch (_) {}
      els.issueError.textContent = msg;
      els.issueError.hidden = false;
      els.issueSave.disabled = false;
      return;
    }
    const data = await r.json();
    els.issueToken.textContent = data.token;
    els.issueResult.hidden = false;
    els.issueSave.hidden = true; // hide "Generate" once we've got one
    await fetchTokens();
    await fetchStatus();
  } catch (err) {
    els.issueError.textContent = `Network error: ${err}`;
    els.issueError.hidden = false;
    els.issueSave.disabled = false;
  }
});

els.issueCopy.addEventListener("click", async () => {
  const tok = els.issueToken.textContent;
  if (!tok) return;
  try {
    await navigator.clipboard.writeText(tok);
    els.issueCopy.textContent = "Copied";
    setTimeout(() => (els.issueCopy.textContent = "Copy"), 1500);
  } catch (e) {
    els.issueCopy.textContent = "Copy failed";
  }
});

document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  if (!els.targetOverlay.hidden) closeTargetModal();
  if (!els.issueOverlay.hidden) closeIssueModal();
});

// --- Forwarder start/stop --------------------------------------------------
els.toggleBtn.addEventListener("click", async () => {
  const url = isRunning ? "/api/forwarder/stop" : "/api/forwarder/start";
  els.toggleBtn.disabled = true;
  try {
    await fetch(url, { method: "POST" });
    await fetchStatus();
  } finally {
    els.toggleBtn.disabled = false;
  }
});

// --- Boot ------------------------------------------------------------------
fetchStatus();
fetchTokens();
fetchQuarantine();
connectWs();
setInterval(fetchStatus, 2000);
setInterval(fetchTokens, 4000);
setInterval(fetchQuarantine, 4000);

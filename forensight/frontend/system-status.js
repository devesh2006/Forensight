import { api } from "./api.js";

const $ = selector => document.querySelector(selector);
const localNames = new Set(["localhost", "127.0.0.1", "::1"]);
const apiUrl = new URL(window.FORENSIGHT_CONFIG?.apiUrl || window.location.origin, window.location.href);
const localBackend = localNames.has(apiUrl.hostname.toLowerCase());
const state = {
  backend: "connecting",
  backendError: "",
  case: "unknown",
  activeCase: null,
  evidenceCount: 0,
  databaseType: null,
  applicationMode: "unknown",
  analysis: "idle",
  analysisMessage: "",
  lastUpdated: null,
  browserOnline: navigator.onLine
};
const listeners = new Set();
let requestInFlight = false;
let retryHandler = null;
let completeTimer = null;

function getDisplayStatus() {
  if (state.backend === "connecting") return { key: "connecting", glyph: "●", text: "CONNECTING..." };
  if (state.backend !== "connected") return { key: "offline", glyph: "○", text: "BACKEND OFFLINE" };
  if (state.analysis === "importing") return { key: "importing", glyph: "↑", text: "IMPORTING EVIDENCE" };
  if (state.analysis === "validating") return { key: "validating", glyph: "◌", text: "VALIDATING EVIDENCE" };
  if (state.analysis === "running") return { key: "running", glyph: "◌", text: "ANALYSIS IN PROGRESS" };
  if (state.analysis === "failed") return { key: "failed", glyph: "!", text: "ANALYSIS FAILED" };
  if (state.case !== "loaded") return { key: "empty", glyph: "○", text: "NO CASE DATA" };
  if (state.applicationMode === "offline" || (localBackend && !state.browserOnline)) return { key: "local-offline", glyph: "◉", text: "LOCAL • OFFLINE" };
  if (state.analysis === "complete") return { key: "complete", glyph: "✓", text: "ANALYSIS COMPLETE" };
  return { key: "connected", glyph: "✓", text: "CASE DATA CONNECTED" };
}

function snapshot() {
  const display = getDisplayStatus();
  return {
    ...state,
    mode: state.applicationMode === "offline" ? "LOCAL · OFFLINE" : localBackend ? (state.browserOnline ? "LOCAL · ONLINE" : "LOCAL · OFFLINE") : (state.browserOnline ? "ONLINE" : "OFFLINE"),
    displayStatus: display.text,
    displayKey: display.key,
    displayGlyph: display.glyph
  };
}

function publish() {
  const current = snapshot();
  listeners.forEach(listener => listener(current));
}

export function subscribeSystemStatus(listener) {
  listeners.add(listener);
  listener(snapshot());
  return () => listeners.delete(listener);
}

export function setAnalysisStatus(phase, message = "") {
  clearTimeout(completeTimer);
  state.analysis = phase;
  state.analysisMessage = message;
  publish();
  if (phase === "complete") {
    completeTimer = setTimeout(() => {
      if (state.analysis === "complete") {
        state.analysis = "idle";
        state.analysisMessage = "";
        publish();
      }
    }, 6000);
  }
}

export function registerStatusRetry(handler) {
  retryHandler = handler;
}

export async function refreshSystemStatus({ connecting = false } = {}) {
  if (requestInFlight) return snapshot();
  requestInFlight = true;
  if (connecting) {
    state.backend = "connecting";
    state.backendError = "";
    publish();
  }
  try {
    const health = await api("/api/health");
    if (health.status !== "ok" || health.database !== "connected") throw new Error("The backend health check did not pass.");
    state.backend = "connected";
    state.backendError = "";
    state.activeCase = health.active_case || health.case || null;
    state.evidenceCount = Number(health.evidence_count || 0);
    state.case = state.activeCase && state.evidenceCount > 0 ? "loaded" : "empty";
    state.databaseType = health.database_type || "database";
    state.applicationMode = health.mode || "unknown";
    state.lastUpdated = Date.now();
  } catch (error) {
    state.backend = "offline";
    state.backendError = error.message || "The backend is unavailable.";
    state.lastUpdated = Date.now();
  } finally {
    requestInFlight = false;
    publish();
  }
  return snapshot();
}

function renderStatus(status) {
  const button = $("#statusIndicator");
  button.dataset.state = status.displayKey;
  button.title = status.analysisMessage || status.backendError || status.displayStatus;
  $("#statusGlyph").textContent = status.displayGlyph;
  $("#statusText").textContent = status.displayStatus;
  $("#detailBackend").textContent = status.backend === "connected" ? "✓ Connected" : status.backend === "connecting" ? "● Connecting…" : "○ Unavailable";
  $("#detailDatabase").textContent = status.databaseType ? status.databaseType.toUpperCase() + " · Connected" : (status.backend === "offline" ? "Unavailable" : "Checking…");
  $("#detailCase").textContent = status.case === "loaded" ? status.activeCase : status.case === "empty" ? "None loaded" : "Checking…";
  $("#detailEvidence").textContent = status.backend === "connected" ? status.evidenceCount.toLocaleString() + " artifacts" : "Unavailable";
  $("#detailAnalysis").textContent = ({
    idle: status.case === "loaded" ? "Ready" : "Waiting for case data",
    importing: "↑ Importing evidence",
    validating: "◌ Validating evidence",
    running: "◌ Running case analysis",
    complete: "✓ Complete",
    failed: "! Failed"
  })[status.analysis];
  $("#detailMode").textContent = status.mode;
  $("#detailUpdated").textContent = status.lastUpdated ? new Intl.DateTimeFormat("en-IN", { dateStyle: "medium", timeStyle: "medium" }).format(status.lastUpdated) : "—";

  const message = $("#statusMessage");
  const detail = status.backendError || status.analysisMessage;
  message.textContent = detail;
  message.classList.toggle("hidden", !detail);
  message.classList.toggle("is-error", Boolean(status.backendError) || status.analysis === "failed");
  const retry = $("#statusRetry");
  retry.classList.toggle("hidden", status.backend === "connected" && status.analysis !== "failed");
  retry.textContent = status.analysis === "failed" ? "Retry analysis" : "Reconnect";
}

function togglePopover(open) {
  const panel = $("#statusPopover");
  const shouldOpen = open ?? panel.classList.contains("hidden");
  panel.classList.toggle("hidden", !shouldOpen);
  $("#statusIndicator").setAttribute("aria-expanded", String(shouldOpen));
}

$("#statusIndicator").addEventListener("click", () => togglePopover());
$("#statusClose").addEventListener("click", () => togglePopover(false));
$("#statusRetry").addEventListener("click", async () => {
  if (retryHandler && state.analysis === "failed") await retryHandler();
  else await refreshSystemStatus({ connecting: true });
});
document.addEventListener("click", event => {
  if (!event.target.closest(".status-wrap")) togglePopover(false);
});
document.addEventListener("keydown", event => {
  if (event.key === "Escape") togglePopover(false);
});

subscribeSystemStatus(renderStatus);

export function startSystemStatus() {
  state.browserOnline = navigator.onLine;
  refreshSystemStatus({ connecting: true });
  window.addEventListener("online", () => {
    state.browserOnline = true;
    publish();
    refreshSystemStatus({ connecting: true });
  });
  window.addEventListener("offline", () => {
    state.browserOnline = false;
    publish();
    // A local API can remain reachable after the browser reports no internet.
    refreshSystemStatus();
  });
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") refreshSystemStatus();
  });
  window.setInterval(() => {
    if (document.visibilityState === "visible") refreshSystemStatus();
  }, 30000);
}


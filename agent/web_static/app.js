(function () {
  "use strict";

  const POLL_RUNNING_MS = 1000;
  const POLL_ACTIVE_MS = 2000;
  const POLL_IDLE_MS = 5000;
  const POLL_FAILURE_MAX_MS = 10000;
  const TEXT_LIMIT = 600;

  const elements = {};
  let controllerToken = "";
  let pollingStopped = false;
  let stateRequestInFlight = false;
  let pollTimer = null;
  let transientFailureDelay = 0;
  let currentStatePayload = null;
  let startRequestInFlight = false;
  let approvalRequestInFlight = false;
  let tickRequestInFlight = false;

  document.addEventListener("DOMContentLoaded", () => {
    collectElements();
    wireEvents();
    controllerToken = captureTokenFromFragment();
    if (!controllerToken) {
      setConnectionState("token-missing", "Token missing");
      setMutationDisabled(true);
      return;
    }
    setConnectionState("connecting", "Connecting");
    refreshCurrentState();
  });

  function collectElements() {
    for (const id of [
      "connection-status",
      "repository-path",
      "initial-task",
      "sandbox-select",
      "start-button",
      "startup-status",
      "run-id",
      "run-repository",
      "run-sandbox",
      "run-status",
      "controller-state",
      "action-running",
      "current-stage",
      "planner-action",
      "planner-reason",
      "actionable-error",
      "latest-codex-summary",
      "latest-chatgpt-submission-summary",
      "latest-chatgpt-capture-summary",
      "latest-prompt-extraction-summary",
      "latest-governance-summary",
      "approval-panel",
      "approval-kind",
      "approve-button",
      "reject-button",
      "approval-status",
      "progress-panel",
      "progress-description",
      "tick-button",
      "tick-status",
      "terminal-status",
      "event-timeline",
    ]) {
      elements[id] = document.getElementById(id);
    }
  }

  function wireEvents() {
    elements["start-button"].addEventListener("click", onStartRun);
    elements["approve-button"].addEventListener("click", () => onApproval("approved"));
    elements["reject-button"].addEventListener("click", () => onApproval("rejected"));
    elements["tick-button"].addEventListener("click", onTick);
  }

  function captureTokenFromFragment() {
    const hash = window.location.hash || "";
    let token = "";
    if (hash.startsWith("#token=") && hash.indexOf("&") === -1) {
      token = hash.slice("#token=".length);
    }
    if (hash) {
      history.replaceState(null, "", window.location.pathname + window.location.search);
    }
    return token;
  }

  async function getCurrentState() {
    return requestJson("GET", "/api/runs/current");
  }

  async function startRun(payload) {
    return requestJson("POST", "/api/runs/start", payload);
  }

  async function submitApproval(decision) {
    return requestJson("POST", "/api/approval", { decision });
  }

  async function requestTick() {
    return requestJson("POST", "/api/tick", {});
  }

  async function requestJson(method, path, payload) {
    const headers = { "X-Controller-Token": controllerToken };
    const options = {
      method,
      headers,
      cache: "no-store",
      credentials: "omit",
    };
    if (payload !== undefined) {
      headers["Content-Type"] = "application/json";
      options.body = JSON.stringify(payload);
    }
    let response;
    try {
      response = await fetch(path, options);
    } catch (error) {
      return {
        ok: false,
        status: 0,
        reason_code: "temporary_read_failure",
        error_message: "Temporary connection failure.",
      };
    }
    const data = await readJsonResponse(response);
    if (response.status === 401) {
      pollingStopped = true;
      clearPollTimer();
      setConnectionState("auth-failed", "API authentication failed");
      setMutationDisabled(true);
    }
    return {
      ok: response.ok && Boolean(data.ok),
      status: response.status,
      ...data,
    };
  }

  async function readJsonResponse(response) {
    try {
      const data = await response.json();
      if (data && typeof data === "object") {
        return data;
      }
    } catch (error) {
      return {
        ok: false,
        reason_code: "invalid_response",
        error_message: "Server returned an invalid response.",
      };
    }
    return {
      ok: false,
      reason_code: "invalid_response",
      error_message: "Server returned an invalid response.",
    };
  }

  async function refreshCurrentState() {
    if (pollingStopped || stateRequestInFlight || !controllerToken) {
      return;
    }
    stateRequestInFlight = true;
    const result = await getCurrentState();
    stateRequestInFlight = false;
    if (pollingStopped) {
      return;
    }
    if (result.status === 401) {
      return;
    }
    if (!result.ok && result.reason_code === "temporary_read_failure") {
      setConnectionState("temporary-failure", "Temporary polling failure");
      transientFailureDelay = Math.min(
        transientFailureDelay ? transientFailureDelay * 2 : 2000,
        POLL_FAILURE_MAX_MS,
      );
      scheduleNextPoll(transientFailureDelay);
      return;
    }
    transientFailureDelay = 0;
    currentStatePayload = result;
    setConnectionState("connected", "Connected");
    renderState(result);
    scheduleNextPoll(nextPollInterval(result));
  }

  function scheduleNextPoll(delay) {
    clearPollTimer();
    if (!pollingStopped) {
      pollTimer = window.setTimeout(refreshCurrentState, delay);
    }
  }

  function clearPollTimer() {
    if (pollTimer !== null) {
      window.clearTimeout(pollTimer);
      pollTimer = null;
    }
  }

  function nextPollInterval(payload) {
    const model = payload.state || null;
    const runtime = model && model.controller_runtime ? model.controller_runtime : {};
    if (runtime.action_running === true) {
      return POLL_RUNNING_MS;
    }
    if (runtime.active_run_id || (model && model.run_id)) {
      return POLL_ACTIVE_MS;
    }
    return POLL_IDLE_MS;
  }

  async function onStartRun() {
    if (startRequestInFlight || !controllerToken) {
      return;
    }
    const repositoryPath = elements["repository-path"].value.trim();
    const initialInstruction = elements["initial-task"].value.trim();
    const sandbox = elements["sandbox-select"].value;
    if (!repositoryPath) {
      setText(elements["startup-status"], "Repository path is required.");
      return;
    }
    if (!initialInstruction) {
      setText(elements["startup-status"], "Initial task is required.");
      return;
    }
    startRequestInFlight = true;
    updateControlState();
    setText(elements["startup-status"], "Starting run...");
    const result = await startRun({
      repository_path: repositoryPath,
      initial_instruction: initialInstruction,
      sandbox,
    });
    startRequestInFlight = false;
    setText(elements["startup-status"], result.ok ? "Run started." : safeMessage(result));
    if (result.status !== 401) {
      await forceRefresh();
    }
    updateControlState();
  }

  async function onApproval(decision) {
    if (approvalRequestInFlight || !controllerToken) {
      return;
    }
    approvalRequestInFlight = true;
    updateControlState();
    setText(elements["approval-status"], decision === "approved" ? "Approving..." : "Rejecting...");
    const result = await submitApproval(decision);
    approvalRequestInFlight = false;
    setText(elements["approval-status"], result.ok ? "Decision submitted." : safeMessage(result));
    if (result.status !== 401) {
      await forceRefresh();
    }
    updateControlState();
  }

  async function onTick() {
    if (tickRequestInFlight || !controllerToken) {
      return;
    }
    tickRequestInFlight = true;
    updateControlState();
    setText(elements["tick-status"], "Requesting progress...");
    const result = await requestTick();
    tickRequestInFlight = false;
    setText(elements["tick-status"], result.ok ? "Progress requested." : safeMessage(result));
    if (result.status !== 401) {
      await forceRefresh();
    }
    updateControlState();
  }

  async function forceRefresh() {
    clearPollTimer();
    stateRequestInFlight = false;
    await refreshCurrentState();
  }

  function renderState(payload) {
    const model = payload.state || null;
    const runtime = model && model.controller_runtime ? model.controller_runtime : {};
    const activeRunId = valueOrNone(model && model.run_id ? model.run_id : runtime.active_run_id);

    setText(elements["run-id"], activeRunId);
    setText(elements["run-repository"], valueOrNone(model && model.repository_path));
    setText(elements["run-sandbox"], valueOrNone(model && model.sandbox));
    setText(elements["run-status"], valueOrNone(model && model.run_status));
    setText(elements["controller-state"], valueOrNone(runtime.controller_state || payload.controller_state));
    setText(elements["action-running"], runtime.action_running ? runningLabel(runtime) : "Not running");
    setText(elements["current-stage"], valueOrNone(model && model.current_stage));
    setText(elements["planner-action"], valueOrNone(model && model.planner_action));
    setText(elements["planner-reason"], valueOrNone(model && model.planner_reason_code));
    setText(elements["actionable-error"], valueOrNone(model && model.actionable_error_message));

    setSummary("latest-codex-summary", model && model.latest_codex_result);
    setSummary("latest-chatgpt-submission-summary", model && model.latest_chatgpt_submission);
    setSummary("latest-chatgpt-capture-summary", model && model.latest_chatgpt_capture);
    setSummary("latest-prompt-extraction-summary", model && model.latest_prompt_extraction);
    setSummary("latest-governance-summary", model && model.latest_governance);

    renderApproval(model, runtime);
    renderProgress(model, runtime);
    renderTerminal(model, runtime);
    renderTimeline(model && Array.isArray(model.event_timeline) ? model.event_timeline : []);
    updateControlState();
  }

  function renderApproval(model, runtime) {
    const required = Boolean(model && model.requires_human_approval);
    elements["approval-panel"].classList.toggle("hidden", !required);
    const kind = model && model.approval_kind ? model.approval_kind.replaceAll("_", " ") : "approval";
    setText(elements["approval-kind"], required ? `Approval required: ${kind}` : "No approval is pending.");
    const disabled = !required || runtime.action_running || approvalRequestInFlight || !controllerToken;
    elements["approve-button"].disabled = disabled;
    elements["reject-button"].disabled = disabled;
  }

  function renderProgress(model, runtime) {
    const available = Boolean(
      model &&
        model.routine_action_available &&
        !model.requires_human_approval &&
        !model.terminal &&
        !model.blocked &&
        !model.completed &&
        !runtime.action_running,
    );
    elements["progress-panel"].classList.toggle("hidden", !available);
    elements["tick-button"].disabled = !available || tickRequestInFlight || !controllerToken;
  }

  function renderTerminal(model, runtime) {
    const label = terminalLabel(model, runtime);
    setText(elements["terminal-status"], label.text);
    elements["terminal-status"].className = `status-badge ${label.className}`;
  }

  function renderTimeline(events) {
    const list = elements["event-timeline"];
    list.replaceChildren();
    for (const event of events) {
      const item = document.createElement("li");
      item.className = "timeline-item";

      const meta = document.createElement("div");
      meta.className = "timeline-meta";
      appendSpan(meta, `#${valueOrNone(event.event_id)}`);
      appendSpan(meta, valueOrNone(event.timestamp));
      appendSpan(meta, valueOrNone(event.event_type));

      const message = document.createElement("p");
      setText(message, valueOrNone(event.message));

      const preview = document.createElement("pre");
      setText(preview, boundedJson(event.metadata_preview));

      item.append(meta, message, preview);
      list.append(item);
    }
  }

  function updateControlState() {
    const model = currentStatePayload && currentStatePayload.state ? currentStatePayload.state : null;
    const runtime = model && model.controller_runtime ? model.controller_runtime : {};
    const activeRun = Boolean((model && model.run_id) || runtime.active_run_id);
    const running = Boolean(runtime.action_running);
    const disableStart = !controllerToken || activeRun || running || startRequestInFlight;
    elements["repository-path"].disabled = disableStart;
    elements["initial-task"].disabled = disableStart;
    elements["sandbox-select"].disabled = disableStart;
    elements["start-button"].disabled = disableStart;

    if (model) {
      renderApproval(model, runtime);
      renderProgress(model, runtime);
    } else {
      elements["approve-button"].disabled = true;
      elements["reject-button"].disabled = true;
      elements["tick-button"].disabled = true;
    }
  }

  function setMutationDisabled(disabled) {
    for (const id of [
      "repository-path",
      "initial-task",
      "sandbox-select",
      "start-button",
      "approve-button",
      "reject-button",
      "tick-button",
    ]) {
      elements[id].disabled = disabled;
    }
  }

  function setSummary(id, value) {
    setText(elements[id], value ? boundedJson(value) : "None");
  }

  function boundedJson(value) {
    const text = JSON.stringify(value === undefined ? null : value, null, 2);
    if (text.length <= TEXT_LIMIT) {
      return text;
    }
    return `${text.slice(0, TEXT_LIMIT)}...`;
  }

  function safeMessage(result) {
    const reason = result && result.reason_code ? result.reason_code : "request_failed";
    const message = result && result.error_message ? result.error_message : "Request failed.";
    return `${boundedText(reason, 80)}: ${boundedText(message, 180)}`;
  }

  function terminalLabel(model, runtime) {
    if (runtime.action_running) {
      return { text: "Action currently running", className: "status-warn" };
    }
    if (model && model.requires_human_approval) {
      return { text: "Waiting for approval", className: "status-warn" };
    }
    if (runtime.controller_state === "failed") {
      return { text: "Failed", className: "status-bad" };
    }
    if (model && model.completed) {
      return { text: "Completed", className: "status-ok" };
    }
    if (model && model.blocked) {
      return { text: "Blocked", className: "status-bad" };
    }
    if (model && model.terminal) {
      return { text: "Terminal", className: "status-muted" };
    }
    if (model && model.configuration_complete === false) {
      return { text: "Configuration incomplete", className: "status-warn" };
    }
    return { text: "Idle", className: "status-muted" };
  }

  function runningLabel(runtime) {
    const kind = runtime.current_action_kind ? `: ${runtime.current_action_kind}` : "";
    return `Running${kind}`;
  }

  function setConnectionState(kind, text) {
    setText(elements["connection-status"], text);
    let className = "status-badge status-muted";
    if (kind === "connected") {
      className = "status-badge status-ok";
    } else if (kind === "auth-failed" || kind === "token-missing") {
      className = "status-badge status-bad";
    } else if (kind === "temporary-failure") {
      className = "status-badge status-warn";
    }
    elements["connection-status"].className = className;
  }

  function appendSpan(parent, text) {
    const span = document.createElement("span");
    setText(span, text);
    parent.append(span);
  }

  function valueOrNone(value) {
    if (value === null || value === undefined || value === "") {
      return "None";
    }
    return String(value);
  }

  function boundedText(value, limit) {
    const text = String(value);
    return text.length > limit ? `${text.slice(0, limit)}...` : text;
  }

  function setText(element, value) {
    element.textContent = boundedText(value, 1000);
  }
})();

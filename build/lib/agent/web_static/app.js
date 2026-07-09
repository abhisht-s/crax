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
  let profileOptions = null;
  let optionsRequestInFlight = false;
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
    refreshProfileOptions();
    refreshCurrentState();
  });

  function collectElements() {
    for (const id of [
      "connection-status",
      "repository-path",
      "initial-task",
      "project-title",
      "chat-title",
      "sandbox-select",
      "model-select",
      "reasoning-lock",
      "approval-lock",
      "start-button",
      "startup-status",
      "run-id",
      "run-repository",
      "run-project-title",
      "run-chat-title",
      "run-destination-state",
      "run-sandbox",
      "run-model",
      "run-reasoning",
      "run-approval",
      "run-profile-source",
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
    elements["repository-path"].addEventListener("input", updateControlState);
    elements["initial-task"].addEventListener("input", updateControlState);
    elements["project-title"].addEventListener("input", updateControlState);
    elements["chat-title"].addEventListener("input", updateControlState);
    elements["sandbox-select"].addEventListener("change", updateControlState);
    elements["model-select"].addEventListener("change", updateControlState);
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

  async function getProfileOptions() {
    return requestJson("GET", "/api/execution-profile/options");
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

  async function refreshProfileOptions() {
    if (optionsRequestInFlight || !controllerToken) {
      return;
    }
    optionsRequestInFlight = true;
    updateControlState();
    const result = await getProfileOptions();
    optionsRequestInFlight = false;
    if (result.ok) {
      profileOptions = result;
      populateProfileOptions(result);
      setText(elements["startup-status"], "");
    } else if (result.status !== 401) {
      profileOptions = null;
      setText(elements["startup-status"], safeMessage(result));
    }
    updateControlState();
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
    const projectTitle = elements["project-title"].value.trim();
    const chatTitle = elements["chat-title"].value.trim();
    const sandbox = elements["sandbox-select"].value;
    const model = elements["model-select"].value;
    if (!repositoryPath) {
      setText(elements["startup-status"], "Repository path is required.");
      return;
    }
    if (!initialInstruction) {
      setText(elements["startup-status"], "Initial task is required.");
      return;
    }
    if (!projectTitle || !chatTitle) {
      setText(elements["startup-status"], "ChatGPT Project and Chat titles are required.");
      return;
    }
    if (!profileSelectionValid()) {
      setText(elements["startup-status"], "Execution profile selection is unavailable.");
      return;
    }
    startRequestInFlight = true;
    updateControlState();
    setText(elements["startup-status"], "Starting run...");
    const result = await startRun({
      repository_path: repositoryPath,
      initial_instruction: initialInstruction,
      project_title: projectTitle,
      chat_title: chatTitle,
      sandbox,
      model,
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
    renderDestinationBinding(model);
    renderExecutionProfile(model);
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

  function renderExecutionProfile(model) {
    const profile = model && model.execution_profile ? model.execution_profile : null;
    if (!profile) {
      setProfileText("None", "None", "None", "None", "None");
      return;
    }
    if (profile.status === "invalid") {
      const reason = profile.reason_code || "execution_profile_invalid";
      setProfileText(
        "Invalid profile history",
        "Invalid profile history",
        "Invalid profile history",
        "Invalid profile history",
        `Blocked: ${reason}`,
      );
      return;
    }
    const source =
      profile.status === "legacy_compatibility"
        ? "Legacy/default compatibility"
        : profile.profile_source;
    setProfileText(
      valueOrNone(profile.sandbox),
      profile.model === "codex_default" ? "Codex default" : valueOrNone(profile.model),
      profile.reasoning_effort === "codex_default"
        ? "Codex default"
        : valueOrNone(profile.reasoning_effort),
      profile.approval_policy === "codex_default"
        ? "Codex default"
        : valueOrNone(profile.approval_policy),
      valueOrNone(source),
    );
  }

  function setProfileText(sandbox, model, reasoning, approval, source) {
    setText(elements["run-sandbox"], sandbox);
    setText(elements["run-model"], model);
    setText(elements["run-reasoning"], reasoning);
    setText(elements["run-approval"], approval);
    setText(elements["run-profile-source"], source);
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
    const optionsUnavailable = optionsRequestInFlight || !profileOptions;
    const invalidSelection = !profileSelectionValid();
    const requiredFieldsMissing =
      !elements["repository-path"].value.trim() ||
      !elements["initial-task"].value.trim() ||
      !elements["project-title"].value.trim() ||
      !elements["chat-title"].value.trim();
    const disableInputs = !controllerToken || activeRun || running || startRequestInFlight;
    const disableStart =
      disableInputs || optionsUnavailable || invalidSelection || requiredFieldsMissing;
    elements["repository-path"].disabled = disableInputs;
    elements["initial-task"].disabled = disableInputs;
    elements["project-title"].disabled = disableInputs;
    elements["chat-title"].disabled = disableInputs;
    elements["sandbox-select"].disabled = disableInputs || optionsUnavailable;
    elements["model-select"].disabled = disableInputs || optionsUnavailable;
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
      "project-title",
      "chat-title",
      "sandbox-select",
      "model-select",
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

  function renderDestinationBinding(model) {
    const binding = model && model.destination_binding ? model.destination_binding : null;
    if (!binding) {
      setDestinationText("None", "None", "No autonomous destination binding");
      return;
    }
    if (binding.status === "present") {
      setDestinationText(
        valueOrNone(binding.project_title),
        valueOrNone(binding.chat_title),
        "Bound and valid",
      );
      return;
    }
    if (binding.status === "missing") {
      setDestinationText("None", "None", "No autonomous destination binding");
      return;
    }
    const reason = binding.reason_code || "destination_binding_invalid";
    setDestinationText("Invalid / contradictory", "Invalid / contradictory", `Invalid / contradictory: ${reason}`);
  }

  function setDestinationText(projectTitle, chatTitle, state) {
    setText(elements["run-project-title"], projectTitle);
    setText(elements["run-chat-title"], chatTitle);
    setText(elements["run-destination-state"], state);
  }

  function populateProfileOptions(options) {
    replaceOptions(
      elements["sandbox-select"],
      Array.isArray(options.sandbox_options) ? options.sandbox_options : [],
      "read-only",
    );
    replaceOptions(
      elements["model-select"],
      Array.isArray(options.model_options) ? options.model_options : [],
      "codex_default",
    );
    const locked = options.locked && typeof options.locked === "object" ? options.locked : {};
    setText(elements["reasoning-lock"], optionLabel(locked.reasoning_effort, "Codex default"));
    setText(elements["approval-lock"], optionLabel(locked.approval_policy, "Codex default"));
  }

  function replaceOptions(select, options, preferredValue) {
    select.replaceChildren();
    for (const option of options) {
      if (!option || typeof option.value !== "string") {
        continue;
      }
      const item = document.createElement("option");
      item.value = option.value;
      setText(item, optionLabel(option, option.value));
      select.append(item);
    }
    if (select.querySelector(`option[value="${cssEscape(preferredValue)}"]`)) {
      select.value = preferredValue;
    } else if (select.options.length > 0) {
      select.selectedIndex = 0;
    }
  }

  function optionLabel(option, fallback) {
    return option && typeof option.label === "string" && option.label ? option.label : fallback;
  }

  function profileSelectionValid() {
    if (!profileOptions) {
      return false;
    }
    return (
      optionValueAllowed(profileOptions.sandbox_options, elements["sandbox-select"].value) &&
      optionValueAllowed(profileOptions.model_options, elements["model-select"].value)
    );
  }

  function optionValueAllowed(options, value) {
    return Array.isArray(options) && options.some((option) => option && option.value === value);
  }

  function cssEscape(value) {
    if (window.CSS && typeof window.CSS.escape === "function") {
      return window.CSS.escape(value);
    }
    return String(value).replace(/"/g, '\\"');
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

(function () {
  "use strict";

  const POLL_RUNNING_MS = 1000;
  const POLL_ACTIVE_MS = 2000;
  const POLL_IDLE_MS = 5000;
  const POLL_FAILURE_MAX_MS = 10000;
  const PROGRESS_POLL_MS = 2000;
  const PROGRESS_EVENT_RENDER_LIMIT = 8;
  const PROGRESS_EVENT_MEMORY_LIMIT = 50;
  const TEXT_LIMIT = 600;
  const ALLOWED_PERMISSION_PRESET_VALUES = new Set(["read-only", "workspace-write"]);
  const PERMISSION_PRESET_LABELS = {
    "read-only": "Read Only",
    "workspace-write": "Workspace Write",
  };
  const PERMISSION_PRESET_DESCRIPTIONS = {
    "read-only": "Codex can inspect the workspace. Edits are not allowed for this dashboard run.",
    "workspace-write": "Codex can edit files in this repository. Outside-workspace and dangerous access remain blocked by this dashboard run.",
  };
  const STALE_LEASE_RECOVERABLE_RUN_STATUSES = new Set([
    "completed",
    "failed",
    "rejected",
    "needs_review",
  ]);

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
  let leaseRequestInFlight = false;
  let leaseReleaseRequestInFlight = false;
  let currentLeasePayload = null;
  let displayedLeaseIdentity = "";
  let progressEvents = [];
  let progressAfterSequence = 0;
  let progressRunId = "";
  let progressStreamController = null;
  let progressStreamActive = false;
  let progressPollTimer = null;
  let progressRequestInFlight = false;

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
      "allow-destination-navigation",
      "sandbox-select",
      "permission-preset-description",
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
      "run-navigation-approved",
      "run-handoff-phase",
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
      "codex-live-state",
      "codex-live-final",
      "codex-live-error",
      "codex-live-events",
      "approval-panel",
      "approval-kind",
      "approve-button",
      "reject-button",
      "approval-status",
      "progress-panel",
      "progress-description",
      "tick-button",
      "tick-status",
      "lease-state",
      "lease-owner-run",
      "lease-owner-pid",
      "lease-acquired-at",
      "lease-active-event-id",
      "lease-token-sha256",
      "lease-owner-run-status",
      "lease-owner-pid-state",
      "lease-release-allowed",
      "lease-latest-denial",
      "lease-confirm-stale",
      "lease-release-reason",
      "lease-allow-owner-pid-alive",
      "lease-release-button",
      "lease-release-status",
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
    elements["sandbox-select"].addEventListener("change", () => {
      updatePermissionPresetDescription();
      updateControlState();
    });
    elements["model-select"].addEventListener("change", updateControlState);
    elements["approve-button"].addEventListener("click", () => onApproval("approved"));
    elements["reject-button"].addEventListener("click", () => onApproval("rejected"));
    elements["tick-button"].addEventListener("click", onTick);
    elements["lease-confirm-stale"].addEventListener("change", updateControlState);
    elements["lease-release-reason"].addEventListener("input", updateControlState);
    elements["lease-allow-owner-pid-alive"].addEventListener("change", updateControlState);
    elements["lease-release-button"].addEventListener("click", onReleaseStaleLease);
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

  async function getCurrentProgress(afterSequence) {
    const cursor = encodeURIComponent(String(afterSequence || 0));
    return requestJson("GET", `/api/runs/current/progress?after_sequence=${cursor}`);
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

  async function getChatGPTUILease() {
    return requestJson("GET", "/api/chatgpt-ui-lease");
  }

  async function releaseStaleChatGPTUILease(payload) {
    return requestJson("POST", "/api/chatgpt-ui-lease/release-stale", payload);
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
    await refreshLeaseStatus();
    if (pollingStopped) {
      return;
    }
    scheduleNextPoll(nextPollInterval(result));
  }

  async function refreshLeaseStatus() {
    if (leaseRequestInFlight || !controllerToken) {
      return;
    }
    leaseRequestInFlight = true;
    const result = await getChatGPTUILease();
    leaseRequestInFlight = false;
    if (result.status === 401) {
      return;
    }
    currentLeasePayload = result;
    renderChatGPTUILease(result);
    updateControlState();
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
    const allowDestinationNavigation = Boolean(elements["allow-destination-navigation"].checked);
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
      allow_destination_navigation: allowDestinationNavigation,
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

  async function onReleaseStaleLease() {
    if (leaseReleaseRequestInFlight || !controllerToken) {
      return;
    }
    const lease = currentChatGPTUILease();
    const reason = elements["lease-release-reason"].value.trim();
    const confirmStale = Boolean(elements["lease-confirm-stale"].checked);
    const allowOwnerPidAlive = Boolean(elements["lease-allow-owner-pid-alive"].checked);
    if (!lease || lease.status !== "active" || !lease.active) {
      setText(elements["lease-release-status"], "No active lease.");
      return;
    }
    if (!confirmStale || !reason) {
      setText(elements["lease-release-status"], "Confirmation and reason are required.");
      return;
    }

    leaseReleaseRequestInFlight = true;
    updateControlState();
    setText(
      elements["lease-release-status"],
      allowOwnerPidAlive ? "Releasing with PID-reuse override..." : "Releasing stale lease...",
    );
    const result = await releaseStaleChatGPTUILease({
      owning_run_id: lease.owning_run_id,
      owner_pid: lease.owner_pid,
      acquired_at: lease.acquired_at,
      active_event_id: lease.active_event_id,
      expected_lease_token_sha256: lease.lease_token_sha256,
      expected_run_status: lease.owning_run_status || null,
      confirm_stale: confirmStale,
      reason,
      allow_owner_pid_alive: allowOwnerPidAlive,
    });
    leaseReleaseRequestInFlight = false;
    if (result.ok) {
      const release = result.metadata && result.metadata.release ? result.metadata.release : {};
      const eventId = release.event_id ? ` Event #${release.event_id}.` : "";
      setText(elements["lease-release-status"], `Lease released.${eventId}`);
      currentLeasePayload = result;
      renderChatGPTUILease(result);
    } else if (result.status !== 401) {
      setText(elements["lease-release-status"], safeMessage(result));
      if (result.metadata && result.metadata.chatgpt_ui_lease) {
        currentLeasePayload = result;
        renderChatGPTUILease(result);
      }
    }
    if (result.status !== 401) {
      await refreshLeaseStatus();
    }
    updateControlState();
  }

  async function forceRefresh() {
    clearPollTimer();
    stateRequestInFlight = false;
    await refreshCurrentState();
    await refreshProgress();
  }

  function ensureProgressTransport(runId) {
    const normalizedRunId = runId ? String(runId) : "";
    if (!normalizedRunId) {
      stopProgressStream();
      clearProgressPollTimer();
      resetProgress("");
      renderCodexLiveProgress({});
      return;
    }
    if (normalizedRunId !== progressRunId) {
      stopProgressStream();
      clearProgressPollTimer();
      resetProgress(normalizedRunId);
      renderCodexLiveProgress({});
      startProgressStream();
      scheduleProgressPoll(PROGRESS_POLL_MS);
      return;
    }
    if (!progressStreamActive && progressPollTimer === null) {
      startProgressStream();
      scheduleProgressPoll(PROGRESS_POLL_MS);
    }
  }

  function resetProgress(runId) {
    progressRunId = runId;
    progressEvents = [];
    progressAfterSequence = 0;
  }

  function startProgressStream() {
    if (!controllerToken || !progressRunId || progressStreamController || !window.ReadableStream) {
      return;
    }
    progressStreamController = new AbortController();
    progressStreamActive = true;
    readProgressStream(progressRunId, progressAfterSequence, progressStreamController)
      .catch(() => {})
      .finally(() => {
        progressStreamActive = false;
        progressStreamController = null;
      });
  }

  function stopProgressStream() {
    if (progressStreamController) {
      progressStreamController.abort();
      progressStreamController = null;
    }
    progressStreamActive = false;
  }

  async function readProgressStream(expectedRunId, afterSequence, controller) {
    const cursor = encodeURIComponent(String(afterSequence || 0));
    const response = await fetch(`/api/runs/current/events?after_sequence=${cursor}`, {
      method: "GET",
      headers: { "X-Controller-Token": controllerToken },
      cache: "no-store",
      credentials: "omit",
      signal: controller.signal,
    });
    if (response.status === 401) {
      pollingStopped = true;
      clearPollTimer();
      clearProgressPollTimer();
      setConnectionState("auth-failed", "API authentication failed");
      setMutationDisabled(true);
      return;
    }
    if (!response.ok || !response.body) {
      return;
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const result = await reader.read();
      if (result.done) {
        buffer += decoder.decode();
        consumeProgressFrames(buffer, expectedRunId);
        return;
      }
      buffer += decoder.decode(result.value, { stream: true });
      buffer = consumeProgressFrames(buffer, expectedRunId);
    }
  }

  function consumeProgressFrames(buffer, expectedRunId) {
    let remaining = buffer;
    let boundary = remaining.indexOf("\n\n");
    while (boundary !== -1) {
      const frame = remaining.slice(0, boundary);
      remaining = remaining.slice(boundary + 2);
      handleProgressFrame(frame, expectedRunId);
      boundary = remaining.indexOf("\n\n");
    }
    return remaining;
  }

  function handleProgressFrame(frame, expectedRunId) {
    const lines = frame.split(/\r?\n/);
    let eventName = "";
    const dataLines = [];
    for (const line of lines) {
      if (!line || line.startsWith(":")) {
        continue;
      }
      if (line.startsWith("event:")) {
        eventName = line.slice("event:".length).trim();
      } else if (line.startsWith("data:")) {
        dataLines.push(line.slice("data:".length).trimStart());
      }
    }
    if (!dataLines.length) {
      return;
    }
    let payload;
    try {
      payload = JSON.parse(dataLines.join("\n"));
    } catch (error) {
      return;
    }
    if (eventName === "progress") {
      appendProgressEvent(payload, expectedRunId);
      renderCodexLiveProgress({});
    } else if (eventName === "progress_state") {
      applyProgressPayload(payload);
      renderCodexLiveProgress({});
    }
  }

  async function refreshProgress() {
    if (!controllerToken || !progressRunId || progressRequestInFlight) {
      return;
    }
    progressRequestInFlight = true;
    const result = await getCurrentProgress(progressAfterSequence);
    progressRequestInFlight = false;
    if (result.status === 401) {
      return;
    }
    if (result.ok) {
      applyProgressPayload(result);
      renderCodexLiveProgress({});
    }
    if (!progressStreamActive && progressRunId) {
      scheduleProgressPoll(PROGRESS_POLL_MS);
    }
  }

  function scheduleProgressPoll(delay) {
    clearProgressPollTimer();
    if (!pollingStopped && progressRunId) {
      progressPollTimer = window.setTimeout(refreshProgress, delay);
    }
  }

  function clearProgressPollTimer() {
    if (progressPollTimer !== null) {
      window.clearTimeout(progressPollTimer);
      progressPollTimer = null;
    }
  }

  function applyProgressPayload(payload) {
    const metadata = payload && payload.metadata && typeof payload.metadata === "object"
      ? payload.metadata
      : {};
    const progress = metadata.progress && typeof metadata.progress === "object"
      ? metadata.progress
      : null;
    if (!progress) {
      return;
    }
    const runId = progress.run_id ? String(progress.run_id) : "";
    if (runId && progressRunId && runId !== progressRunId) {
      return;
    }
    if (runId && !progressRunId) {
      progressRunId = runId;
    }
    const events = Array.isArray(progress.events) ? progress.events : [];
    for (const event of events) {
      appendProgressEvent(event, progressRunId);
    }
    if (Number.isInteger(progress.latest_sequence)) {
      progressAfterSequence = Math.max(progressAfterSequence, progress.latest_sequence);
    }
  }

  function appendProgressEvent(event, expectedRunId) {
    if (!event || typeof event !== "object") {
      return;
    }
    if (event.run_id && expectedRunId && String(event.run_id) !== String(expectedRunId)) {
      return;
    }
    const sequence = Number.isInteger(event.sequence) ? event.sequence : 0;
    if (sequence && progressEvents.some((item) => item.sequence === sequence)) {
      return;
    }
    progressEvents.push(event);
    if (sequence) {
      progressAfterSequence = Math.max(progressAfterSequence, sequence);
    }
    if (progressEvents.length > PROGRESS_EVENT_MEMORY_LIMIT) {
      progressEvents = progressEvents.slice(-PROGRESS_EVENT_MEMORY_LIMIT);
    }
  }

  function renderState(payload) {
    const model = payload.state || null;
    const runtime = model && model.controller_runtime ? model.controller_runtime : {};
    const rawActiveRunId = model && model.run_id ? model.run_id : runtime.active_run_id || "";
    const activeRunId = valueOrNone(rawActiveRunId);

    setText(elements["run-id"], activeRunId);
    setText(elements["run-repository"], valueOrNone(model && model.repository_path));
    renderDestinationBinding(model);
    renderNavigationApproval(model);
    renderHandoffPhase(model);
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

    ensureProgressTransport(rawActiveRunId);
    renderCodexLiveProgress(runtime);
    renderApproval(model, runtime);
    renderProgress(model, runtime);
    renderTerminal(model, runtime);
    renderTimeline(model && Array.isArray(model.event_timeline) ? model.event_timeline : []);
    updateControlState();
  }

  function renderChatGPTUILease(payload) {
    const lease = leaseFromPayload(payload);
    const identity = leaseIdentity(lease);
    if (identity !== displayedLeaseIdentity) {
      displayedLeaseIdentity = identity;
      elements["lease-confirm-stale"].checked = false;
      elements["lease-release-reason"].value = "";
      elements["lease-allow-owner-pid-alive"].checked = false;
    }

    if (!payload || !lease) {
      setLeaseBadge("status-bad", "Lease status unavailable");
      setLeaseDetails({
        ownerRun: "None",
        ownerPid: "None",
        acquiredAt: "None",
        eventId: "None",
        tokenSha: "None",
        runStatus: "None",
        pidState: "Unknown",
        releaseAllowed: "No",
        latestDenial: "None",
      });
      return;
    }

    if (!lease.active || lease.status === "missing") {
      setLeaseBadge("status-muted", "No active lease");
      setLeaseDetails({
        ownerRun: "None",
        ownerPid: "None",
        acquiredAt: "None",
        eventId: "None",
        tokenSha: "None",
        runStatus: "None",
        pidState: "Unknown",
        releaseAllowed: "No",
        latestDenial: latestDenialLabel(lease.latest_denial),
      });
      return;
    }

    if (lease.status === "invalid") {
      setLeaseBadge("status-bad", "Invalid lease history");
      setLeaseDetails({
        ownerRun: "None",
        ownerPid: "None",
        acquiredAt: "None",
        eventId: "None",
        tokenSha: "None",
        runStatus: "None",
        pidState: "Unknown",
        releaseAllowed: `No (${valueOrNone(lease.release_block_reason)})`,
        latestDenial: latestDenialLabel(lease.latest_denial),
      });
      return;
    }

    const pidState = valueOrNone(lease.owner_pid_state);
    const releaseAllowed = lease.release_allowed
      ? "Yes"
      : `No (${leaseBlockLabel(lease.release_block_reason)})`;
    setLeaseBadge(lease.release_allowed ? "status-warn" : "status-bad", "Active lease");
    setLeaseDetails({
      ownerRun: valueOrNone(lease.owning_run_id),
      ownerPid: valueOrNone(lease.owner_pid),
      acquiredAt: valueOrNone(lease.acquired_at),
      eventId: valueOrNone(lease.active_event_id),
      tokenSha: valueOrNone(lease.lease_token_sha256),
      runStatus: valueOrNone(lease.owning_run_status),
      pidState,
      releaseAllowed,
      latestDenial: latestDenialLabel(lease.latest_denial),
    });
  }

  function setLeaseBadge(className, text) {
    setText(elements["lease-state"], text);
    elements["lease-state"].className = `status-badge ${className}`;
  }

  function setLeaseDetails(details) {
    setText(elements["lease-owner-run"], details.ownerRun);
    setText(elements["lease-owner-pid"], details.ownerPid);
    setText(elements["lease-acquired-at"], details.acquiredAt);
    setText(elements["lease-active-event-id"], details.eventId);
    setText(elements["lease-token-sha256"], details.tokenSha);
    setText(elements["lease-owner-run-status"], details.runStatus);
    setText(elements["lease-owner-pid-state"], details.pidState);
    setText(elements["lease-release-allowed"], details.releaseAllowed);
    setText(elements["lease-latest-denial"], details.latestDenial);
  }

  function latestDenialLabel(denial) {
    if (!denial) {
      return "None";
    }
    const eventId = denial.event_id ? `#${denial.event_id}` : "unknown event";
    const runId = valueOrNone(denial.requested_owning_run_id || denial.run_id);
    const pid = valueOrNone(denial.request_owner_pid);
    const deniedAt = valueOrNone(denial.denied_at || denial.created_at);
    return `${eventId}: run ${runId}, PID ${pid}, ${deniedAt}`;
  }

  function leaseBlockLabel(reason) {
    const labels = {
      owner_pid_alive: "owner PID appears alive",
      owner_pid_unknown: "owner PID state unknown",
      owner_run_status_unknown: "owner run status unknown",
      owner_run_not_terminal: "owner run is not terminal",
      chatgpt_ui_lease_not_active: "no active lease",
    };
    return labels[reason] || valueOrNone(reason);
  }

  function leaseFromPayload(payload) {
    const metadata = payload && payload.metadata && typeof payload.metadata === "object"
      ? payload.metadata
      : {};
    const lease = metadata.chatgpt_ui_lease;
    return lease && typeof lease === "object" ? lease : null;
  }

  function currentChatGPTUILease() {
    return leaseFromPayload(currentLeasePayload);
  }

  function leaseIdentity(lease) {
    if (!lease || typeof lease !== "object") {
      return "unavailable";
    }
    if (lease.status !== "active" || !lease.active) {
      return String(lease.status || "not-active");
    }
    return [
      lease.owning_run_id,
      lease.owner_pid,
      lease.acquired_at,
      lease.active_event_id,
      lease.lease_token_sha256,
      lease.owning_run_status || "",
    ].join("|");
  }

  function renderCodexLiveProgress(runtime) {
    const latest = progressEvents.length ? progressEvents[progressEvents.length - 1] : null;
    const label = codexLiveLabel(latest, runtime || {});
    setText(elements["codex-live-state"], label.text);
    elements["codex-live-state"].className = `status-badge ${label.className}`;

    const finalEvent = latestProgressEventOfKind("final_message_available");
    if (finalEvent) {
      const metadata = finalEvent.metadata && typeof finalEvent.metadata === "object"
        ? finalEvent.metadata
        : {};
      const status = valueOrNone(metadata.final_message_status || finalEvent.status);
      const length = valueOrNone(metadata.final_message_length);
      setText(elements["codex-live-final"], `${status}, ${length} chars`);
    } else {
      setText(elements["codex-live-final"], "Not available yet");
    }

    const issue = latestIssueProgressEvent();
    setText(elements["codex-live-error"], issue ? progressEventLabel(issue) : "None");
    renderProgressEvents();
  }

  function renderProgressEvents() {
    const list = elements["codex-live-events"];
    list.replaceChildren();
    const events = progressEvents.slice(-PROGRESS_EVENT_RENDER_LIMIT);
    if (!events.length) {
      const item = document.createElement("li");
      item.className = "progress-event";
      const message = document.createElement("p");
      setText(message, progressRunId ? "Waiting for Codex progress." : "No active run.");
      item.append(message);
      list.append(item);
      return;
    }
    for (const event of events) {
      const item = document.createElement("li");
      item.className = "progress-event";

      const meta = document.createElement("div");
      meta.className = "progress-meta";
      appendSpan(meta, `#${valueOrNone(event.sequence)}`);
      appendSpan(meta, valueOrNone(event.created_at));
      appendSpan(meta, valueOrNone(event.kind));
      appendSpan(meta, valueOrNone(event.status));

      const message = document.createElement("p");
      setText(message, progressEventLabel(event));

      item.append(meta, message);
      list.append(item);
    }
  }

  function codexLiveLabel(latest, runtime) {
    if (!progressRunId) {
      return { text: "No active run", className: "status-muted" };
    }
    if (latest && latest.kind === "error") {
      return { text: "Codex error", className: "status-bad" };
    }
    if (latest && latest.kind === "blocked") {
      return { text: "Blocked", className: "status-bad" };
    }
    if (latest && latest.kind === "process_exited") {
      return {
        text: latest.status === "completed" ? "Codex exited" : "Codex exited with issue",
        className: latest.status === "completed" ? "status-ok" : "status-bad",
      };
    }
    if ((runtime && runtime.action_running) || progressStreamActive) {
      return { text: "Listening", className: "status-warn" };
    }
    if (progressEvents.length) {
      return { text: "Progress captured", className: "status-ok" };
    }
    return { text: "Waiting", className: "status-muted" };
  }

  function latestProgressEventOfKind(kind) {
    for (let index = progressEvents.length - 1; index >= 0; index -= 1) {
      if (progressEvents[index].kind === kind) {
        return progressEvents[index];
      }
    }
    return null;
  }

  function latestIssueProgressEvent() {
    for (let index = progressEvents.length - 1; index >= 0; index -= 1) {
      if (progressEvents[index].kind === "error" || progressEvents[index].kind === "blocked") {
        return progressEvents[index];
      }
    }
    return null;
  }

  function progressEventLabel(event) {
    if (!event || typeof event !== "object") {
      return "Unknown progress event";
    }
    const title = valueOrNone(event.title);
    const summary = event.summary ? `: ${event.summary}` : "";
    return `${title}${summary}`;
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
      permissionPresetSummary(profile.sandbox),
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
    elements["allow-destination-navigation"].disabled = disableInputs;
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
    updateLeaseControlState();
  }

  function updateLeaseControlState() {
    const lease = currentChatGPTUILease();
    const active = Boolean(lease && lease.status === "active" && lease.active);
    const ownerPidAlive = Boolean(active && lease.owner_pid_state === "alive");
    const overrideSelected = Boolean(elements["lease-allow-owner-pid-alive"].checked);
    const ownerRunRecoverable = Boolean(
      active && STALE_LEASE_RECOVERABLE_RUN_STATUSES.has(lease.owning_run_status),
    );
    const releaseAllowed = Boolean(
      active && (lease.release_allowed || (ownerPidAlive && overrideSelected && ownerRunRecoverable)),
    );
    const disabledBase = !controllerToken || leaseReleaseRequestInFlight || !active;
    const missingConfirmation = !elements["lease-confirm-stale"].checked;
    const missingReason = !elements["lease-release-reason"].value.trim();

    elements["lease-confirm-stale"].disabled = disabledBase;
    elements["lease-release-reason"].disabled = disabledBase;
    elements["lease-allow-owner-pid-alive"].disabled = disabledBase || !ownerPidAlive;
    if (!ownerPidAlive) {
      elements["lease-allow-owner-pid-alive"].checked = false;
    }
    elements["lease-release-button"].disabled =
      disabledBase || missingConfirmation || missingReason || !releaseAllowed;
  }

  function setMutationDisabled(disabled) {
    for (const id of [
      "repository-path",
      "initial-task",
      "project-title",
      "chat-title",
      "allow-destination-navigation",
      "sandbox-select",
      "model-select",
      "start-button",
      "approve-button",
      "reject-button",
      "tick-button",
      "lease-confirm-stale",
      "lease-release-reason",
      "lease-allow-owner-pid-alive",
      "lease-release-button",
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

  function renderNavigationApproval(model) {
    const approved = Boolean(model && model.allow_destination_navigation);
    setText(elements["run-navigation-approved"], approved ? "Operator-approved" : "Disabled");
  }

  function renderHandoffPhase(model) {
    const phase = model && model.latest_handoff_phase ? model.latest_handoff_phase : null;
    if (!phase || !phase.phase) {
      setText(elements["run-handoff-phase"], "None");
      return;
    }
    const approvedLabel = phase.navigation_operator_approved ? "operator-approved" : "navigation off";
    const outcome = phase.navigation_outcome ? `, ${phase.navigation_outcome}` : "";
    setText(elements["run-handoff-phase"], `${phase.phase} (${approvedLabel}${outcome})`);
  }

  function populateProfileOptions(options) {
    const sandboxOptions = safePermissionPresetOptions(options.sandbox_options);
    replaceOptions(
      elements["sandbox-select"],
      sandboxOptions,
      "read-only",
    );
    replaceOptions(
      elements["model-select"],
      Array.isArray(options.model_options) ? options.model_options : [],
      "codex_default",
    );
    const locked = options.locked && typeof options.locked === "object" ? options.locked : {};
    setText(elements["reasoning-lock"], optionLabel(locked.reasoning_effort, "Codex default"));
    setText(
      elements["approval-lock"],
      optionLabel(locked.approval_policy, "Codex default — not dashboard-controlled yet"),
    );
    updatePermissionPresetDescription();
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

  function optionDescription(option, fallback) {
    return option && typeof option.description === "string" && option.description ? option.description : fallback;
  }

  function safePermissionPresetOptions(options) {
    if (!Array.isArray(options)) {
      return [];
    }
    return options.filter(
      (option) =>
        option &&
        typeof option.value === "string" &&
        ALLOWED_PERMISSION_PRESET_VALUES.has(option.value),
    );
  }

  function updatePermissionPresetDescription() {
    const value = elements["sandbox-select"].value;
    const selected = safePermissionPresetOptions(profileOptions && profileOptions.sandbox_options)
      .find((option) => option.value === value);
    const fallback = PERMISSION_PRESET_DESCRIPTIONS[value] || "Select the Codex sandbox mode for this dashboard run.";
    setText(elements["permission-preset-description"], optionDescription(selected, fallback));
  }

  function permissionPresetSummary(value) {
    if (!value) {
      return "None";
    }
    const text = String(value);
    const label = PERMISSION_PRESET_LABELS[text] || text;
    return `${label} (${text})`;
  }

  function profileSelectionValid() {
    if (!profileOptions) {
      return false;
    }
    return (
      optionValueAllowed(safePermissionPresetOptions(profileOptions.sandbox_options), elements["sandbox-select"].value) &&
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

(function () {
  "use strict";

  const POLL_RUNNING_MS = 1000;
  const POLL_ACTIVE_MS = 2000;
  const POLL_IDLE_MS = 5000;
  const POLL_FAILURE_MAX_MS = 10000;
  const PROGRESS_POLL_MS = 2000;
  const PROGRESS_EVENT_RENDER_LIMIT = 8;
  const PROGRESS_EVENT_MEMORY_LIMIT = 50;
  const ALLOWED_PERMISSION_PRESET_VALUES = new Set(["read-only", "workspace-write", "danger-full-access"]);
  const PERMISSION_PRESET_LABELS = {
    "read-only": "Read Only",
    "workspace-write": "Workspace Write",
    "danger-full-access": "Full Access (Autonomous)",
  };
  const PERMISSION_PRESET_DESCRIPTIONS = {
    "read-only": "Codex can inspect the workspace. Edits are not allowed for this dashboard run.",
    "workspace-write": "Codex can edit files in this repository. Outside-workspace and dangerous access remain blocked by this dashboard run.",
    "danger-full-access": "Codex runs autonomously without filesystem, network, or prompt-policy limits. The loop does not request per-run approval.",
  };
  const STALE_LEASE_RECOVERABLE_RUN_STATUSES = new Set([
    "completed",
    "failed",
    "rejected",
    "needs_review",
  ]);
  const REPLACEABLE_RUN_STATUSES = new Set([
    "completed",
    "failed",
    "needs_review",
    "rejected",
  ]);

  const elements = {};
  let controllerToken = "";
  let authenticated = false;
  let remoteAccessEnabled = false;
  let remoteDevice = false;
  let currentPrincipal = null;
  let pendingPairingCode = "";
  let pollingStopped = false;
  let stateRequestInFlight = false;
  let pollTimer = null;
  let transientFailureDelay = 0;
  let currentStatePayload = null;
  let profileOptions = null;
  let optionsRequestInFlight = false;
  let repositoryPickerRequestInFlight = false;
  let defaultGreetingRequestInFlight = false;
  let startRequestInFlight = false;
  let approvalRequestInFlight = false;
  let tickRequestInFlight = false;
  let retryRequestInFlight = false;
  let cancelRequestInFlight = false;
  let quotaContinueRequestInFlight = false;
  let leaseRequestInFlight = false;
  let leaseReleaseRequestInFlight = false;
  let currentLeasePayload = null;
  let displayedLeaseIdentity = "";
  let progressEvents = [];
  let progressAfterSequence = 0;
  let progressRunId = "";
  let currentCodexSessionId = "";
  let currentCodexPlan = [];
  let progressStreamController = null;
  let progressStreamActive = false;
  let progressPollTimer = null;
  let progressRequestInFlight = false;

  document.addEventListener("DOMContentLoaded", async () => {
    collectElements();
    wireEvents();
    const bootstrap = captureBootstrapFromFragment();
    controllerToken = bootstrap.token;
    pendingPairingCode = bootstrap.pairingCode;
    if (pendingPairingCode) {
      elements["pairing-code"].value = pendingPairingCode;
    }
    setConnectionState("connecting", "Connecting");
    const sessionReady = await initializeSession();
    if (!sessionReady) {
      showPairingPanel();
      return;
    }
    refreshProfileOptions();
    refreshCurrentState();
    if ("serviceWorker" in navigator && window.isSecureContext) {
      navigator.serviceWorker.register("/service-worker.js").catch(() => {});
    }
  });

  function collectElements() {
    for (const id of [
      "connection-status",
      "pairing-panel",
      "pairing-code",
      "device-label",
      "pair-button",
      "pairing-status",
      "remote-device-panel",
      "refresh-devices-button",
      "remote-device-status",
      "remote-device-list",
      "startup-panel",
      "repository-path",
      "repository-browse-button",
      "repository-catalog",
      "repository-picker-status",
      "initial-task",
      "default-greeting-button",
      "default-greeting-status",
      "project-title",
      "chat-title",
      "allow-destination-navigation",
      "sandbox-select",
      "full-access-button",
      "permission-preset-description",
      "model-select",
      "reasoning-lock",
      "approval-lock",
      "full-access-confirmation-label",
      "full-access-confirmation",
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
      "codex-live-state",
      "codex-live-session-id",
      "codex-live-quota-wait",
      "quota-force-continue-button",
      "quota-force-continue-status",
      "codex-live-final",
      "codex-live-error",
      "codex-live-events",
      "codex-live-plan",
      "approval-panel",
      "approval-kind",
      "approve-button",
      "reject-button",
      "approval-status",
      "progress-panel",
      "progress-description",
      "tick-button",
      "tick-status",
      "failure-panel",
      "failure-summary",
      "failure-action",
      "failure-event-id",
      "failure-reason",
      "failure-timestamp",
      "failure-error",
      "failure-recovery",
      "retry-button",
      "retry-status",
      "cancel-run-button",
      "cancel-run-status",
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
    ]) {
      elements[id] = document.getElementById(id);
    }
  }

  function wireEvents() {
    elements["pair-button"].addEventListener("click", onPairDevice);
    elements["refresh-devices-button"].addEventListener("click", refreshRemoteDevices);
    elements["start-button"].addEventListener("click", onStartRun);
    elements["repository-browse-button"].addEventListener("click", onBrowseRepository);
    elements["repository-path"].addEventListener("input", updateControlState);
    elements["repository-catalog"].addEventListener("change", () => {
      if (elements["repository-catalog"].value) {
        elements["repository-path"].value = elements["repository-catalog"].value;
      }
      updateControlState();
    });
    elements["default-greeting-button"].addEventListener("click", onDefaultGreeting);
    elements["initial-task"].addEventListener("input", updateControlState);
    elements["project-title"].addEventListener("input", updateControlState);
    elements["chat-title"].addEventListener("input", updateControlState);
    elements["sandbox-select"].addEventListener("change", () => {
      updatePermissionPresetDescription();
      updateFullAccessConfirmation();
      updateControlState();
    });
    elements["full-access-button"].addEventListener("click", onSelectFullAccess);
    elements["model-select"].addEventListener("change", updateControlState);
    elements["full-access-confirmation"].addEventListener("input", updateControlState);
    elements["approve-button"].addEventListener("click", () => onApproval("approved"));
    elements["reject-button"].addEventListener("click", () => onApproval("rejected"));
    elements["tick-button"].addEventListener("click", onTick);
    elements["retry-button"].addEventListener("click", onRetry);
    elements["cancel-run-button"].addEventListener("click", onCancelRun);
    elements["quota-force-continue-button"].addEventListener("click", onForceQuotaContinue);
    elements["lease-confirm-stale"].addEventListener("change", updateControlState);
    elements["lease-release-reason"].addEventListener("input", updateControlState);
    elements["lease-allow-owner-pid-alive"].addEventListener("change", updateControlState);
    elements["lease-release-button"].addEventListener("click", onReleaseStaleLease);
  }

  function captureBootstrapFromFragment() {
    const hash = window.location.hash || "";
    let token = "";
    let pairingCode = "";
    if (hash.startsWith("#token=") && hash.indexOf("&") === -1) {
      token = hash.slice("#token=".length);
    } else if (hash.startsWith("#pair=") && hash.indexOf("&") === -1) {
      pairingCode = hash.slice("#pair=".length);
    }
    if (hash) {
      history.replaceState(null, "", window.location.pathname + window.location.search);
    }
    return { token, pairingCode };
  }

  async function initializeSession() {
    const result = await requestJson("GET", "/api/session", undefined, { suppressAuthFailure: true });
    if (!result.ok) {
      authenticated = false;
      setConnectionState("token-missing", "Pairing required");
      setMutationDisabled(true);
      return false;
    }
    authenticated = true;
    remoteAccessEnabled = result.remote_mode === true;
    currentPrincipal = result.principal || null;
    remoteDevice = Boolean(currentPrincipal && currentPrincipal.kind === "remote_device");
    elements["pairing-panel"].classList.add("hidden");
    elements["startup-panel"].classList.remove("remote-locked");
    setConnectionState("connected", remoteDevice ? "Remote connected" : "Connected");
    if (remoteAccessEnabled) {
      await loadRepositoryCatalog();
      elements["remote-device-panel"].classList.remove("hidden");
      await refreshRemoteDevices();
    }
    return true;
  }

  async function onPairDevice() {
    const code = elements["pairing-code"].value.trim();
    const deviceLabel = elements["device-label"].value.trim();
    if (!code || !deviceLabel) {
      setText(elements["pairing-status"], "Pairing code and device name are required.");
      return;
    }
    elements["pair-button"].disabled = true;
    setText(elements["pairing-status"], "Pairing...");
    const result = await requestJson(
      "POST",
      "/api/remote/pair",
      { code, device_label: deviceLabel },
      { suppressAuthFailure: true },
    );
    elements["pair-button"].disabled = false;
    if (!result.ok) {
      setText(elements["pairing-status"], safeMessage(result));
      return;
    }
    setText(elements["pairing-status"], "Phone paired.");
    pollingStopped = false;
    if (await initializeSession()) {
      refreshProfileOptions();
      refreshCurrentState();
    }
  }

  function showPairingPanel() {
    elements["pairing-panel"].classList.remove("hidden");
    elements["startup-panel"].classList.add("remote-locked");
  }

  async function getRemoteDevices() {
    return requestJson("GET", "/api/remote/devices");
  }

  async function revokeRemoteDevice(deviceId) {
    return requestJson("POST", "/api/remote/devices/revoke", { device_id: deviceId });
  }

  async function rotateCurrentRemoteDevice() {
    return requestJson("POST", "/api/remote/devices/rotate-current", {});
  }

  async function refreshRemoteDevices() {
    if (!authenticated || !remoteAccessEnabled) {
      return;
    }
    setText(elements["remote-device-status"], "Loading...");
    const result = await getRemoteDevices();
    if (!result.ok || !Array.isArray(result.devices)) {
      setText(elements["remote-device-status"], safeMessage(result));
      return;
    }
    renderRemoteDevices(result.devices);
    setText(elements["remote-device-status"], `${result.devices.length} device records.`);
  }

  function renderRemoteDevices(devices) {
    const list = elements["remote-device-list"];
    list.replaceChildren();
    for (const device of devices) {
      const item = document.createElement("li");
      const details = document.createElement("span");
      const current = currentPrincipal && currentPrincipal.device_id === device.id;
      const state = device.active ? "active" : "revoked or expired";
      setText(details, `${device.label || "Unnamed device"}${current ? " (this device)" : ""} — ${state}`);
      item.appendChild(details);
      if (device.active) {
        const actions = document.createElement("span");
        actions.className = "device-actions";
        if (current) {
          const rotateButton = document.createElement("button");
          rotateButton.type = "button";
          rotateButton.className = "secondary";
          setText(rotateButton, "Rotate credential");
          rotateButton.addEventListener("click", async () => {
            rotateButton.disabled = true;
            const result = await rotateCurrentRemoteDevice();
            setText(
              elements["remote-device-status"],
              result.ok ? "Credential rotated." : safeMessage(result),
            );
            rotateButton.disabled = false;
          });
          actions.appendChild(rotateButton);
        }
        const button = document.createElement("button");
        button.type = "button";
        button.className = "secondary";
        setText(button, "Revoke");
        button.addEventListener("click", async () => {
          if (!window.confirm(`Revoke ${device.label || "this device"}?`)) {
            return;
          }
          button.disabled = true;
          const result = await revokeRemoteDevice(device.id);
          setText(elements["remote-device-status"], result.ok ? "Device revoked." : safeMessage(result));
          if (current && result.ok) {
            authenticated = false;
            pollingStopped = true;
            showPairingPanel();
            return;
          }
          await refreshRemoteDevices();
        });
        actions.appendChild(button);
        item.appendChild(actions);
      }
      list.appendChild(item);
    }
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

  async function getRepositories(query) {
    return requestJson("GET", `/api/repositories?query=${encodeURIComponent(query || "")}`);
  }

  async function startRun(payload) {
    return requestJson("POST", "/api/runs/start", payload);
  }

  async function pickRepository() {
    return requestJson("POST", "/api/repository/pick", {});
  }

  async function getDefaultGreeting() {
    return requestJson("GET", "/api/default-greeting");
  }

  async function submitApproval(decision) {
    return requestJson("POST", "/api/approval", { decision });
  }

  async function requestTick() {
    return requestJson("POST", "/api/tick", {});
  }

  async function requestRetry(failureEventId) {
    return requestJson("POST", "/api/runs/current/retry", {
      failure_event_id: failureEventId,
    });
  }

  async function requestCancel() {
    return requestJson("POST", "/api/runs/current/cancel", {});
  }

  async function requestForceQuotaResume() {
    return requestJson("POST", "/api/runs/current/quota-resume", {});
  }

  async function getChatGPTUILease() {
    return requestJson("GET", "/api/chatgpt-ui-lease");
  }

  async function releaseStaleChatGPTUILease(payload) {
    return requestJson("POST", "/api/chatgpt-ui-lease/release-stale", payload);
  }

  async function requestJson(method, path, payload, requestOptions) {
    const headers = {};
    if (controllerToken) {
      headers["X-Controller-Token"] = controllerToken;
    }
    const options = {
      method,
      headers,
      cache: "no-store",
      credentials: "same-origin",
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
    if (response.status === 401 && !(requestOptions && requestOptions.suppressAuthFailure)) {
      authenticated = false;
      pollingStopped = true;
      clearPollTimer();
      setConnectionState("auth-failed", "API authentication failed");
      setMutationDisabled(true);
      showPairingPanel();
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
    if (pollingStopped || stateRequestInFlight || !authenticated) {
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
    if (leaseRequestInFlight || !authenticated) {
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
    if (optionsRequestInFlight || !authenticated) {
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
    if (startRequestInFlight || !authenticated) {
      return;
    }
    const repositoryPath = elements["repository-path"].value.trim();
    const initialInstruction = elements["initial-task"].value.trim();
    const projectTitle = elements["project-title"].value.trim();
    const chatTitle = elements["chat-title"].value.trim();
    const sandbox = elements["sandbox-select"].value;
    const model = elements["model-select"].value;
    const allowDestinationNavigation = Boolean(elements["allow-destination-navigation"].checked);
    const fullAccessConfirmation = elements["full-access-confirmation"].value.trim();
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
      ...(sandbox === "danger-full-access"
        ? { full_access_confirmation: fullAccessConfirmation }
        : {}),
    });
    startRequestInFlight = false;
    setText(elements["startup-status"], result.ok ? "Run started." : safeMessage(result));
    if (result.status !== 401) {
      await forceRefresh();
    }
    updateControlState();
  }

  function onSelectFullAccess() {
    const fullAccessAvailable = Array.from(elements["sandbox-select"].options)
      .some((option) => option.value === "danger-full-access");
    if (!fullAccessAvailable) {
      setText(
        elements["startup-status"],
        "Remote Full Access requires --allow-remote-full-access on the Mac.",
      );
      return;
    }
    elements["sandbox-select"].value = "danger-full-access";
    updatePermissionPresetDescription();
    updateFullAccessConfirmation();
    updateControlState();
    if (remoteDevice) {
      elements["full-access-confirmation"].focus();
    }
  }

  async function onBrowseRepository() {
    if (repositoryPickerRequestInFlight || !authenticated) {
      return;
    }
    if (remoteDevice) {
      await loadRepositoryCatalog();
      elements["repository-catalog"].classList.remove("hidden");
      elements["repository-catalog"].focus();
      return;
    }
    repositoryPickerRequestInFlight = true;
    setText(elements["repository-picker-status"], "Opening folder picker...");
    updateControlState();
    const result = await pickRepository();
    repositoryPickerRequestInFlight = false;
    if (result.status === 401) {
      return;
    }
    if (result.ok && result.selected && typeof result.repository_path === "string") {
      elements["repository-path"].value = result.repository_path;
      setText(elements["repository-picker-status"], "Repository selected.");
    } else if (result.ok) {
      setText(elements["repository-picker-status"], "No folder selected.");
    } else {
      setText(elements["repository-picker-status"], safeMessage(result));
    }
    updateControlState();
  }

  async function loadRepositoryCatalog() {
    if (!authenticated) {
      return;
    }
    setText(elements["repository-picker-status"], "Loading authorized repositories...");
    const result = await getRepositories("");
    if (!result.ok || !Array.isArray(result.repositories)) {
      setText(elements["repository-picker-status"], safeMessage(result));
      return;
    }
    const currentValue = elements["repository-catalog"].value;
    elements["repository-catalog"].replaceChildren();
    const placeholder = document.createElement("option");
    placeholder.value = "";
    placeholder.textContent = "Select an authorized repository";
    elements["repository-catalog"].appendChild(placeholder);
    for (const repository of result.repositories) {
      if (!repository || typeof repository.path !== "string") {
        continue;
      }
      const option = document.createElement("option");
      option.value = repository.path;
      option.textContent = repository.name
        ? `${repository.name} — ${repository.path}`
        : repository.path;
      elements["repository-catalog"].appendChild(option);
    }
    elements["repository-catalog"].value = currentValue;
    elements["repository-catalog"].classList.remove("hidden");
    setText(
      elements["repository-picker-status"],
      result.repositories.length
        ? `${result.repositories.length} authorized repositories available.`
        : "No authorized repositories found. Configure --repository-root on the Mac.",
    );
  }

  async function onDefaultGreeting() {
    if (defaultGreetingRequestInFlight || !authenticated) {
      return;
    }
    defaultGreetingRequestInFlight = true;
    setText(elements["default-greeting-status"], "Loading default greeting...");
    updateControlState();
    const result = await getDefaultGreeting();
    defaultGreetingRequestInFlight = false;
    if (result.status === 401) {
      return;
    }
    if (result.ok && typeof result.initial_instruction === "string") {
      elements["initial-task"].value = result.initial_instruction;
      setText(elements["default-greeting-status"], "Default greeting loaded.");
    } else {
      setText(elements["default-greeting-status"], safeMessage(result));
    }
    updateControlState();
  }

  async function onApproval(decision) {
    if (approvalRequestInFlight || !authenticated) {
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
    if (tickRequestInFlight || !authenticated) {
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

  async function onRetry() {
    if (retryRequestInFlight || !authenticated) {
      return;
    }
    const model = currentStatePayload && currentStatePayload.state
      ? currentStatePayload.state
      : null;
    const failure = model && model.latest_failure ? model.latest_failure : null;
    const failureEventId = failure && Number.isInteger(failure.event_id)
      ? failure.event_id
      : 0;
    if (!failureEventId) {
      setText(elements["retry-status"], "No current failure is available to retry.");
      return;
    }
    if (failure.retryable !== true) {
      setText(
        elements["retry-status"],
        failure.recovery_message || "This action requires review before it can be retried safely.",
      );
      return;
    }

    retryRequestInFlight = true;
    updateControlState();
    setText(elements["retry-status"], "Retrying the failed action...");
    const result = await requestRetry(failureEventId);
    retryRequestInFlight = false;
    setText(
      elements["retry-status"],
      result.ok ? "Retry started from the paused action." : safeMessage(result),
    );
    if (result.status !== 401) {
      await forceRefresh();
    }
    updateControlState();
  }

  async function onCancelRun() {
    if (cancelRequestInFlight || !authenticated) {
      return;
    }
    if (!window.confirm("Stop the current CRAX run and terminate an active Codex process?")) {
      return;
    }
    cancelRequestInFlight = true;
    updateControlState();
    setText(elements["cancel-run-status"], "Stopping run...");
    const result = await requestCancel();
    cancelRequestInFlight = false;
    setText(
      elements["cancel-run-status"],
      result.ok ? "Stop requested." : safeMessage(result),
    );
    if (result.status !== 401) {
      await forceRefresh();
    }
    updateControlState();
  }

  async function onForceQuotaContinue() {
    if (quotaContinueRequestInFlight || !authenticated) {
      return;
    }
    quotaContinueRequestInFlight = true;
    updateControlState();
    setText(elements["quota-force-continue-status"], "Resuming Codex...");
    const result = await requestForceQuotaResume();
    quotaContinueRequestInFlight = false;
    setText(
      elements["quota-force-continue-status"],
      result.ok ? "Continue started." : safeMessage(result),
    );
    if (result.status !== 401) {
      await forceRefresh();
    }
    updateControlState();
  }

  async function onReleaseStaleLease() {
    if (leaseReleaseRequestInFlight || !authenticated) {
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
    currentCodexSessionId = "";
    currentCodexPlan = [];
  }

  function startProgressStream() {
    if (!authenticated || !progressRunId || progressStreamController || !window.ReadableStream) {
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
      headers: controllerToken ? { "X-Controller-Token": controllerToken } : {},
      cache: "no-store",
      credentials: "same-origin",
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
    if (!authenticated || !progressRunId || progressRequestInFlight) {
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
    if (event.kind === "process_started") {
      currentCodexSessionId = "";
      currentCodexPlan = [];
    }
    const sessionId = codexSessionIdFromProgressEvent(event);
    if (sessionId) {
      currentCodexSessionId = sessionId;
    }
    const plan = codexPlanFromProgressEvent(event);
    if (plan !== null) {
      currentCodexPlan = plan;
    }
    progressEvents.push(event);
    if (sequence) {
      progressAfterSequence = Math.max(progressAfterSequence, sequence);
    }
    if (progressEvents.length > PROGRESS_EVENT_MEMORY_LIMIT) {
      progressEvents = progressEvents.slice(-PROGRESS_EVENT_MEMORY_LIMIT);
    }
  }

  function codexSessionIdFromProgressEvent(event) {
    const metadata = event && event.metadata && typeof event.metadata === "object"
      ? event.metadata
      : {};
    if (metadata.event_type !== "thread.started") {
      return "";
    }
    const summary = metadata.value_summary && typeof metadata.value_summary === "object"
      ? metadata.value_summary
      : {};
    return typeof summary.codex_session_id === "string" ? summary.codex_session_id : "";
  }

  function codexPlanFromProgressEvent(event) {
    const metadata = event && event.metadata && typeof event.metadata === "object"
      ? event.metadata
      : {};
    const summary = metadata.value_summary && typeof metadata.value_summary === "object"
      ? metadata.value_summary
      : {};
    if (summary.item_type !== "todo_list" || !Array.isArray(summary.plan_items)) {
      return null;
    }
    return summary.plan_items
      .filter(
        (item) => item && typeof item.label === "string" && typeof item.completed === "boolean",
      )
      .map((item) => ({ label: item.label, completed: item.completed }));
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

    ensureProgressTransport(rawActiveRunId);
    renderCodexLiveProgress(runtime, model);
    renderApproval(model, runtime);
    renderProgress(model, runtime);
    renderFailure(model, runtime);
    renderTerminal(model, runtime);
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

  function renderCodexLiveProgress(runtime, model) {
    const latest = progressEvents.length ? progressEvents[progressEvents.length - 1] : null;
    const label = codexLiveLabel(latest, runtime || {}, model);
    setText(elements["codex-live-state"], label.text);
    elements["codex-live-state"].className = `status-badge ${label.className}`;
    setText(
      elements["codex-live-session-id"],
      currentCodexSessionId || (progressRunId ? "Not available yet" : "None"),
    );
    const quotaWait = model && model.quota_wait && typeof model.quota_wait === "object"
      ? model.quota_wait
      : null;
    const quotaResumeLive = quotaResumeIsLive(runtime);
    if (quotaWait && quotaWait.resume_at && !quotaResumeLive) {
      setText(elements["codex-live-quota-wait"], quotaWaitClientMessage(quotaWait));
    } else {
      setText(elements["codex-live-quota-wait"], "None");
    }

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
    setText(
      elements["codex-live-error"],
      quotaWait && !quotaResumeLive ? "None" : (issue ? progressEventLabel(issue) : "None"),
    );
    renderProgressEvents();
    renderCodexPlan();
  }

  function renderProgressEvents() {
    const list = elements["codex-live-events"];
    list.replaceChildren();
    const events = progressEvents
      .filter((event) => event && event.kind === "assistant_commentary" && event.summary)
      .slice(-PROGRESS_EVENT_RENDER_LIMIT);
    if (!events.length) {
      const item = document.createElement("li");
      item.className = "progress-event";
      const message = document.createElement("p");
      setText(message, progressRunId ? "Waiting for Codex to share an update." : "No active run.");
      item.append(message);
      list.append(item);
      return;
    }
    for (const event of events) {
      const item = document.createElement("li");
      item.className = "progress-event progress-commentary";

      const message = document.createElement("p");
      setText(message, event.summary);

      item.append(message);
      list.append(item);
    }
  }

  function renderCodexPlan() {
    const list = elements["codex-live-plan"];
    list.replaceChildren();
    if (!currentCodexPlan.length) {
      const item = document.createElement("li");
      item.className = "codex-plan-empty";
      setText(item, progressRunId ? "No plan published yet." : "No active run.");
      list.append(item);
      return;
    }
    for (const planItem of currentCodexPlan) {
      const item = document.createElement("li");
      item.className = planItem.completed ? "codex-plan-item completed" : "codex-plan-item";
      item.setAttribute(
        "aria-label",
        `${planItem.completed ? "Completed" : "Pending"}: ${planItem.label}`,
      );

      const marker = document.createElement("span");
      marker.className = "codex-plan-marker";
      marker.setAttribute("aria-hidden", "true");
      setText(marker, planItem.completed ? "✓" : "○");

      const label = document.createElement("span");
      label.className = "codex-plan-label";
      setText(label, planItem.label);

      item.append(marker, label);
      list.append(item);
    }
  }

  function quotaResumeIsLive(runtime) {
    return Boolean(
      runtime &&
        (runtime.current_action_kind === "quota_resume" ||
          (runtime.action_running && runtime.controller_state === "running_routine_action")),
    );
  }

  function quotaWaitClientMessage(quotaWait) {
    const until = Date.parse(quotaWait && quotaWait.resume_at ? quotaWait.resume_at : "");
    if (Number.isFinite(until)) {
      const remainingMs = until - Date.now();
      if (remainingMs <= 0) {
        return "Codex limits ran out. Resuming shortly.";
      }
      const totalMinutes = Math.ceil(remainingMs / 60000);
      const hours = Math.floor(totalMinutes / 60);
      const minutes = totalMinutes % 60;
      const hh = String(hours).padStart(2, "0");
      const mm = String(minutes).padStart(2, "0");
      return `Codex limits ran out. Reset in ${hh}:${mm} hours`;
    }
    if (quotaWait && typeof quotaWait.message === "string" && quotaWait.message.trim()) {
      return quotaWait.message;
    }
    return "Codex limits ran out. Waiting for reset.";
  }

  function codexLiveLabel(latest, runtime, model) {
    if (
      model &&
      model.quota_wait &&
      model.quota_wait.resume_at &&
      !quotaResumeIsLive(runtime)
    ) {
      return { text: "Waiting for Codex reset", className: "status-warn" };
    }
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
    const metadata = event.metadata && typeof event.metadata === "object" ? event.metadata : {};
    const valueSummary =
      metadata.value_summary && typeof metadata.value_summary === "object"
        ? metadata.value_summary
        : {};
    const nestedError =
      typeof valueSummary.error === "string" && valueSummary.error.trim()
        ? valueSummary.error.trim()
        : "";
    const summaryText = nestedError || event.summary;
    const summary = summaryText ? `: ${summaryText}` : "";
    return `${title}${summary}`;
  }

  function renderApproval(model, runtime) {
    const required = Boolean(
      model &&
        model.requires_human_approval &&
        !model.latest_failure,
    );
    elements["approval-panel"].classList.toggle("hidden", !required);
    const kind = model && model.approval_kind ? model.approval_kind.replaceAll("_", " ") : "approval";
    setText(elements["approval-kind"], required ? `Approval required: ${kind}` : "No approval is pending.");
    const disabled = !required || runtime.action_running || approvalRequestInFlight || !authenticated;
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
        !model.latest_failure &&
        !model.requires_human_approval &&
        !model.terminal &&
        !model.blocked &&
        !model.completed &&
        !runtime.action_running,
    );
    elements["progress-panel"].classList.toggle("hidden", !available);
    elements["tick-button"].disabled = !available || tickRequestInFlight || !authenticated;
  }

  function renderFailure(model, runtime) {
    const failure = model && model.latest_failure && typeof model.latest_failure === "object"
      ? model.latest_failure
      : null;
    elements["failure-panel"].classList.toggle("hidden", !failure);
    if (!failure) {
      elements["retry-button"].disabled = true;
      setText(elements["retry-status"], "");
      return;
    }

    const retryable = failure.retryable === true;
    const classification = valueOrNone(failure.retry_classification);
    setText(
      elements["failure-summary"],
      retryable ? "Paused — ready for manual retry" : "Paused — manual review required",
    );
    elements["failure-summary"].className =
      `status-badge ${retryable ? "status-warn" : "status-bad"}`;
    setText(elements["failure-action"], valueOrNone(failure.action_key));
    setText(elements["failure-event-id"], failure.event_id ? `#${failure.event_id}` : "None");
    setText(
      elements["failure-reason"],
      `${valueOrNone(failure.reason_code)} (${classification})`,
    );
    setText(elements["failure-timestamp"], valueOrNone(failure.timestamp));
    setText(
      elements["failure-error"],
      valueOrNone(failure.error_message || failure.message),
    );
    setText(
      elements["failure-recovery"],
      valueOrNone(failure.recovery_message),
    );
    elements["retry-button"].disabled =
      !retryable ||
      Boolean(runtime.action_running) ||
      retryRequestInFlight ||
      !authenticated;
    setText(
      elements["retry-button"],
      retryable ? "Retry failed action" : "Retry unavailable — review required",
    );
  }

  function renderTerminal(model, runtime) {
    const label = terminalLabel(model, runtime);
    setText(elements["terminal-status"], label.text);
    elements["terminal-status"].className = `status-badge ${label.className}`;
  }

  function updateControlState() {
    const model = currentStatePayload && currentStatePayload.state ? currentStatePayload.state : null;
    const runtime = model && model.controller_runtime ? model.controller_runtime : {};
    const activeRun = Boolean((model && model.run_id) || runtime.active_run_id);
    const running = Boolean(runtime.action_running);
    const activeRunReplaceable = Boolean(
      activeRun &&
      !running &&
      model &&
      (
        REPLACEABLE_RUN_STATUSES.has(model.run_status) ||
        runtime.controller_state === "blocked" ||
        runtime.controller_state === "waiting_for_retry" ||
        runtime.controller_state === "failed" ||
        runtime.controller_state === "completed"
      ),
    );
    const optionsUnavailable = optionsRequestInFlight || !profileOptions;
    const invalidSelection = !profileSelectionValid();
    const fullAccessConfirmationMissing =
      remoteDevice &&
      elements["sandbox-select"].value === "danger-full-access" &&
      elements["full-access-confirmation"].value.trim() !== "ENABLE FULL ACCESS";
    const requiredFieldsMissing =
      !elements["repository-path"].value.trim() ||
      !elements["initial-task"].value.trim() ||
      !elements["project-title"].value.trim() ||
      !elements["chat-title"].value.trim();
    const disableInputs =
      !authenticated ||
      (activeRun && !activeRunReplaceable) ||
      running ||
      startRequestInFlight ||
      repositoryPickerRequestInFlight ||
      defaultGreetingRequestInFlight;
    const disableStart =
      disableInputs ||
      optionsUnavailable ||
      invalidSelection ||
      requiredFieldsMissing ||
      fullAccessConfirmationMissing;
    elements["repository-path"].disabled = disableInputs;
    elements["repository-browse-button"].disabled = disableInputs;
    elements["repository-catalog"].disabled = disableInputs;
    elements["initial-task"].disabled = disableInputs;
    elements["default-greeting-button"].disabled = disableInputs;
    elements["project-title"].disabled = disableInputs;
    elements["chat-title"].disabled = disableInputs;
    elements["allow-destination-navigation"].disabled = disableInputs;
    elements["sandbox-select"].disabled = disableInputs || optionsUnavailable;
    elements["full-access-button"].disabled = disableInputs || optionsUnavailable;
    elements["model-select"].disabled = disableInputs || optionsUnavailable;
    elements["full-access-confirmation"].disabled = disableInputs;
    elements["start-button"].disabled = disableStart;
    elements["cancel-run-button"].disabled =
      !authenticated ||
      !activeRun ||
      cancelRequestInFlight ||
      Boolean(model && model.completed) ||
      Boolean(model && REPLACEABLE_RUN_STATUSES.has(model.run_status));
    const quotaWaitActive = Boolean(
      authenticated &&
      model &&
      model.quota_wait &&
      model.quota_wait.resume_at &&
      runtime.controller_state === "waiting_for_quota_reset" &&
      !quotaResumeIsLive(runtime) &&
      !running &&
      !quotaContinueRequestInFlight,
    );
    elements["quota-force-continue-button"].disabled = !quotaWaitActive;

    if (model) {
      renderApproval(model, runtime);
      renderProgress(model, runtime);
      renderFailure(model, runtime);
    } else {
      elements["approve-button"].disabled = true;
      elements["reject-button"].disabled = true;
      elements["tick-button"].disabled = true;
      elements["retry-button"].disabled = true;
      elements["cancel-run-button"].disabled = true;
      elements["quota-force-continue-button"].disabled = true;
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
    const disabledBase = !authenticated || leaseReleaseRequestInFlight || !active;
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
      "repository-browse-button",
      "repository-catalog",
      "initial-task",
      "default-greeting-button",
      "project-title",
      "chat-title",
      "allow-destination-navigation",
      "sandbox-select",
      "full-access-button",
      "model-select",
      "full-access-confirmation",
      "start-button",
      "approve-button",
      "reject-button",
      "tick-button",
      "retry-button",
      "cancel-run-button",
      "quota-force-continue-button",
      "lease-confirm-stale",
      "lease-release-reason",
      "lease-allow-owner-pid-alive",
      "lease-release-button",
    ]) {
      elements[id].disabled = disabled;
    }
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
      optionLabel(locked.approval_policy, "Codex default — Full Access bypasses approvals"),
    );
    updatePermissionPresetDescription();
    updateFullAccessConfirmation();
    const fullAccessAvailable = sandboxOptions.some(
      (option) => option.value === "danger-full-access",
    );
    elements["full-access-button"].classList.toggle("hidden", !fullAccessAvailable);
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

  function updateFullAccessConfirmation() {
    const visible = remoteDevice && elements["sandbox-select"].value === "danger-full-access";
    elements["full-access-confirmation-label"].classList.toggle("hidden", !visible);
    elements["full-access-confirmation"].classList.toggle("hidden", !visible);
    if (!visible) {
      elements["full-access-confirmation"].value = "";
    }
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

  function safeMessage(result) {
    const reason = result && result.reason_code ? result.reason_code : "request_failed";
    const message = result && result.error_message ? result.error_message : "Request failed.";
    return `${boundedText(reason, 80)}: ${boundedText(message, 180)}`;
  }

  function terminalLabel(model, runtime) {
    if (runtime.action_running) {
      return { text: "Action currently running", className: "status-warn" };
    }
    if (runtime.controller_state === "waiting_for_retry") {
      return { text: "Paused — waiting for manual retry", className: "status-warn" };
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

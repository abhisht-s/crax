// Executes the real dashboard code with an inert DOM and fake HTTP transport.
// No browser, UI automation, clipboard, or live server is involved.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");

const nodes = new Map();
function element(id) {
  if (!nodes.has(id)) {
    nodes.set(id, {
      textContent: "", value: "", disabled: false, checked: false,
      listeners: {}, classList: { toggle() {}, add() {}, remove() {} },
      addEventListener(name, handler) { this.listeners[name] = handler; },
    });
  }
  return nodes.get(id);
}
const calls = [];
const context = vm.createContext({
  assert, console, nodes, calls,
  document: { addEventListener() {}, getElementById: element },
  window: { confirm: () => true },
  fetch: async (url, options) => {
    calls.push({ url, options });
    return { status: 202, json: async () => ({ ok: true, metadata: { process_termination: { terminated: true } } }) };
  },
});
const harness = `
  globalThis.done = (async () => {
    collectElements();
    wireEvents();
    renderApproval = renderProgress = renderFailure = updateLeaseControlState = () => {};
    forceRefresh = async () => {};
    profileSelectionValid = () => true;
    authenticated = true;
    profileOptions = {};
    selectedRunId = "run-a";
    sessionListPayload = {live_session_count: 1, max_active_sessions: 4};
    currentStatePayload = {state: {
      run_id: "run-a", terminal: true, run_status: "completed",
      codex_session: {session_id: "session-a"}, controller_runtime: {action_running: true},
    }};
    updateControlState();
    assert.equal(elements["cancel-run-button"].disabled, false, "Stop remains available for terminal planner results with a running worker");
    assert.equal(elements["quota-force-continue-button"].disabled, true, "Continue cannot duplicate a running worker");
    await elements["cancel-run-button"].listeners.click({toString: () => "[object PointerEvent]"});
    assert.equal(calls[0].url, "/api/runs/run-a/cancel", "Click must target selected run ID");
    currentStatePayload.state.controller_runtime = {cancel_requested: true, cancel_termination: {terminated: true}};
    updateControlState();
    assert.equal(elements["cancel-run-status"].textContent, "Codex terminated; controller stopped.");
    assert.equal(elements["cancel-run-button"].disabled, false, "Stop can be retried");
    assert.equal(elements["quota-force-continue-button"].disabled, false, "Continue is available after stop without a quota wait");
    await elements["quota-force-continue-button"].listeners.click();
    assert.equal(calls[1].url, "/api/runs/run-a/continue");
    resetProgress("run-b");
    selectedRunId = "run-b";
    assert.equal(elements["cancel-run-status"].textContent, "", "Stop messages cannot leak across runs");
    assert.equal(elements["quota-force-continue-status"].textContent, "");
    currentStatePayload.state = {run_id: "run-b", terminal: true, controller_runtime: {}};
    updateControlState();
    assert.equal(elements["cancel-run-button"].disabled, false);
    assert.equal(elements["cancel-run-status"].textContent, "");
    assert.equal(elements["quota-force-continue-button"].disabled, false, "Stopped runs always offer continuation even when session recovery is unavailable");
    fetch = async () => ({ status: 409, json: async () => ({ok: false, reason_code: "codex_session_unavailable", error_message: "This run has no recoverable Codex session ID."}) });
    await elements["quota-force-continue-button"].listeners.click();
    assert.match(elements["quota-force-continue-status"].textContent, /no recoverable Codex session ID/, "Server denial must explain why continuation cannot proceed");
    updateControlState();
    assert.equal(elements["quota-force-continue-button"].disabled, false, "A denied request leaves the stopped-run option available");
    currentStatePayload.state.codex_session = {session_id: "session-b"};
    currentStatePayload.state.controller_runtime.worker_running = true;
    updateControlState();
    assert.equal(elements["quota-force-continue-button"].disabled, true, "Wait for the cancelled worker to exit");
    currentStatePayload.state.controller_runtime.worker_running = false;
    currentStatePayload.state.requires_human_approval = true;
    updateControlState();
    assert.equal(elements["quota-force-continue-button"].disabled, false, "Pending approval is explained by the server instead of hiding the option");
    for (const runStatus of ["needs_review", "failed", "completed", "rejected", "waiting_for_approval"]) {
      currentStatePayload.state.run_status = runStatus;
      currentStatePayload.state.latest_codex_result = {exit_code: 1};
      currentStatePayload.state.quota_wait = null;
      updateControlState();
      assert.equal(elements["quota-force-continue-button"].disabled, false, "Stopped status " + runStatus + " offers continuation without a quota wait");
    }
    sessionListPayload.ledger_durability = {blocked: true};
    updateControlState();
    assert.equal(elements["quota-force-continue-button"].disabled, false, "Persistence blockers must be explained by the server");
    sessionListPayload.ledger_durability = {blocked: false};
    fetch = async () => { throw new Error("offline"); };
    await elements["cancel-run-button"].listeners.click();
    assert.match(elements["cancel-run-status"].textContent, /Temporary connection failure/, "A failed stop request must remain visible");
    resetProgress("run-c");
    assert.equal(elements["cancel-run-status"].textContent, "");
    authenticated = false;
    updateControlState();
    assert.equal(elements["cancel-run-button"].disabled, true);
    assert.equal(elements["quota-force-continue-button"].disabled, true);
    console.log("Dashboard controls: all behavioral assertions passed.");
  })();
`;
const source = fs.readFileSync(path.join(__dirname, "../agent/web_static/app.js"), "utf8");
vm.runInContext(source.replace(/\}\)\(\);\s*$/, harness + "})();"), context);
context.done.catch(error => { console.error(error); process.exitCode = 1; });

// Real dashboard code, inert DOM, deterministic timers and HTTP fakes. No live UI.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");
const source = fs.readFileSync(path.join(__dirname, "../agent/web_static/app.js"), "utf8");

async function test(name, body) {
  const timers = new Map();
  let timerId = 0;
  function element() {
    return {
      textContent: "", value: "", disabled: false, checked: false, childNodes: [],
      classList: {toggle() {}, add() {}, remove() {}},
      addEventListener() {}, setAttribute() {},
      replaceChildren() { this.childNodes = []; },
      append(...children) { this.childNodes.push(...children); },
      appendChild(child) { this.append(child); },
    };
  }
  const nodes = new Map();
  const context = vm.createContext({
    assert, console, AbortController, TextDecoder, TextEncoder, timers,
    document: {
      addEventListener() {}, createElement: element,
      getElementById(id) { if (!nodes.has(id)) nodes.set(id, element()); return nodes.get(id); },
    },
    window: {
      ReadableStream: true,
      setTimeout(callback) { timers.set(++timerId, callback); return timerId; },
      clearTimeout(id) { timers.delete(id); },
    },
    fetch() { throw new Error("Unexpected HTTP request"); },
  });
  const harness = `
    globalThis.done = (async () => {
      collectElements(); authenticated = true;
      renderDurabilityBanner = renderChatGPTLaneStatus = renderSessionCapacity = () => {};
      renderDestinationBinding = renderNavigationApproval = renderHandoffPhase = renderExecutionProfile = () => {};
      renderApproval = renderProgress = renderFailure = renderTerminal = updateControlState = () => {};
      refreshLeaseStatus = async () => {};
      const deferred = () => { let resolve; const promise = new Promise(r => {resolve = r}); return {promise, resolve}; };
      const settle = async () => {for (let i = 0; i < 20; i++) await Promise.resolve();};
      const event = (run, sequence, kind = "assistant_commentary", metadata = {}) =>
        ({run_id: run, sequence, kind, summary: run + " note " + sequence, metadata});
      const payload = (run, events = []) => ({ok: true, metadata: {progress: {
        run_id: run, latest_sequence: Math.max(0, ...events.map(e => e.sequence)), events,
      }}});
      ${body}
    })();
  `;
  try {
    vm.runInContext(source.replace(/\}\)\(\);\s*$/, harness + "})();"), context);
    await context.done;
    console.log("PASS " + name);
  } catch (error) {
    console.error("FAIL " + name + ": " + error.stack);
    process.exitCode = 1;
  }
}

(async () => {
  await test("late A state cannot overwrite B or release B's request", `
    renderSessionList = () => {};
    getSessionList = async () => ({ok: true, sessions: [{run_id: "A"}, {run_id: "B"}]});
    const a = deferred(), b = deferred(), entered = deferred();
    getRunState = id => { if (id === "A") {entered.resolve(); return a.promise;} return b.promise; };
    startProgressStream = () => {};
    refreshProgress = async () => {};
    selectedRunId = "A";
    const pendingA = refreshCurrentState(); await entered.promise;
    selectedRunId = "B";
    const pendingB = forceRefresh(); await settle();
    a.resolve({ok: true, state: {run_id: "A"}}); await pendingA;
    assert.equal(currentStatePayload, null);
    assert.equal(stateRequestInFlight, true);
    b.resolve({ok: true, state: {run_id: "B"}}); await pendingB;
    assert.equal(currentStatePayload.state.run_id, "B");
    assert.equal(progressRunId, "B");
  `);

  await test("selection detaches A immediately while focus is pending", `
    focusRun = () => new Promise(() => {});
    forceRefresh = async () => {};
    getRunProgressSnapshot = async run => payload(run);
    readProgressStream = () => new Promise(() => {});
    selectedRunId = "A"; resetProgress("A");
    const oldController = new AbortController(); progressStreamController = oldController;
    appendProgressEvent(event("A", 1), "A");
    currentStatePayload = {state: {run_id: "A"}};
    sessionListPayload = {sessions: [{run_id: "A", focused: true}, {run_id: "B"}]};
    onSelectSession("B");
    assert.equal(progressRunId, "B"); assert.equal(oldController.signal.aborted, true);
    assert.equal(currentReadModel().run_id, "B"); assert.equal(progressEvents.length, 0);
    handleProgressFrame('event: progress\\ndata: ' + JSON.stringify(event("A", 2)), "A");
    assert.equal(progressEvents.length, 0);
    const cards = elements["session-list"].childNodes;
    assert.equal(cards.filter(card => card.className.includes("focused")).length, 1);
    assert.ok(cards[1].className.includes("focused"));
  `);

  await test("initial snapshot precedes streaming and returning to B refreshes the tail", `
    selectedRunId = "B";
    const snapshot = deferred(), streams = [];
    getRunProgressSnapshot = () => snapshot.promise;
    readProgressStream = (run, after, controller) => {streams.push({run, after, controller}); return new Promise(() => {});};
    ensureProgressTransport("B");
    assert.equal(streams.length, 0, "Do not stream from zero during snapshot loading");
    snapshot.resolve(payload("B", [event("B", 950), event("B", 951)])); await settle();
    assert.equal(progressAfterSequence, 951); assert.equal(streams[0].after, 951);
    assert.equal(progressEvents[0].sequence, 950);
    getRunProgressSnapshot = async run => payload(run, [event(run, 1200)]);
    selectedRunId = "A"; ensureProgressTransport("A"); await settle();
    selectedRunId = "B"; ensureProgressTransport("B"); await settle();
    assert.equal(progressEvents[0].sequence, 1200);
    assert.equal(streams.at(-1).after, 1200);
  `);

  await test("old snapshots cannot apply after A -> B -> A", `
    const old = deferred(); let count = 0;
    getRunProgressSnapshot = run => ++count === 1 ? old.promise : Promise.resolve(payload(run, [event(run, 200)]));
    readProgressStream = () => new Promise(() => {});
    selectedRunId = "A"; ensureProgressTransport("A");
    selectedRunId = "B"; ensureProgressTransport("B"); await settle();
    selectedRunId = "A"; ensureProgressTransport("A"); await settle();
    old.resolve(payload("A", [event("A", 100)])); await settle();
    assert.equal(progressEvents.at(-1).sequence, 200);
    assert.equal(progressAfterSequence, 200);
  `);

  await test("old stream cleanup preserves B and current disconnect schedules recovery", `
    const a = deferred(), b = deferred();
    getRunProgressSnapshot = async run => payload(run, [event(run, 10)]);
    readProgressStream = run => run === "A" ? a.promise : b.promise;
    selectedRunId = "A"; ensureProgressTransport("A"); await settle();
    selectedRunId = "B"; ensureProgressTransport("B"); await settle();
    const controllerB = progressStreamController;
    a.resolve(); await settle();
    assert.equal(progressStreamController, controllerB); assert.equal(progressStreamActive, true);
    b.resolve(); await settle();
    assert.equal(progressStreamController, null); assert.equal(progressStreamActive, false);
    assert.notEqual(progressPollTimer, null, "A disconnected stream must recover");
  `);

  await test("buffered frames from an aborted A stream cannot enter a later A view", `
    const reads = [], urls = [];
    getRunProgressSnapshot = async run => payload(run, [event(run, 200)]);
    fetch = async url => {
      urls.push(url); const pending = deferred(); reads.push(pending);
      return {status: 200, ok: true, body: {getReader: () => ({read: () => pending.promise})}};
    };
    selectedRunId = "A"; ensureProgressTransport("A"); await settle();
    selectedRunId = "B"; ensureProgressTransport("B"); await settle();
    selectedRunId = "A"; ensureProgressTransport("A"); await settle();
    const current = progressStreamController;
    assert.equal(urls[2], "/api/runs/A/events?after_sequence=200");
    reads[0].resolve({done: false, value: new TextEncoder().encode(
      'event: progress\\ndata: ' + JSON.stringify(event("A", 900)) + '\\n\\n'
    )}); await settle();
    assert.equal(progressAfterSequence, 200);
    assert.equal(progressStreamController, current);
  `);

  await test("older batches cannot replay evicted events or regress plan/session", `
    selectedRunId = "B"; resetProgress("B");
    for (let sequence = 1; sequence <= 200; sequence++) appendProgressEvent(event("B", sequence), "B");
    currentCodexSessionId = "new-session"; currentCodexPlan = [{label: "Latest plan", completed: false}];
    const old = Array.from({length: 100}, (_, i) => event("B", i + 1));
    old.push(event("B", 101, "process_started"));
    applyProgressPayload(payload("B", old));
    assert.equal(progressEvents.at(-1).sequence, 200); assert.equal(progressAfterSequence, 200);
    assert.equal(currentCodexSessionId, "new-session"); assert.equal(currentCodexPlan[0].label, "Latest plan");
    applyProgressPayload(payload("B", [event("B", 202), event("B", 201)]));
    assert.equal(progressEvents.at(-1).sequence, 202);
  `);

  await test("working notes survive a burst of command events", `
    selectedRunId = "B"; resetProgress("B"); appendProgressEvent(event("B", 1), "B");
    for (let sequence = 2; sequence < 200; sequence++) appendProgressEvent(event("B", sequence, "command_finished"), "B");
    assert.equal(progressEvents.filter(e => e.kind === "assistant_commentary")[0].sequence, 1);
    assert.ok(progressEvents.length <= PROGRESS_EVENT_MEMORY_LIMIT + PROGRESS_EVENT_RENDER_LIMIT);
  `);

  await test("failed snapshot retries without streaming history", `
    selectedRunId = "B";
    getRunProgressSnapshot = async () => ({ok: false, status: 0});
    readProgressStream = () => {throw Error("Must not start a stream before snapshot succeeds");};
    ensureProgressTransport("B"); await settle();
    assert.equal(progressStreamController, null); assert.notEqual(progressPollTimer, null);
    getRunProgressSnapshot = async () => payload("B", [event("B", 800)]);
    readProgressStream = () => new Promise(() => {});
    const callback = timers.get(progressPollTimer); callback(); await settle();
    assert.equal(progressAfterSequence, 800); assert.equal(progressStreamActive, true);
  `);

  await test("polling-only clients load a snapshot then follow its cursor", `
    window.ReadableStream = undefined; selectedRunId = "B";
    const urls = [];
    fetch = async url => {
      urls.push(url);
      return {ok: true, status: 200, json: async () => payload("B", [event("B", urls.length === 1 ? 950 : 951)])};
    };
    ensureProgressTransport("B"); await settle();
    assert.equal(urls[0], "/api/runs/B/progress?snapshot=1");
    assert.equal(progressStreamController, null);
    timers.get(progressPollTimer)(); await settle();
    assert.equal(urls[1], "/api/runs/B/progress?after_sequence=950");
    assert.equal(progressAfterSequence, 951); assert.notEqual(progressPollTimer, null);
  `);
})();

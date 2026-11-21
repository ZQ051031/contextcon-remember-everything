"use strict";

const $ = (id) => document.getElementById(id);
const state = { sessions: [], active: null, turns: [], selected: null, view: "chat",
  query: "", page: 1, busy: false, agents: {}, provider: "", model: "" };
const labels = { memory: "Memory", conversation: "Past conversation", knowledge: "Shared knowledge" };
const dateFormat = new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric" });

function node(tag, className, text) {
  const el = document.createElement(tag);
  if (className) el.className = className;
  if (text !== undefined) el.textContent = text;
  return el;
}

function button(className, label, onClick) {
  const el = node("button", className, label);
  el.type = "button";
  el.addEventListener("click", onClick);
  return el;
}

async function api(path, method = "GET", body) {
  const options = { method };
  if (body !== undefined) {
    options.headers = { "Content-Type": "application/json" };
    options.body = JSON.stringify(body);
  }
  const res = await fetch("/api" + path, options);
  if (res.status === 204) return null;
  if (!res.ok) {
    const error = await res.json().catch(() => ({}));
    throw new Error(error.error || "Request failed (" + res.status + ").");
  }
  return res.json();
}

function endpoint(resource = "") {
  if (!state.active) throw new Error("Create a session to continue.");
  return "/sessions/" + encodeURIComponent(state.active.id) + resource;
}

function notify(text, error = false) {
  const dialog = document.querySelector("dialog[open]");
  if (dialog && error) {
    const alert = node("p", "dialog-error", text);
    alert.setAttribute("role", "alert");
    dialog.append(alert);
    return;
  }
  $("notice").textContent = text;
  $("notice").classList.toggle("error", error);
  $("notice").setAttribute("role", error ? "alert" : "status");
  $("notice").hidden = !text;
}

async function run(action) {
  if (state.busy) return;
  state.busy = true;
  $("send").disabled = true;
  for (const alert of document.querySelectorAll(".dialog-error")) alert.remove();
  notify("");
  try {
    await action();
  } catch (error) {
    notify(error.message, true);
  } finally {
    state.busy = false;
    $("send").disabled = !state.active;
  }
}

function displayDate(value) {
  const date = new Date(value);
  return value && !Number.isNaN(date.valueOf()) ? dateFormat.format(date) : "";
}

function agentName(agent) {
  return state.agents[agent] || agent || "Assistant";
}

function selectView(view) {
  state.view = view;
  $("chat").hidden = view !== "chat";
  $("library").hidden = view !== "library";
  $("show-chat").setAttribute("aria-pressed", view === "chat");
  $("show-library").setAttribute("aria-pressed", view === "library");
}

function showSessions() {
  $("sessions").replaceChildren(...state.sessions.map((session) => {
    const selected = state.active?.id === session.id;
    const row = button("session-item", "", () => run(async () => {
      await openSession(session);
      selectView("chat");
    }));
    if (selected) row.setAttribute("aria-current", "true");
    row.append(node("span", "session-symbol", "↳"));
    const copy = node("span", "session-copy");
    copy.append(node("strong", "", session.title), node("small", "",
      agentName(session.agent) + " · " + displayDate(session.created)));
    row.append(copy);
    return row;
  }));
}

async function openSession(session) {
  const turns = await api("/sessions/" + encodeURIComponent(session.id) + "/turns");
  state.active = session;
  state.turns = turns;
  state.selected = turns.at(-1) || null;
  $("question").value = "";
  $("recall").checked = true;
  $("session-title").textContent = session.title;
  $("session-agent").textContent = agentName(session.agent);
  $("delete-session").hidden = false;
  showSessions();
  showTurns(true);
}

async function addSession(data) {
  const session = await api("/sessions", "POST", data);
  state.sessions.unshift(session);
  showSessions();
  return session;
}

function citationText(text, turn) {
  const paragraph = node("div", "answer-text");
  const pieces = text.split(/(\[\d+\])/);
  for (const piece of pieces) {
    const ref = /^\[(\d+)\]$/.exec(piece);
    const hit = ref && turn.trace.evidence.find((item) => item.ref === Number(ref[1]));
    if (hit) {
      const citation = button("citation", piece, () => selectTurn(turn, hit.ref));
      citation.setAttribute("aria-label", "Inspect source " + hit.ref);
      paragraph.append(citation);
    } else {
      paragraph.append(document.createTextNode(piece));
    }
  }
  return paragraph;
}

function showTurns(scrollDown) {
  $("welcome").hidden = state.turns.length > 0;
  $("turns").replaceChildren(...state.turns.map((turn) => {
    const section = node("article", "turn");
    const prompt = node("div", "user-turn");
    prompt.append(node("span", "turn-byline", "You"), node("p", "", turn.question));
    const reply = node("div", "assistant-turn");
    const title = node("div", "turn-byline reply-name");
    title.append(node("span", "tiny-mark"), node("strong", "", agentName(turn.trace.agent)));
    if (turn.trace.tool === "Recall off") title.append(node("span", "agent", "Recall off"));
    reply.append(title, citationText(turn.answer, turn));
    const inspect = button("quiet-button inspect-button", "Inspect recall ↗", () => selectTurn(turn));
    inspect.setAttribute("aria-pressed", state.selected?.id === turn.id);
    reply.append(inspect);
    section.append(prompt, reply);
    return section;
  }));
  showTrace();
  if (scrollDown) {
    requestAnimationFrame(() => { $("chat-scroll").scrollTop = $("chat-scroll").scrollHeight; });
  }
}

function selectTurn(turn, ref) {
  state.selected = turn;
  showTurns(false);
  const target = ref ? $("evidence-" + ref) : $("trace-heading");
  target?.focus({ preventScroll: true });
  target?.scrollIntoView({ block: "nearest" });
}

function showTrace() {
  const root = $("trace");
  root.replaceChildren();
  if (!state.selected) {
    const empty = node("div", "trace-empty");
    empty.append(node("span", "empty-orbit", "·"), node("h3", "", "No turn selected"),
      node("p", "", "Send a question to inspect the sources, ranking signals, and tool calls."));
    root.append(empty);
    return;
  }
  const turn = state.selected;
  const trace = turn.trace;
  const stats = node("div", "trace-summary");
  const heading = node("div", "trace-summary-title");
  heading.id = "trace-heading";
  heading.tabIndex = -1;
  heading.append(node("strong", "", trace.evidence.length + " evidence items"),
    node("span", "tag", trace.elapsed_ms + " ms"));
  stats.append(heading, node("p", "trace-tool", trace.tool));
  const facts = node("dl", "trace-facts");
  const pairs = [["Responder", trace.responder], ["Capture", trace.capture]];
  if (typeof trace.match_quality === "string") pairs.push(["Meko match quality", trace.match_quality]);
  for (const [name, value] of pairs) {
    facts.append(node("dt", "", name), node("dd", "", value || "—"));
  }
  stats.append(facts);
  if (trace.conversation_id) {
    const details = node("details", "trace-details");
    details.append(node("summary", "", "Meko conversation ID"),
      node("code", "", trace.conversation_id));
    stats.append(details);
  }
  if (typeof trace.hint === "string") stats.append(node("p", "trace-hint", trace.hint));
  for (const warning of trace.warnings || []) stats.append(node("p", "trace-warning", warning));
  root.append(stats);

  if (Object.keys(trace.buckets).length) {
    const counts = node("div", "source-counts");
    for (const [source, count] of Object.entries(trace.buckets)) {
      const line = node("div", "source-count");
      line.append(node("span", "", labels[source]), node("strong", "", count ?? "Unavailable"));
      counts.append(line);
    }
    root.append(counts);
  }

  for (const hit of trace.evidence) {
    const card = node("article", "evidence-card");
    card.id = "evidence-" + hit.ref;
    card.tabIndex = -1;
    const row = node("div", "evidence-head");
    row.append(node("span", "evidence-ref", String(hit.ref)), node("strong", "", labels[hit.source]));
    if (typeof hit.score === "number") row.append(node("span", "score", "Score " + hit.score.toFixed(2)));
    if (typeof hit.distance === "number") row.append(node("span", "score", "Distance " + hit.distance.toFixed(2)));
    card.append(row);
    const origin = [hit.session_title, hit.agent ? agentName(hit.agent) : "", displayDate(hit.created)]
      .filter(Boolean).join(" · ");
    if (origin) card.append(node("p", "evidence-origin", origin));
    card.append(node("p", "evidence-text", hit.text), node("p", "evidence-reason", hit.reason));
    if (typeof hit.cosine === "number") {
      card.append(node("p", "evidence-origin", "Vector cosine " + hit.cosine.toFixed(2)));
    }
    root.append(card);
  }

  if (trace.tool !== "Recall off") {
    root.append(button("button comparison", "Compare with recall off", () => run(async () => {
      await sendTurn(turn.question, false);
    })));
  }
}

async function sendTurn(question, recall) {
  if (!question.trim()) return;
  $("send").textContent = recall ? "Recalling…" : "Comparing…";
  try {
    const turn = await api(endpoint("/turns"), "POST", { question, recall });
    state.turns.push(turn);
    state.selected = turn;
    showTurns(true);
    return turn;
  } finally {
    $("send").textContent = "Send ↑";
  }
}

async function readLibrary() {
  const params = new URLSearchParams({ q: state.query, page: state.page });
  const { memories, pages } = await api(endpoint("/memories?") + params);
  if (state.page > pages && !memories.length) {
    state.page = pages;
    return readLibrary();
  }
  $("clear-search").hidden = !state.query;
  $("pagination").hidden = pages < 2;
  $("page-label").textContent = "Page " + state.page + " of " + pages;
  $("page-back").disabled = state.page <= 1;
  $("page-next").disabled = state.page >= pages;
  const list = $("memory-list");
  list.replaceChildren();
  if (!memories.length) {
    const empty = node("div", "library-empty");
    empty.append(node("h2", "", state.query ? "No matching memories" : "Your library is empty"),
      node("p", "", state.query ? "Try a different term or clear the search."
        : "Save a fact, preference, or decision for future sessions."));
    list.append(empty);
    return;
  }
  for (const memory of memories) {
    const card = node("article", "memory-card");
    const head = node("div", "memory-head");
    head.append(node("span", "tag kind-" + memory.kind, memory.kind),
      node("time", "", displayDate(memory.created)));
    const controls = node("div", "memory-footer");
    const origin = [memory.session_title, memory.agent ? agentName(memory.agent) : ""]
      .filter(Boolean).join(" · ");
    controls.append(node("span", "", origin));
    if (memory.id) {
      const remove = button("quiet-button remove-memory", "Delete", () => run(async () => {
        if (!await confirmDelete("Permanently remove this memory from the library? Saved chats and past trace snapshots remain.")) return;
        await api(endpoint("/memories/") + encodeURIComponent(memory.id), "DELETE", {});
        await readLibrary();
        notify("Memory deleted.");
      }));
      remove.setAttribute("aria-label", "Delete memory: " + memory.text.slice(0, 100));
      controls.append(remove);
    }
    card.append(head, node("p", "", memory.text), controls);
    list.append(card);
  }
}

function confirmDelete(description) {
  const dialog = $("confirm-dialog");
  $("confirm-description").textContent = description;
  dialog.returnValue = "cancel";
  return new Promise((resolve) => {
    dialog.addEventListener("close", () => resolve(dialog.returnValue === "delete"), { once: true });
    dialog.showModal();
  });
}

async function loadDemo() {
  $("demo-dialog").close();
  const planner = await addSession({ title: "Atlas planning", agent: "fieldnote_planner" });
  await openSession(planner);
  selectView("chat");
  await api(endpoint("/memories"), "POST", {
    text: "The Atlas kickoff is scheduled for June 12 at 10 a.m. Pacific.",
    kind: "fact",
  });
  await api(endpoint("/memories"), "POST", {
    text: "For the Atlas kickoff, I prefer a one-page brief with an owner beside each action item.",
    kind: "preference",
  });
  const prior = await sendTurn(
    "Atlas kickoff decision: The pilot is capped at twelve participants, and Hana will collect feedback every Friday.",
    true,
  );
  const writer = await addSession({ title: "Atlas follow-up", agent: "fieldnote_writer" });
  await openSession(writer);
  $("question").value = "What should I put in the Atlas kickoff brief? Include the pilot decision.";
  $("question").focus();
  notify(prior.captured ? "Atlas follow-up is ready. Send the drafted question."
    : "Atlas follow-up is ready. The planning chat was saved locally because Meko capture failed.", !prior.captured);
}

for (const id of ["open-demo", "welcome-demo"]) {
  $(id).addEventListener("click", () => { if (!state.busy) $("demo-dialog").showModal(); });
}
$("load-demo").addEventListener("click", () => run(loadDemo));
for (const control of document.querySelectorAll("[data-close]")) {
  control.addEventListener("click", () => $(control.dataset.close).close());
}
$("show-chat").addEventListener("click", () => { if (!state.busy) selectView("chat"); });
$("show-library").addEventListener("click", () => run(async () => {
  await readLibrary();
  selectView("library");
}));
$("new-session").addEventListener("click", () => {
  if (state.busy) return;
  $("session-form").reset();
  $("session-dialog").showModal();
});
$("session-form").addEventListener("submit", (event) => {
  event.preventDefault();
  run(async () => {
    const session = await addSession(Object.fromEntries(new FormData(event.target)));
    $("session-dialog").close();
    await openSession(session);
    selectView("chat");
    $("question").focus();
  });
});
$("delete-session").addEventListener("click", () => run(async () => {
  if (!await confirmDelete("Delete this chat and its trace history? Separately saved memories remain.")) return;
  await api(endpoint(), "DELETE", {});
  state.sessions = state.sessions.filter((session) => session.id !== state.active.id);
  await openSession(state.sessions[0] || await addSession({ title: "New session" }));
  notify("Chat deleted.");
}));
$("compose").addEventListener("submit", (event) => {
  event.preventDefault();
  run(async () => {
    const question = $("question").value;
    const turn = await sendTurn(question, $("recall").checked);
    if (turn && $("question").value === question) $("question").value = "";
  });
});
$("question").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing && event.keyCode !== 229) {
    event.preventDefault();
    $("compose").requestSubmit();
  }
});
$("add-memory").addEventListener("click", () => {
  if (state.busy) return;
  $("memory-form").reset();
  $("memory-dialog").showModal();
});
$("memory-form").addEventListener("submit", (event) => {
  event.preventDefault();
  run(async () => {
    await api(endpoint("/memories"), "POST", Object.fromEntries(new FormData(event.target)));
    $("memory-dialog").close();
    state.query = "";
    state.page = 1;
    $("search").value = "";
    await readLibrary();
    notify("Memory saved.");
  });
});
$("search-form").addEventListener("submit", (event) => {
  event.preventDefault();
  run(async () => {
    state.query = $("search").value.trim();
    state.page = 1;
    await readLibrary();
  });
});
$("clear-search").addEventListener("click", () => run(async () => {
  $("search").value = "";
  state.query = "";
  state.page = 1;
  await readLibrary();
}));
for (const [id, offset] of [["page-back", -1], ["page-next", 1]]) {
  $(id).addEventListener("click", () => run(async () => {
    state.page += offset;
    await readLibrary();
  }));
}

run(async () => {
  Object.assign(state, await api("/workspace"));
  for (const [id, label] of Object.entries(state.agents)) {
    const option = node("option", "", label);
    option.value = id;
    $("agent").append(option);
  }
  const cloud = state.provider === "meko";
  $("engine-name").textContent = cloud ? "Meko MCP" : "Local SQLite";
  $("engine-details").textContent = cloud ? "Uses your Meko datapack for recall and capture."
    : "Keyless workspace. Set MEKO_API_KEY to connect Meko.";
  $("responder").textContent = state.model ? "Local model · " + state.model : "Evidence brief · Keyless";
  await openSession(state.sessions[0] || await addSession({ title: "First session" }));
});

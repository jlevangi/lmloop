/* lmloop dashboard.
 *
 * Three decisions worth stating.
 *
 * **Views, not dialogs.** A run detail is a page of content, and a modal is the
 * wrong container for one: it fights the scroll, it has no back button, and it
 * reads as provisional. Routing on the hash gives real navigation, working
 * browser back, and links you can send yourself.
 *
 * **Rows are patched, never rebuilt.** The poll used to replace every node each
 * cycle, which restarted animations and made the page flinch every few seconds.
 * Each row is created once, keyed by run id, and updated in place; only the
 * fields that changed are touched.
 *
 * **Polling, not streaming.** A run emits an event every few seconds at most and
 * lives for hours, so a socket would buy nothing and cost a reconnect story.
 * Polling also degrades correctly when a phone sleeps, which is most of the time
 * a run is being watched.
 */

const $ = (id) => document.getElementById(id);
const state = {
  config: null, runs: [], project: null, timer: null,
  route: { name: "list" }, rows: new Map(), detailKey: null,
};

/* ── Fetch ─────────────────────────────────────────────────────────────── */

async function api(path, options = {}) {
  const init = { headers: {}, ...options };
  if (options.body) {
    init.method = options.method || "POST";
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(options.body);
    if (state.config?.csrf) init.headers["X-CSRF-Token"] = state.config.csrf;
  }
  const response = await fetch(path, init);
  if (response.status === 401) { location.href = "/login"; throw new Error("unauthenticated"); }
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
  return payload;
}

/* ── Formatting ────────────────────────────────────────────────────────── */

const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;

function duration(seconds) {
  if (seconds == null) return "";
  const m = Math.floor(seconds / 60);
  return m < 60 ? `${m}m` : `${Math.floor(m / 60)}h${String(m % 60).padStart(2, "0")}`;
}

const ago = (s) => (s == null ? "never" : s < 60 ? "just now" : `${duration(s)} ago`);

/* The poll is every few seconds; a clock that only moves when it lands looks
 * stuck. Elapsed is extrapolated from when the figure was fetched, so the page
 * keeps time on its own between updates. */
function liveElapsed(run) {
  const drift = (Date.now() - (state.fetchedAt || Date.now())) / 1000;
  return duration(Math.round((run.elapsed_seconds || 0) + drift));
}

function el(tag, className, content) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (content != null) node.textContent = content;
  return node;
}

function metaBits(run) {
  const bits = [];
  if (run.iteration) bits.push(`iter ${run.iteration}/${run.max_iterations ?? "?"}`);
  if (run.commits) bits.push(plural(run.commits, "commit"));
  // The tool is already on the activity line for a running run; repeating it
  // here just spends a phone's width saying the same word twice.
  if (run.state !== "running" && run.last_tool) bits.push(run.last_tool);
  if (run.state === "running" && run.elapsed_seconds != null) bits.push(liveElapsed(run));
  if (run.compactions) bits.push(`${run.compactions} ovf`);
  if (run.model) bits.push(run.model.split("/").pop());
  bits.push(ago(run.age_seconds));
  return bits;
}

/* ── Row (created once, patched thereafter) ────────────────────────────── */

function makeRow(run) {
  const node = el("button", "row");
  node.type = "button";

  const head = el("div", "row-head");
  const where = el("span", "where");
  const stateLabel = el("span", "state");
  head.append(where, stateLabel);

  const title = el("h3", "title");
  const progress = el("div", "progress");
  const track = el("div", "track");
  const fill = el("span");
  const steps = el("small");
  track.append(fill);
  progress.append(track, steps);

  const now = el("div", "now");
  const nowStep = el("span", "now-step");
  const nowAct = el("span", "now-act");
  now.append(nowStep, nowAct);

  const pips = el("div", "pips");
  const meta = el("div", "meta");

  node.append(head, title, progress, now, pips, meta);
  node.addEventListener("click", () => go(`#${run.project}/${run.run_id}`));

  const parts = { node, where, stateLabel, title, progress, track, fill, steps,
                  now, nowStep, nowAct, pips, meta, last: null };
  patchRow(parts, run);
  return parts;
}

function patchRow(parts, run) {
  // Cheap guard: nothing below runs unless something the row shows has moved.
  const key = JSON.stringify([
    run.state, run.title, run.plan_done, run.plan_total, run.outcomes,
    run.iteration, run.commits, run.last_tool, run.last_target, run.current_step,
    run.elapsed_seconds, run.compactions, run.model, run.age_seconds,
  ]);
  if (key === parts.last) return;
  parts.last = key;

  parts.where.textContent = run.project;
  parts.stateLabel.textContent = run.state;
  parts.stateLabel.className = `state ${run.state}`;
  parts.title.textContent = run.title;

  if (run.plan_total) {
    parts.progress.hidden = false;
    parts.fill.style.width = `${Math.round((run.plan_done / run.plan_total) * 100)}%`;
    parts.steps.textContent = `${run.plan_done}/${run.plan_total}`;
  } else {
    parts.progress.hidden = true;
  }

  // What it is doing, right now, without opening anything.
  const live = run.state === "running";
  parts.track.classList.toggle("working", live);
  parts.now.hidden = !(live && (run.current_step || run.last_tool));
  parts.nowStep.textContent = run.current_step || "";
  parts.nowAct.textContent = live
    ? [run.last_tool, run.last_target].filter(Boolean).join(" ")
    : "";

  const outcomes = run.outcomes || [];
  parts.pips.hidden = outcomes.length === 0;
  while (parts.pips.children.length > outcomes.length) parts.pips.lastChild.remove();
  outcomes.forEach((outcome, index) => {
    const pip = parts.pips.children[index] || parts.pips.appendChild(el("span"));
    pip.className = `pip ${outcome}`;
  });
  parts.pips.title = outcomes.join(", ");

  const bits = metaBits(run);
  while (parts.meta.children.length > bits.length) parts.meta.lastChild.remove();
  const clockAt = bits.indexOf(live && run.elapsed_seconds != null ? liveElapsed(run) : "\u0000");
  bits.forEach((bit, index) => {
    const span = parts.meta.children[index] || parts.meta.appendChild(el("span"));
    span.textContent = bit;
    span.className = index === clockAt ? "clock" : "";
  });
}

function syncGroup(container, runs) {
  runs.forEach((run, index) => {
    let parts = state.rows.get(run.run_id);
    if (!parts) {
      parts = makeRow(run);
      state.rows.set(run.run_id, parts);
    } else {
      patchRow(parts, run);
    }
    const atIndex = container.children[index];
    if (atIndex !== parts.node) container.insertBefore(parts.node, atIndex || null);
  });
  while (container.children.length > runs.length) container.lastChild.remove();
}

/* ── List ──────────────────────────────────────────────────────────────── */

const ACTIVE = new Set(["running", "paused", "stopping"]);

function renderList() {
  const runs = state.project ? state.runs.filter((r) => r.project === state.project) : state.runs;
  const active = runs.filter((r) => ACTIVE.has(r.state));
  const rest = runs.filter((r) => !ACTIVE.has(r.state));

  syncGroup($("active"), active);
  syncGroup($("finished"), rest);
  $("active-count").textContent = active.length;
  $("finished-count").textContent = rest.length;
  $("active-group").hidden = active.length === 0;
  $("finished-group").hidden = rest.length === 0;
  $("empty").hidden = runs.length > 0;

  const live = active.some((r) => r.state === "running");
  const stale = runs.some((r) => r.state === "stale");
  $("moon").className = `moon ${live ? "live" : stale ? "stale" : ""}`;
  if (state.route.name === "list") {
    $("bar-sub").textContent = live
      ? `${plural(active.length, "run")} working`
      : `${plural(runs.length, "run")}, idle`;
    $("bar-sub").classList.toggle("warn", stale);
  }
}

function renderFilters() {
  const projects = [...new Set(state.runs.map((r) => r.project))].sort();
  const holder = $("filters");
  holder.replaceChildren();
  if (projects.length < 2) return;
  const tab = (label, value) => {
    const node = el("button", "tab", label);
    node.type = "button";
    node.setAttribute("aria-pressed", String(state.project === value));
    node.addEventListener("click", () => { state.project = value; renderFilters(); renderList(); });
    return node;
  };
  holder.append(tab("all", null), ...projects.map((p) => tab(p, p)));
}

/* ── Run view ──────────────────────────────────────────────────────────── */

function fact(label, value, hot = false) {
  const box = el("div", "fact");
  const dd = el("dd", hot ? "hot" : null, value);
  box.append(el("dt", null, label), dd);
  return box;
}

/* Plan steps are markdown the agent wrote for itself, so they arrive with
 * `**emphasis**` and `backticks` in them. Rendering those two is the difference
 * between a plan and a dump of a file; anything more would be a markdown
 * parser, which this is deliberately not. */
function inlineMarkdown(text) {
  const holder = document.createDocumentFragment();
  const pattern = /\*\*(.+?)\*\*|`([^`]+)`/g;
  let last = 0;
  for (const match of text.matchAll(pattern)) {
    if (match.index > last) holder.append(text.slice(last, match.index));
    if (match[1]) {
      const strong = el("strong");
      strong.append(inlineMarkdown(match[1]));  // `code` nests inside **bold**
      holder.append(strong);
    } else {
      holder.append(el("code", null, match[2]));
    }
    last = match.index + match[0].length;
  }
  if (last < text.length) holder.append(text.slice(last));
  return holder;
}

function planNodes(plan) {
  const holder = el("div", "plan");
  let seenNext = false;
  for (const line of plan.split("\n")) {
    const match = /^[-*]\s\[( |x|X)\]\s*(.*)$/.exec(line.trim());
    if (!match) continue;
    const done = match[1].toLowerCase() === "x";
    const isNext = !done && !seenNext;
    if (isNext) seenNext = true;
    const step = el("div", `step${done ? " done" : isNext ? " next" : ""}`);
    const label = el("span");
    label.append(inlineMarkdown(match[2]));
    step.append(el("span", "box", done ? "✓" : isNext ? "▸" : "·"), label);
    holder.append(step);
  }
  return holder;
}

const OUTCOME_CLASS = {
  ok: "ok", thrashing: "warn", truncated: "warn", "no-action": "warn",
  stalled: "bad", timeout: "bad", "agent-error": "bad", interrupted: "",
};

function iterationTable(rows) {
  const wrap = el("div", "scroll-x");
  const table = document.createElement("table");
  const head = document.createElement("tr");
  for (const label of ["#", "outcome", "time", "wr", "ovf", "plan", "commit"]) {
    head.append(el("th", null, label));
  }
  table.append(head);
  for (const row of rows) {
    const tr = document.createElement("tr");
    tr.append(el("td", null, row.iteration));
    tr.append(el("td", OUTCOME_CLASS[row.outcome] ?? "", row.outcome ?? "…"));
    tr.append(el("td", null, row.seconds ? duration(row.seconds) : ""));
    tr.append(el("td", null, row.writes ?? ""));
    tr.append(el("td", null, row.compactions || ""));
    tr.append(el("td", null, row.plan_total ? `${row.plan_done}/${row.plan_total}` : ""));
    tr.append(el("td", null, row.commit ? row.commit.slice(0, 8) : ""));
    table.append(tr);
  }
  wrap.append(table);
  return wrap;
}

function control(label, action, run, { risk = false, body = {} } = {}) {
  const button = el("button", risk ? "risk" : "quiet", label);
  button.type = "button";
  button.addEventListener("click", async () => {
    button.disabled = true;
    try {
      await api(`/api/runs/${run.project}/${run.run_id}/${action}`, { body });
      state.detailKey = null;
      await poll();
    } catch (error) {
      button.disabled = false;
      alert(error.message);
    }
  });
  return button;
}

async function renderRun(project, runId, { quiet = false } = {}) {
  const view = $("view-run");
  const summary = state.runs.find((r) => r.run_id === runId);
  const key = `${runId}:${summary?.updated_at || ""}:${summary?.state || ""}`;
  if (quiet && key === state.detailKey) return;

  $("bar-title").textContent = project;
  $("bar-sub").textContent = runId;

  if (!quiet) view.replaceChildren(el("p", "empty", "Loading…"));

  let run;
  try {
    run = await api(`/api/runs/${project}/${runId}`);
  } catch (error) {
    view.replaceChildren(el("p", "alert", error.message));
    return;
  }

  // Preserve what the reader was doing across a background refresh.
  const scroll = window.scrollY;
  const open = [...view.querySelectorAll("details")].map((node) => node.open);

  const parts = [];

  const facts = el("div", "facts");
  facts.append(
    fact("state", run.state, ACTIVE.has(run.state)),
    fact("plan", run.plan_total ? `${run.plan_done}/${run.plan_total}` : "—"),
    fact("commits", String(run.commits)),
    fact("iter", `${run.iteration ?? "?"}/${run.max_iterations ?? "?"}`),
    fact("updated", ago(run.age_seconds)),
  );
  parts.push(facts);

  if (ACTIVE.has(run.state) && (run.current_step || run.last_tool)) {
    const doing = el("div", "doing");
    doing.append(el("div", "label", run.paused ? "paused on" : "working on"));
    if (run.current_step) doing.append(el("div", "step", run.current_step));
    const act = [run.last_tool, run.last_target].filter(Boolean).join(" ");
    const line = el("div", "act");
    line.append(el("span", "clock", liveElapsed(run)));
    if (act) line.append(` · ${act}`);
    if (run.output_tokens) line.append(` · ${run.output_tokens} out`);
    if (run.quiet_seconds > 60) line.append(` · quiet ${duration(run.quiet_seconds)}`);
    doing.append(line);
    parts.push(doing);
  }

  if (!state.config?.read_only) {
    const controls = el("div", "controls");
    if (ACTIVE.has(run.state)) {
      controls.append(run.paused ? control("Resume", "resume", run) : control("Pause", "pause", run));
      if (!run.stopping) controls.append(control("Stop after this iteration", "stop", run, { risk: true }));
    } else {
      controls.append(control("Continue 3 iterations", "continue", run, { body: { iterations: 3 } }));
    }
    parts.push(controls);
  }

  parts.push(el("h2", null, "Objective"));
  const objective = el("div", "panel");
  objective.append(el("div", "objective", run.objective));
  parts.push(objective);

  if (run.plan) {
    parts.push(el("h2", null, `Plan — ${run.plan_done} of ${run.plan_total}`));
    const panel = el("div", "panel");
    panel.append(planNodes(run.plan));
    parts.push(panel);
  }

  if (run.iterations?.length) {
    parts.push(el("h2", null, "Iterations"));
    const panel = el("div", "panel");
    panel.append(iterationTable(run.iterations));
    parts.push(panel);
  }

  for (const [label, content] of [["Handoff", run.handoff], ["Notes", run.notes]]) {
    if (!content) continue;
    const box = document.createElement("details");
    box.append(el("summary", null, label), el("pre", null, content));
    parts.push(box);
  }

  view.replaceChildren(...parts);
  view.querySelectorAll("details").forEach((node, index) => { if (open[index]) node.open = true; });
  if (quiet) window.scrollTo(0, scroll);
  state.detailKey = key;
}

/* ── New run ───────────────────────────────────────────────────────────── */

async function renderNew() {
  $("bar-title").textContent = "New run";
  $("bar-sub").textContent = "one objective, many iterations";
  $("launch-error").hidden = true;

  const { projects } = await api("/api/projects");
  $("project").replaceChildren(...projects.map((p) => {
    const option = document.createElement("option");
    option.value = p.id;
    option.textContent = p.runs ? `${p.name} · ${plural(p.runs, "run")}` : p.name;
    return option;
  }));
  const NEW = "\u0000new";
  const fresh = document.createElement("option");
  fresh.value = NEW;
  fresh.textContent = "+ new project…";
  $("project").append(fresh);
  if (state.project) $("project").value = state.project;
  toggleNewProject();

  // Fetched here rather than at startup: it costs a couple of seconds and only
  // this form needs it.
  const catalogue = state.models || (state.models = await api("/api/models"));
  $("model").replaceChildren(...(catalogue.models || []).map((id) => {
    const option = document.createElement("option");
    option.value = option.textContent = id;
    return option;
  }));
  $("model").value = state.config.default_model;
  $("thinking").value = state.config.default_thinking || "";
  $("iterations").value = state.config.default_max_iterations;
}

function toggleNewProject() {
  const creating = $("project").value === "\u0000new";
  $("new-project-fields").hidden = !creating;
  $("project-name").required = creating;
  $("launch-submit").textContent = creating ? "Create project and start" : "Start run";
}

$("project").addEventListener("change", toggleNewProject);

$("launch-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = $("launch-submit");
  button.disabled = true;
  try {
    let project = $("project").value;
    if (project === "\u0000new") {
      // Create first, then run against it: two calls, but one action as far as
      // anyone using it is concerned.
      const made = await api("/api/projects", {
        body: { name: $("project-name").value.trim(), objective: $("objective").value },
      });
      project = made.id;
      state.project = made.id;
    }
    await api("/api/runs", {
      body: {
        project,
        objective: $("objective").value,
        model: $("model").value,
        thinking: $("thinking").value,
        max_iterations: Number($("iterations").value),
      },
    });
    $("objective").value = "";
    $("project-name").value = "";
    state.models = state.models;   // catalogue is still good
    await poll();
    go("#");
  } catch (error) {
    $("launch-error").textContent = error.message;
    $("launch-error").hidden = false;
  } finally {
    button.disabled = false;
  }
});

/* ── Routing ───────────────────────────────────────────────────────────── */

function go(hash) {
  if (location.hash === hash) route();
  else location.hash = hash;
}

function parseHash() {
  const raw = decodeURIComponent(location.hash.slice(1));
  if (raw === "new") return { name: "new" };
  const [project, runId] = raw.split("/");
  if (project && runId) return { name: "run", project, run_id: runId };
  return { name: "list" };
}

async function route({ quiet = false } = {}) {
  const next = parseHash();
  const changed = JSON.stringify(next) !== JSON.stringify(state.route);
  state.route = next;
  if (changed) state.detailKey = null;

  $("view-list").hidden = next.name !== "list";
  $("view-run").hidden = next.name !== "run";
  $("view-new").hidden = next.name !== "new";
  $("back").hidden = next.name === "list";
  $("new-run").hidden = next.name !== "list" || Boolean(state.config?.read_only);

  if (next.name === "list") {
    $("bar-title").textContent = "lmloop";
    renderList();
  } else if (next.name === "run") {
    await renderRun(next.project, next.run_id, { quiet: quiet && !changed });
  } else if (changed) {
    await renderNew();
  }
}

window.addEventListener("hashchange", () => route());
$("back").addEventListener("click", () => (history.length > 1 ? history.back() : go("#")));
$("new-run").addEventListener("click", () => go("#new"));

/* ── Poll ──────────────────────────────────────────────────────────────── */

async function poll() {
  try {
    const { runs } = await api("/api/runs");
    state.runs = runs;
    state.fetchedAt = Date.now();
    renderFilters();
    await route({ quiet: true });
  } catch (error) {
    $("bar-sub").textContent = error.message;
    $("bar-sub").classList.add("warn");
    $("moon").className = "moon stale";
  }
}

function schedule() {
  clearTimeout(state.timer);
  const seconds = document.hidden ? state.config.hidden_poll_seconds : state.config.poll_seconds;
  state.timer = setTimeout(async () => { await poll(); schedule(); }, seconds * 1000);
}

document.addEventListener("visibilitychange", schedule);

// One second, only while something is running and the tab is visible.
setInterval(() => {
  if (document.hidden) return;
  if (!state.runs.some((run) => run.state === "running")) return;
  for (const [runId, parts] of state.rows) {
    const run = state.runs.find((item) => item.run_id === runId);
    if (!run || run.state !== "running") continue;
    const clock = parts.meta.querySelector(".clock");
    if (clock) clock.textContent = liveElapsed(run);
  }
  const panel = document.querySelector("#view-run .clock");
  const shown = state.runs.find((run) => run.run_id === state.route.run_id);
  if (panel && shown?.state === "running") panel.textContent = liveElapsed(shown);
}, 1000);

(async function start() {
  try {
    state.config = await api("/api/config");
  } catch {
    return;
  }
  await poll();
  schedule();
})();

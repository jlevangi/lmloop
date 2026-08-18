/* lmloop dashboard.
 *
 * Polls rather than streams. A run emits an event every few seconds at most and
 * lives for hours, so a websocket would buy nothing and cost a reconnect story;
 * polling also degrades correctly when the phone sleeps, which is most of the
 * time a run is being watched. The interval drops when the tab is hidden.
 */

const $ = (id) => document.getElementById(id);
const state = { config: null, runs: [], project: null, timer: null, openRun: null, detailSignature: null };

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
  if (m < 60) return `${m}m`;
  return `${Math.floor(m / 60)}h${String(m % 60).padStart(2, "0")}m`;
}

function ago(seconds) {
  if (seconds == null) return "never";
  if (seconds < 60) return "just now";
  return `${duration(seconds)} ago`;
}

function text(tag, className, content) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (content != null) node.textContent = content;
  return node;
}

/* ── Run card ──────────────────────────────────────────────────────────── */

function card(run) {
  const node = text("button", "card");
  node.type = "button";

  const top = text("div", "card-top");
  top.append(text("span", "project", run.project), text("span", `state ${run.state}`, run.state));
  node.append(top, text("h3", "title", run.title));

  if (run.plan_total) {
    const wrap = text("div", "progress");
    const bar = text("div", "bar");
    const fill = text("span");
    fill.style.width = `${Math.round((run.plan_done / run.plan_total) * 100)}%`;
    bar.append(fill);
    wrap.append(bar, text("small", null, `${run.plan_done} of ${run.plan_total} steps`));
    node.append(wrap);
  }

  if (run.outcomes?.length) {
    const pips = text("div", "outcomes");
    for (const outcome of run.outcomes) pips.append(text("span", `pip ${outcome}`));
    pips.title = run.outcomes.join(", ");
    node.append(pips);
  }

  const bits = [];
  if (run.iteration) bits.push(`iteration ${run.iteration}/${run.max_iterations ?? "?"}`);
  if (run.commits) bits.push(plural(run.commits, "commit"));
  if (run.state === "running" && run.last_tool) bits.push(run.last_tool);
  if (run.state === "running" && run.elapsed_seconds) bits.push(duration(run.elapsed_seconds) + " in");
  if (run.compactions) bits.push(`${run.compactions} overflow`);
  if (run.model) bits.push(run.model.split("/").pop());
  bits.push(ago(run.age_seconds));

  const meta = text("div", "meta");
  for (const bit of bits) meta.append(text("span", null, bit));
  node.append(meta);

  node.addEventListener("click", () => openDetail(run));
  return node;
}

/* ── List ──────────────────────────────────────────────────────────────── */

const ACTIVE = new Set(["running", "paused", "stopping"]);

function render() {
  const runs = state.project ? state.runs.filter((r) => r.project === state.project) : state.runs;
  const active = runs.filter((r) => ACTIVE.has(r.state));
  const rest = runs.filter((r) => !ACTIVE.has(r.state));

  for (const [key, list] of [["active", active], ["finished", rest]]) {
    const holder = $(key);
    holder.replaceChildren(...list.map(card));
    $(`${key}-count`).textContent = list.length;
    $(`${key}-section`).hidden = list.length === 0;
  }
  $("empty").hidden = runs.length > 0;

  const live = active.some((r) => r.state === "running");
  const stale = runs.some((r) => r.state === "stale");
  $("pulse").className = `pulse ${live ? "live" : stale ? "stale" : ""}`;
  $("connection").textContent = live
    ? `${plural(active.length, "run")} active`
    : `${plural(runs.length, "run")}, none active`;
  $("connection").classList.toggle("warn", stale);
}

function renderFilters() {
  const projects = [...new Set(state.runs.map((r) => r.project))].sort();
  const holder = $("filters");
  holder.replaceChildren();
  if (projects.length < 2) return;
  const make = (label, value) => {
    const chip = text("button", "chip", label);
    chip.type = "button";
    chip.setAttribute("aria-pressed", String(state.project === value));
    chip.addEventListener("click", () => { state.project = value; renderFilters(); render(); });
    return chip;
  };
  holder.append(make("All", null), ...projects.map((p) => make(p, p)));
}

/* ── Detail ────────────────────────────────────────────────────────────── */

function planNodes(plan) {
  const holder = text("div", "plan");
  for (const line of plan.split("\n")) {
    const stripped = line.trim();
    const match = /^[-*]\s\[( |x|X)\]\s*(.*)$/.exec(stripped);
    if (match) {
      const done = match[1].toLowerCase() === "x";
      const step = text("div", `step${done ? " done" : ""}`);
      step.append(text("span", "box", done ? "✓" : "○"), text("span", null, match[2]));
      holder.append(step);
    } else if (stripped && !stripped.startsWith("#")) {
      holder.append(text("div", "hint", stripped));
    }
  }
  return holder;
}

function iterationTable(rows) {
  const wrap = text("div", "scroll-x");
  const table = document.createElement("table");
  const head = document.createElement("tr");
  for (const label of ["#", "outcome", "time", "writes", "ovf", "plan", "commit"]) {
    head.append(text("th", null, label));
  }
  table.append(head);
  for (const row of rows) {
    const tr = document.createElement("tr");
    const cells = [
      row.iteration,
      row.outcome ?? "…",
      row.seconds ? duration(row.seconds) : "",
      row.writes ?? "",
      row.compactions || "",
      row.plan_total ? `${row.plan_done}/${row.plan_total}` : "",
      row.commit ? row.commit.slice(0, 8) : "",
    ];
    for (const value of cells) tr.append(text("td", null, value));
    table.append(tr);
  }
  wrap.append(table);
  return wrap;
}

function controlButton(label, action, run, { danger = false, body = {} } = {}) {
  const button = text("button", danger ? "danger" : "secondary", label);
  button.type = "button";
  button.addEventListener("click", async () => {
    button.disabled = true;
    try {
      await api(`/api/runs/${run.project}/${run.run_id}/${action}`, { body });
      await refresh();
      state.detailSignature = null;
      openDetail(state.runs.find((r) => r.run_id === run.run_id) || run);
    } catch (error) {
      alert(error.message);
      button.disabled = false;
    }
  });
  return button;
}

/* Re-rendering the sheet under someone's thumb is the difference between a
 * dashboard and a jack-in-the-box: the poll used to rebuild it every few
 * seconds, which threw away the scroll position and slammed every <details>
 * shut mid-read. So a refresh only redraws when the run actually changed, and
 * carries the reader's place across when it does. */
function captureView() {
  const body = $("detail-body");
  return {
    scroll: body.scrollTop,
    open: [...body.querySelectorAll("details")].map((node) => node.open),
  };
}

function restoreView(view) {
  if (!view) return;
  const body = $("detail-body");
  body.querySelectorAll("details").forEach((node, index) => {
    if (view.open[index]) node.open = true;
  });
  body.scrollTop = view.scroll;
}

async function openDetail(summary, { refresh = false } = {}) {
  const signature = `${summary.run_id}:${summary.updated_at || ""}:${summary.state || ""}`;
  if (refresh && signature === state.detailSignature) return;

  state.openRun = summary.run_id;
  const hash = `#${summary.project}/${summary.run_id}`;
  if (location.hash !== hash) history.replaceState(null, "", hash);
  $("detail-title").textContent = summary.project;
  const body = $("detail-body");
  const view = refresh ? captureView() : null;
  if (!refresh) body.replaceChildren(text("p", "hint", "Loading…"));
  present($("detail"));

  let run;
  try {
    run = await api(`/api/runs/${summary.project}/${summary.run_id}`);
  } catch (error) {
    body.replaceChildren(text("p", "error", error.message));
    return;
  }

  const parts = [];

  const meta = text("div", "detail-meta");
  for (const bit of [
    run.state,
    run.model,
    `iteration ${run.iteration ?? "?"}/${run.max_iterations ?? "?"}`,
    plural(run.commits, "commit"),
    `updated ${ago(run.age_seconds)}`,
  ]) meta.append(text("span", null, bit));
  parts.push(meta);

  if (!state.config?.read_only) {
    const controls = text("div", "controls");
    if (ACTIVE.has(run.state)) {
      controls.append(
        run.paused
          ? controlButton("Resume", "resume", run)
          : controlButton("Pause", "pause", run),
      );
      if (!run.stopping) controls.append(controlButton("Stop after this iteration", "stop", run, { danger: true }));
    } else {
      controls.append(controlButton("Continue 3 more iterations", "continue", run, { body: { iterations: 3 } }));
    }
    parts.push(controls);
  }

  parts.push(text("h2", null, "Objective"), text("div", "objective", run.objective));

  if (run.plan) {
    parts.push(text("h2", null, `Plan — ${run.plan_done} of ${run.plan_total} done`));
    parts.push(planNodes(run.plan));
  }

  if (run.iterations?.length) {
    parts.push(text("h2", null, "Iterations"), iterationTable(run.iterations));
  }

  for (const [label, content] of [["Handoff", run.handoff], ["Notes", run.notes]]) {
    if (!content) continue;
    const box = document.createElement("details");
    box.append(text("summary", null, label), text("pre", null, content));
    parts.push(box);
  }

  body.replaceChildren(...parts);
  state.detailSignature = signature;
  restoreView(view);
}

$("detail").addEventListener("close", () => {
  state.openRun = null;
  state.detailSignature = null;
  if (location.hash) history.replaceState(null, "", location.pathname);
});

/* A run is a thing you send someone, or open on your phone from a message to
 * yourself. The hash is project/run-id, which is exactly what the API path is. */
function hashTarget() {
  const [project, runId] = decodeURIComponent(location.hash.slice(1)).split("/");
  return project && runId ? { project, run_id: runId } : null;
}

async function openFromHash() {
  // `#new` is worth a link of its own: it makes "start a run" a home-screen
  // shortcut rather than a page you have to land on and then tap.
  if (location.hash === "#new") return openLaunch();
  const target = hashTarget();
  if (target && target.run_id !== state.openRun) await openDetail(target);
}

window.addEventListener("hashchange", openFromHash);

/* ── Launch ────────────────────────────────────────────────────────────── */

async function openLaunch() {
  /* Open first, populate second. Fetching before showModal() meant a tap on
   * "New run" did nothing at all until the round-trip finished, which reads as
   * a dropped tap and gets tapped again. */
  $("launch-error").hidden = true;
  present($("launch"));

  const { projects } = await api("/api/projects");
  const select = $("project");
  select.replaceChildren(...projects.map((p) => {
    const option = document.createElement("option");
    option.value = p.id;
    option.textContent = p.runs ? `${p.name} (${plural(p.runs, "run")})` : p.name;
    return option;
  }));
  if (state.project) select.value = state.project;

  const models = $("model");
  models.replaceChildren(...(state.config.models || []).map((id) => {
    const option = document.createElement("option");
    option.value = option.textContent = id;
    return option;
  }));
  models.value = state.config.default_model;
  $("thinking").value = state.config.default_thinking || "";
  $("iterations").value = state.config.default_max_iterations;
}

$("launch-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = $("launch-submit");
  button.disabled = true;
  try {
    await api("/api/runs", {
      body: {
        project: $("project").value,
        objective: $("objective").value,
        model: $("model").value,
        thinking: $("thinking").value,
        max_iterations: Number($("iterations").value),
      },
    });
    $("launch").close();
    $("objective").value = "";
    await refresh();
  } catch (error) {
    $("launch-error").textContent = error.message;
    $("launch-error").hidden = false;
  } finally {
    button.disabled = false;
  }
});

$("new-run").addEventListener("click", () => {
  openLaunch().catch((error) => {
    $("launch-error").textContent = error.message;
    $("launch-error").hidden = false;
  });
});

/* A modal autofocuses its first focusable child, which here is the close
 * button -- so every sheet opened with a ring around its dismiss control. Focus
 * the sheet itself instead: still keyboard-reachable, no misleading target. */
for (const id of ["detail", "launch"]) {
  // A click landing on the dialog element itself is a click on the backdrop:
  // everything inside is a child. Tapping away to dismiss is what a bottom
  // sheet is expected to do on a phone.
  $(id).addEventListener("click", (event) => {
    if (event.target === $(id)) $(id).close();
  });
}

/* showModal() focuses the first focusable descendant, which in both sheets is
 * the close button -- so every sheet opened with a ring drawn around the one
 * control that throws the sheet away. Move focus to the sheet itself. */
function present(dialog) {
  if (!dialog.open) dialog.showModal();
  dialog.focus();
}

/* ── Poll ──────────────────────────────────────────────────────────────── */

async function refresh() {
  try {
    const { runs } = await api("/api/runs");
    state.runs = runs;
    renderFilters();
    render();
  } catch (error) {
    $("connection").textContent = error.message;
    $("connection").classList.add("warn");
    $("pulse").className = "pulse stale";
  }
}

function schedule() {
  clearTimeout(state.timer);
  const seconds = document.hidden
    ? state.config.hidden_poll_seconds
    : state.config.poll_seconds;
  state.timer = setTimeout(async () => {
    await refresh();
    if (state.openRun) {
      const run = state.runs.find((r) => r.run_id === state.openRun);
      if (run && $("detail").open) openDetail(run, { refresh: true });
    }
    schedule();
  }, seconds * 1000);
}

document.addEventListener("visibilitychange", schedule);

(async function start() {
  try {
    state.config = await api("/api/config");
  } catch {
    return;
  }
  if (state.config.read_only) $("new-run").hidden = true;
  await refresh();
  await openFromHash();
  schedule();
})();

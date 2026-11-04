// The presenter view /panel/screen (showcase.md §7). Polls /panel/screen/feed every 1.5 s,
// lays out one column per agent and appends a card per row. It follows the newest run
// unless ?run_id= is in the URL. Text is set with textContent only, never as HTML.
"use strict";

(function () {
  const POLL_MS = 1500;
  const pinned = new URLSearchParams(window.location.search).get("run_id");
  const columnsEl = document.getElementById("columns");
  const waitingEl = document.getElementById("waiting");
  const kindEl = document.getElementById("run-kind");
  const elapsedEl = document.getElementById("elapsed");

  let runId = null;
  let after = null;
  let run = null;
  let skewMs = 0;
  let seen = new Set();
  let columns = new Map();

  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function clock(seconds) {
    const s = Math.max(0, Math.floor(seconds));
    const m = Math.floor(s / 60);
    return m + ":" + String(s % 60).padStart(2, "0");
  }

  function reset(next) {
    runId = next.run_id;
    after = null;
    seen = new Set();
    columns = new Map();
    columnsEl.replaceChildren();
    kindEl.textContent = next.kind;
    kindEl.hidden = false;
  }

  function column(agent) {
    let col = columns.get(agent.id);
    if (col) return col;
    const section = el("section", "column");
    const head = el("h2", "column-head");
    head.append(el("span", "column-icon", (agent.name || "?").charAt(0).toUpperCase()));
    head.append(el("span", "column-name", agent.name));
    const cards = el("div", "column-cards");
    section.append(head, cards);
    columnsEl.append(section);
    col = cards;
    columns.set(agent.id, col);
    return col;
  }

  function card(row) {
    if (row.kind === "message") {
      const c = el("article", "card message");
      c.append(el("span", "label", row.type));
      c.append(el("span", "summary", row.summary));
      c.append(el("span", "meta", "→ " + row.to));
      return c;
    }
    if (row.kind === "decision") {
      const model = row.source === "model";
      const c = el("article", "card decision" + (model ? " model" : ""));
      c.append(el("span", "label", model ? "Model: Spark" : "Rule default"));
      c.append(el("span", "summary", row.choice ? row.agent + " chose " + row.choice : row.node));
      const meta = row.latency_ms !== null && row.latency_ms !== undefined
        ? row.node + " · " + row.latency_ms + " ms"
        : row.node;
      c.append(el("span", "meta", meta));
      return c;
    }
    if (row.kind === "check") {
      const ok = row.result === "ok";
      const c = el("article", "card check" + (ok ? "" : " stop"));
      c.append(el("span", "label", row.tool));
      c.append(el("span", "summary", ok ? "passed" : row.result));
      return c;
    }
    if (row.kind === "approval") {
      const c = el("article", "card approval");
      c.append(el("span", "label", "Approval"));
      c.append(el("span", "summary", row.status));
      return c;
    }
    return null;
  }

  function render(feed) {
    if (!feed.run) {
      waitingEl.hidden = false;
      return;
    }
    waitingEl.hidden = true;
    if (feed.run.run_id !== runId) reset(feed.run);
    run = feed.run;
    skewMs = Date.parse(feed.now) - Date.now();
    const byId = new Map();
    for (const agent of feed.agents) {
      byId.set(agent.id, agent);
      column(agent);
    }
    for (const row of feed.rows) {
      if (seen.has(row.seq)) continue;
      seen.add(row.seq);
      const owner = row.kind === "message" ? row.from_id : row.agent_id;
      const node = card(row);
      if (!node) continue;
      const col = column(byId.get(owner) || { id: owner, name: row.from || row.agent });
      col.append(node);
      node.scrollIntoView({ block: "nearest" });
    }
    if (feed.next) after = feed.next;
    const t = feed.totals;
    if (t) {
      document.getElementById("t-agents").textContent = t.agents;
      document.getElementById("t-messages").textContent = t.messages;
      document.getElementById("t-model").textContent = t.model_calls;
      document.getElementById("t-tokens").textContent = t.tokens.toLocaleString();
      document.getElementById("t-elapsed").textContent = clock(t.elapsed_s);
    }
  }

  function tick() {
    if (!run) return;
    const end = run.ended_at ? Date.parse(run.ended_at) : Date.now() + skewMs;
    elapsedEl.textContent = clock((end - Date.parse(run.started_at)) / 1000);
  }

  async function poll() {
    const params = new URLSearchParams();
    if (pinned) params.set("run_id", pinned);
    if (after && (pinned || runId)) params.set("after", after);
    try {
      const resp = await fetch("/panel/screen/feed?" + params.toString(), {
        credentials: "same-origin",
        cache: "no-store",
      });
      if (resp.ok) render(await resp.json());
    } catch (err) {
      // A missed poll is retried on the next one.
    }
    window.setTimeout(poll, POLL_MS);
  }

  window.setInterval(tick, 1000);
  poll();
})();

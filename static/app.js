const $ = (sel) => document.querySelector(sel);

let lastResult = null;
let lastQuery = "";
let lastContext = "";
let currentSessionId = null;

const SAMPLE = `-- Sample: a "where are we bleeding money?" query
-- This one has a lot of the classic traps.

SELECT u.country, COUNT(DISTINCT o.id) AS order_count,
       (SELECT MAX(created_at) FROM orders o2 WHERE o2.user_id = u.id) AS last_order
FROM users u, orders o
WHERE u.signup_date BETWEEN '2026-01-01' AND '2026-01-31'
  AND u.email LIKE '%@gmail.com'
  AND YEAR(u.created_at) = 2026
  AND o.user_id = u.id
GROUP BY u.country
ORDER BY order_count DESC;
`;

$("#sample").addEventListener("click", () => {
  $("#query").value = SAMPLE;
  $("#context").value = "Snowflake on a 50M row events table, prod warehouse, daily batch — slow on month-end";
  $("#flavor").value = "snowflake";
});

$("#run").addEventListener("click", run);
$("#save").addEventListener("click", saveSession);
$("#patch").addEventListener("click", patchSession);
$("#export-md").addEventListener("click", () => exportSession("md"));
$("#export-json").addEventListener("click", () => exportSession("json"));

async function run() {
  const query = $("#query").value.trim();
  const flavor = $("#flavor").value;
  const contextRaw = $("#context").value.trim();
  const context = flavor ? `${flavor} — ${contextRaw}` : contextRaw;
  if (!query) { alert("Paste a query first."); return; }
  $("#run").disabled = true;
  $("#run").textContent = "Analyzing…";
  try {
    const r = await fetch("/api/analyze", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query, context }),
    });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const data = await r.json();
    lastResult = data;
    lastQuery = query;
    lastContext = context;
    currentSessionId = null;
    render(data);
    $("#results").classList.remove("hidden");
    $("#saved-detail").classList.add("hidden");
    $("#saved").textContent = "";
    $("#results").scrollIntoView({ behavior: "smooth" });
  } catch (e) {
    alert("Analysis failed: " + e.message);
  } finally {
    $("#run").disabled = false;
    $("#run").textContent = "Analyze";
  }
}

function renderVerdict(v) {
  const labels = {
    ship_ready: "Ship-ready",
    mostly_ready: "Mostly ready",
    needs_polish: "Needs polish",
    tight_but_usable: "Tight but usable",
    needs_rework: "Needs rework",
  };
  $("#verdict").innerHTML = `
    <div class="verdict-score">${v.scores.overall}<span style="font-size:20px;color:var(--fg-3)">/100</span></div>
    <div class="verdict-band ${v.verdict}">${labels[v.verdict]}</div>
    <div class="verdict-meta">
      <span>complexity: <strong>${v.complexity}</strong></span>
      <span>flavor: <strong>${v.flavor}</strong></span>
      <span>tables: <strong>${v.tables}</strong></span>
      <span>words: <strong>${v.word_count}</strong></span>
      ${v.context_summary.row_count ? `<span>~${formatNum(v.context_summary.row_count)} rows</span>` : ""}
      ${v.context_summary.is_prod ? `<span>prod</span>` : ""}
      ${v.context_summary.concerns.length ? `<span>concerns: ${v.context_summary.concerns.join(", ")}</span>` : ""}
    </div>`;
}

function formatNum(n) {
  if (n >= 1e9) return (n / 1e9).toFixed(1) + "B";
  if (n >= 1e6) return (n / 1e6).toFixed(1) + "M";
  if (n >= 1e3) return (n / 1e3).toFixed(1) + "K";
  return String(n);
}

function renderScores(s) {
  const axes = [
    ["correctness", "Correctness"],
    ["performance", "Performance"],
    ["readability", "Readability"],
    ["structure", "Structure"],
    ["portability", "Portability"],
  ];
  $("#scores").innerHTML = axes.map(([k, label]) => `
    <div class="score-pill">
      <div class="label">${label}</div>
      <div class="value" style="color:${scoreColor(s[k])}">${s[k]}</div>
    </div>`).join("");
  $("#tip").textContent = `💡 ${lastResult.tip}`;
}

function scoreColor(v) {
  if (v >= 85) return "var(--accent)";
  if (v >= 70) return "#a3e635";
  if (v >= 55) return "var(--warn)";
  if (v >= 40) return "#fb923c";
  return "var(--bad)";
}

function renderFindings(findings) {
  if (!findings.length) {
    $("#findings").innerHTML = `<div class="muted">No anti-patterns detected. Nice.</div>`;
    return;
  }
  $("#findings").innerHTML = findings.map(f => `
    <div class="finding ${f.severity}">
      <header><h3>${f.label}</h3><span class="severity">${f.severity}</span></header>
      <p>${f.description}</p>
      ${f.evidence ? `<code class="evidence">${escapeHtml(f.evidence)}</code>` : ""}
      <p class="fix">↳ ${f.fix}</p>
    </div>`).join("");
}

function renderPlan(plan) {
  if (!plan.length) {
    $("#plan").innerHTML = `<div class="muted">No plan steps projected.</div>`;
    return;
  }
  $("#plan").innerHTML = plan.map(p => `
    <div class="plan-step">
      <div class="op">${p.op}</div>
      <div class="detail">${p.detail}</div>
      <div class="cost ${p.est_cost}">${p.est_cost}</div>
    </div>`).join("");
}

function renderIndexes(idxs) {
  if (!idxs.length) {
    $("#indexes").innerHTML = `<div class="muted">No index changes suggested.</div>`;
    return;
  }
  $("#indexes").innerHTML = idxs.map(i => `
    <div class="idx">
      <code>${escapeHtml(i.column)}</code><span class="type">${escapeHtml(i.type)}</span>
      <div class="muted" style="margin-top:6px">${i.rationale}</div>
    </div>`).join("");
}

function renderRefactors(refs) {
  if (!refs.length) {
    $("#refactors").innerHTML = `<div class="muted">No refactors suggested — the query already uses the right patterns for the issues found.</div>`;
    return;
  }
  $("#refactors").innerHTML = refs.map(r => `
    <div class="refactor">
      <h3>${r.label}</h3>
      <div class="detail">${r.detail}</div>
      <div class="diff-block">
        <div><div class="label">Before</div><pre class="before">${escapeHtml(r.example_before)}</pre></div>
        <div><div class="label">After</div><pre class="after">${escapeHtml(r.example_after)}</pre></div>
      </div>
    </div>`).join("");
}

function renderFlavorHints(hints) {
  $("#flavor-hints").innerHTML = hints.map(h => `<div class="flavor-hint">${escapeHtml(h)}</div>`).join("");
}

function render(r) {
  renderVerdict(r);
  renderScores(r.scores);
  renderFindings(r.findings);
  renderPlan(r.plan);
  renderIndexes(r.indexes);
  renderRefactors(r.refactors);
  renderFlavorHints(r.flavor_hints);
}

async function saveSession() {
  if (!lastResult) { alert("Run an analysis first."); return; }
  const r = await fetch("/api/sessions", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ query: lastQuery, context: lastContext, result: lastResult }),
  });
  if (!r.ok) { alert("Save failed."); return; }
  const d = await r.json();
  currentSessionId = d.id;
  $("#saved").textContent = `Saved as #${d.id}`;
  $("#saved-detail").classList.remove("hidden");
  loadSessions();
}

async function patchSession() {
  if (!currentSessionId) { alert("Save first."); return; }
  const rating = parseInt($("#rating").value, 10);
  const notes = $("#notes").value;
  const body = {};
  if (Number.isFinite(rating) && rating >= 1 && rating <= 5) body.self_rating = rating;
  if (notes) body.notes = notes;
  if (!Object.keys(body).length) { alert("Add a rating or a note."); return; }
  const r = await fetch(`/api/sessions/${currentSessionId}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!r.ok) { alert("Patch failed."); return; }
  loadSessions();
}

async function exportSession(fmt) {
  if (!currentSessionId) { alert("Save first."); return; }
  const r = await fetch(`/api/export/${currentSessionId}?fmt=${fmt}`);
  if (!r.ok) { alert("Export failed."); return; }
  const d = await r.json();
  if (fmt === "md") {
    download(`sql-coach-session-${currentSessionId}.md`, d.markdown, "text/markdown");
  } else {
    download(`sql-coach-session-${currentSessionId}.json`, JSON.stringify(d, null, 2), "application/json");
  }
}

function download(name, content, type) {
  const blob = new Blob([content], { type });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  a.click();
  URL.revokeObjectURL(url);
}

async function loadSessions() {
  const r = await fetch("/api/sessions");
  if (!r.ok) return;
  const list = await r.json();
  if (!list.length) {
    $("#sessions").innerHTML = `<div class="muted">No saved sessions yet.</div>`;
    return;
  }
  $("#sessions").innerHTML = list.map(s => `
    <div class="session" data-id="${s.id}">
      <div class="row1">
        <span>#${s.id} · ${new Date(s.created_at).toLocaleString()}</span>
        <span class="verdict ${s.verdict}">${s.score || "?"}/100</span>
      </div>
      <div class="q">${escapeHtml((s.flavor || "?") + " · " + (s.complexity || "?"))}</div>
      <div class="meta">
        <span class="verdict ${s.verdict}">${s.verdict}</span>
        ${s.self_rating ? `<span class="stars">${'★'.repeat(s.self_rating)}${'☆'.repeat(5 - s.self_rating)}</span>` : ""}
      </div>
    </div>`).join("");
  document.querySelectorAll(".session").forEach(el => {
    el.addEventListener("click", async () => {
      const id = el.dataset.id;
      const r = await fetch(`/api/sessions/${id}`);
      const s = await r.json();
      lastResult = s.result;
      lastQuery = s.query_text;
      lastContext = s.context || "";
      currentSessionId = s.id;
      render(s.result);
      $("#query").value = s.query_text;
      $("#context").value = s.context || "";
      $("#saved").textContent = `Loaded #${s.id}`;
      $("#saved-detail").classList.remove("hidden");
      $("#rating").value = s.self_rating || "";
      $("#notes").value = s.notes || "";
      $("#results").classList.remove("hidden");
      $("#results").scrollIntoView({ behavior: "smooth" });
    });
  });
}

function escapeHtml(s) {
  return String(s || "").replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
}

loadSessions();
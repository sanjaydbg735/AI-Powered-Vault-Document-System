"use strict";

const SAMPLES = ["services_agreement.md", "q3_financial_report.pdf", "employee_handbook.docx"];
const DEMO_QUESTIONS = [
  "How much notice is needed to terminate the services agreement?",
  "What was Northwind's Q3 revenue, and what is the main risk?",
  "How many weeks of parental leave do primary caregivers get?",
  "Who won the 2018 football world cup?",
];

const state = {
  key: localStorage.getItem("vault-key") || "dev-key",
  view: "library",
  docs: [],
  filter: "",
  checked: new Set(),
  detail: null,
  sessions: [],
  session: null,
  messages: [],
  streaming: false,
  streamText: "",
  streamSources: [],
  search: null,
  compare: null,
  metrics: null,
  abort: null,
};

const $ = (sel, root = document) => root.querySelector(sel);

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function fmtBytes(n) {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

function fmtMs(n) {
  if (n == null || Number.isNaN(Number(n))) return "–";
  n = Number(n);
  return n < 1000 ? `${Math.round(n)} ms` : `${(n / 1000).toFixed(2)} s`;
}

function toast(message, kind = "bad") {
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.textContent = message;
  $("#toasts").appendChild(el);
  setTimeout(() => el.remove(), 4200);
}

async function api(path, { method = "GET", json, body } = {}) {
  const headers = { "X-API-Key": state.key };
  let payload = body;
  if (json !== undefined) {
    headers["Content-Type"] = "application/json";
    payload = JSON.stringify(json);
  }
  const res = await fetch("/api/v1" + path, { method, headers, body: payload });
  const text = await res.text();
  const data = text ? JSON.parse(text) : null;
  if (!res.ok) {
    const error = new Error(data?.error?.message || res.statusText);
    error.code = data?.error?.code;
    error.status = res.status;
    if (res.status === 401) $("#api-key")?.focus();
    throw error;
  }
  return data;
}

function setNav() {
  const current = state.view === "doc" || state.view === "search" || state.view === "compare" ? "library" : state.view;
  document.querySelectorAll(".nav").forEach((b) => b.classList.toggle("on", b.dataset.view === current));
}

function pill(text) {
  const kind = String(text || "").toLowerCase();
  return `<span class="pill ${esc(kind)}">${esc(text)}</span>`;
}

function docName(id) {
  return state.docs.find((d) => d.id === id)?.filename || String(id).slice(0, 8);
}

async function refreshHealth() {
  const el = $("#health");
  try {
    const res = await fetch("/api/v1/health/ready");
    const data = await res.json();
    el.className = "health " + (data.status === "ok" ? "ok" : "bad");
    const mode = data.llm_provider === "fake" ? "Offline mode" : data.llm_provider;
    el.textContent = `${data.status === "ok" ? "Ready" : "Degraded"} · ${mode}`;
    state.provider = data.llm_provider;
  } catch {
    el.className = "health bad";
    el.textContent = "API unreachable";
  }
}

async function loadDocs() {
  const page = await api("/documents?limit=100");
  state.docs = page.items;
  for (const id of [...state.checked]) {
    if (!state.docs.some((d) => d.id === id)) state.checked.delete(id);
  }
}

function filteredDocs() {
  const q = state.filter.trim().toLowerCase();
  if (!q) return state.docs;
  return state.docs.filter((d) =>
    [d.filename, d.title, d.category, ...(d.tags || [])].join(" ").toLowerCase().includes(q)
  );
}

function renderDocList() {
  const docs = filteredDocs();
  $("#doc-list").innerHTML = docs.length
    ? docs.map((d) => `
      <article class="doc ${state.detail?.doc?.id === d.id ? "on" : ""}">
        <input type="checkbox" data-action="check" data-id="${d.id}" ${state.checked.has(d.id) ? "checked" : ""} aria-label="Select ${esc(d.filename)}" />
        <button type="button" class="doc-hit" data-action="open-doc" data-id="${d.id}">
          <strong>${esc(d.title || d.filename)}</strong>
          <small>${esc(d.filename)} · ${esc(d.file_type)} · ${fmtBytes(d.size_bytes)}</small>
          <span class="pills" data-doc-status="${d.id}">
            ${pill(d.status)}${pill(d.analysis_status)}${d.category ? pill(d.category) : ""}
          </span>
        </button>
      </article>`).join("")
    : `<p class="empty">${state.docs.length ? "No documents match that filter." : "No documents yet. Drop files here or load the sample set."}</p>`;
  renderSelection();
}

function renderSelection() {
  const bar = $("#selection-bar");
  const n = state.checked.size;
  bar.hidden = n === 0;
  if (!n) return;
  bar.innerHTML = `
    <span>${n} selected</span>
    <button type="button" data-action="chat-selected">Chat</button>
    <button type="button" data-action="compare" ${n < 2 ? "disabled" : ""}>Compare</button>`;
}

function statusLine(doc) {
  const bits = [
    doc.file_type.toUpperCase(),
    fmtBytes(doc.size_bytes),
    doc.page_count ? `${doc.page_count} pages` : null,
    `${doc.chunk_count} chunks`,
    doc.token_count ? `${doc.token_count} tokens` : null,
    `v${doc.version}`,
  ].filter(Boolean);
  return bits.join(" · ");
}

function renderMain() {
  setNav();
  const main = $("#main");
  if (state.view === "chat") main.innerHTML = chatHtml();
  else if (state.view === "metrics") main.innerHTML = metricsHtml();
  else if (state.view === "doc") main.innerHTML = docHtml();
  else if (state.view === "compare") main.innerHTML = compareHtml();
  else main.innerHTML = libraryHtml();
  if (state.view === "chat") scrollThread();
}

function waitingCount() {
  return state.docs.filter((d) => d.status === "queued" || d.status === "processing").length;
}

function libraryHtml() {
  const hits = state.search?.hits || [];
  return `
    <section class="hero">
      <h1>Ask your documents.</h1>
      <p class="lede">Upload a file, wait until it is <b>ready</b>, then ask a question. Answers quote the passage they came from, and refuse when the documents do not contain it.</p>
      ${state.provider === "fake" ? `<p class="banner">Offline mode is on, so no API key is used. Answers are pulled from the documents and still show citations. Switch <b>LLM_PROVIDER</b> to <b>openai</b> when you add a key.</p>` : ""}
      ${waitingCount() ? `<p class="muted">${waitingCount()} document${waitingCount() === 1 ? "" : "s"} still processing. Chat works as soon as the status turns ready.</p>` : ""}
      <form class="searchbar" data-form="search">
        <input name="query" value="${esc(state.search?.query || "")}" placeholder="Search inside your documents" aria-label="Search" />
        <button class="primary" type="submit">Search</button>
      </form>
      ${state.search ? `<p class="muted">${hits.length} hits · ${state.search.took_ms} ms <button type="button" class="ghost" data-action="clear-search">Clear</button></p>
        <div class="hits">${hits.map(hitHtml).join("") || `<p class="empty">Nothing matched.</p>`}</div>` : `
      <div class="questions">
        ${DEMO_QUESTIONS.map((q) => `<button type="button" class="chip" data-action="ask" data-question="${esc(q)}">${esc(q)}</button>`).join("")}
      </div>
      <p class="muted">The last question is not in the samples, so Vault should say it cannot find an answer.</p>`}
    </section>`;
}

function hitHtml(h) {
  return `
    <article class="hit">
      <header>
        <strong>${esc(h.filename)}${h.pages ? ` · p.${esc(h.pages)}` : ""}</strong>
        <span class="ranks">score ${h.score} · vector #${h.vector_rank ?? "–"} · keyword #${h.keyword_rank ?? "–"}</span>
      </header>
      <p>${esc(h.content.slice(0, 420))}</p>
    </article>`;
}

function docHtml() {
  const d = state.detail;
  if (!d || d.loading) return `<p class="muted">Loading document…</p>`;
  if (d.error) return `<p class="empty">${esc(d.error)}</p>`;
  const doc = d.doc;
  const ins = d.insights || {};
  const entities = (ins.entities || []).slice(0, 12);
  return `
    <article class="doc-view">
      <button type="button" class="ghost" data-action="nav" data-view="library">← Library</button>
      <h1>${esc(ins.title || doc.title || doc.filename)}</h1>
      <p class="muted">${esc(doc.filename)} · ${esc(statusLine(doc))}</p>
      <div class="toolbar">
        <span class="pills">${pill(doc.status)}${pill(doc.analysis_status)}${doc.category ? pill(doc.category) : ""}${(doc.tags || []).map(pill).join("")}</span>
        <span class="spacer"></span>
        <button type="button" class="ghost" data-action="reprocess" data-id="${doc.id}">Reprocess</button>
        <button type="button" class="danger" data-action="delete-doc" data-id="${doc.id}">Delete</button>
      </div>
      ${doc.error_message ? `<p class="toast bad" style="position:static">${esc(doc.error_code)}: ${esc(doc.error_message)}</p>` : ""}
      <div class="split">
        <section class="card">
          <h2>Summary</h2>
          <p class="prose">${esc(ins.summary || "Insights appear when analysis completes. Chat works as soon as status is ready.")}</p>
          ${ins.key_points?.length ? `<h2>Key points</h2><ul>${ins.key_points.map((p) => `<li>${esc(p)}</li>`).join("")}</ul>` : ""}
          ${ins.suggested_questions?.length ? `<h2>Try asking</h2><div class="questions">${ins.suggested_questions.map((q) => `<button type="button" class="chip" data-action="ask-doc" data-id="${doc.id}" data-question="${esc(q)}">${esc(q)}</button>`).join("")}</div>` : ""}
        </section>
        <div>
          <section class="card">
            <h2>What the model extracted</h2>
            <p>${ins.sentiment ? `Sentiment <b>${esc(ins.sentiment.label)}</b> (${ins.sentiment.score})` : "No sentiment yet."}</p>
            ${entities.length ? `<p>${entities.map((e) => `<span class="pill">${esc(e.name)} · ${esc(e.type)}</span>`).join(" ")}</p>` : ""}
            <p class="muted">${esc(ins.language || "")} · ${state.provider === "fake" ? "offline" : esc(ins.model || "")} ${ins.prompt_version ? `· prompt ${esc(ins.prompt_version)}` : ""}</p>
          </section>
          <section class="card" style="margin-top:12px">
            <h2>Custom summary</h2>
            <form class="summary-form" data-form="summary">
              <label class="field">Length<select name="length"><option>short</option><option selected>medium</option><option>long</option></select></label>
              <label class="field">Tone<select name="tone"><option>neutral</option><option>executive</option><option>technical</option><option>simple</option></select></label>
              <label class="field grow">Focus<input name="focus" placeholder="e.g. payment terms" /></label>
              <label class="check"><input type="checkbox" name="bullet_points" /> Bullet points</label>
              <button class="primary" type="submit" ${doc.status !== "ready" ? "disabled" : ""}>Generate</button>
            </form>
            <div id="summary-result">${d.summary ? summaryHtml(d.summary) : ""}</div>
          </section>
        </div>
      </div>
      <section class="card" style="margin-top:12px">
        <h2>Indexed passages</h2>
        <button type="button" class="ghost" data-action="chunks" data-id="${doc.id}">${d.chunks ? "Refresh" : "Show"} what was embedded</button>
        <div id="chunks">${(d.chunks || []).map(chunkHtml).join("")}</div>
      </section>
    </article>`;
}

function summaryHtml(s) {
  return `<p class="prose">${esc(s.summary)}</p><p class="muted">${s.cached ? "Served from cache" : "Fresh"} · ${esc(s.model)}</p>`;
}

function chunkHtml(c) {
  const pages = c.page_start ? `pages ${c.page_start}${c.page_end !== c.page_start ? "–" + c.page_end : ""}` : "no pages";
  return `<div class="chunk"><small>#${c.chunk_index} · ${pages} · ${c.token_count} tokens</small><p class="prose">${esc(c.content)}</p></div>`;
}

function scopeLabel(session) {
  if (!session?.document_ids?.length) return "Searching all ready documents";
  return "Scoped to " + session.document_ids.map(docName).join(", ");
}

function chatHtml() {
  const session = state.session;
  return `
    <div class="chat">
      <div class="sessions">
        <button type="button" class="primary" data-action="new-chat">New chat</button>
        ${state.sessions.map((s) => `
          <button type="button" class="session ${s.id === session?.id ? "on" : ""}" data-action="open-session" data-id="${s.id}">
            ${esc(s.title || "New chat")}
            <small>${s.message_count} messages · ${s.document_ids.length ? s.document_ids.length + " docs" : "all docs"}</small>
          </button>`).join("") || `<p class="empty">No conversations yet.</p>`}
      </div>
      <div class="chat-main">
        <div class="scope">${session ? esc(scopeLabel(session)) : "Start a chat to ask across your documents."}
          ${session ? `<button type="button" class="ghost" data-action="delete-session" data-id="${session.id}">Delete chat</button>` : ""}
        </div>
        <div id="thread">
          ${(state.messages || []).map(messageHtml).join("")}
          ${state.streaming ? streamHtml() : ""}
          ${session && !state.messages.length && !state.streaming ? `<p class="empty">Ask a question. Follow-ups like “and the interest?” are rewritten into a standalone search.</p>` : ""}
        </div>
        <form class="composer" data-form="ask">
          <textarea name="question" rows="2" placeholder="${session ? "Ask about these documents…" : "Ask about all of your documents…"}" ${state.streaming ? "disabled" : ""} required></textarea>
          <button class="primary" type="submit" ${state.streaming ? "disabled" : ""}>Ask</button>
        </form>
      </div>
    </div>`;
}

function messageHtml(m, index) {
  if (m.role === "user") {
    return `<article class="msg user"><div class="who">You</div><div class="answer">${esc(m.content)}</div></article>`;
  }
  const cites = m.citations || [];
  return `
    <article class="msg assistant" data-idx="${index}">
      <div class="who">Vault ${m.latency_ms != null ? `· ${fmtMs(m.latency_ms)}` : ""} ${cites.length ? `<span class="tag yes">cited</span>` : `<span class="tag no">not in documents</span>`}</div>
      ${m.standalone_query ? `<p class="rewritten">Searched for “${esc(m.standalone_query)}”</p>` : ""}
      <div class="answer">${formatAnswer(m.content)}</div>
      ${cites.length ? `<div class="cites">${cites.map(citeCard).join("")}</div>` : ""}
      ${m.suggested_questions?.length ? `<div class="follow">${m.suggested_questions.map((q) => `<button type="button" class="chip" data-action="ask" data-question="${esc(q)}">${esc(q)}</button>`).join("")}</div>` : ""}
    </article>`;
}

function streamHtml() {
  const sources = state.streamSources.map((s) => `<span class="pill">${esc(s.source_id)} · ${esc(s.filename)}${s.pages ? " p." + esc(s.pages) : ""}</span>`).join(" ");
  return `
    <article class="msg assistant">
      <div class="who">Vault</div>
      <div class="pills" id="stream-sources">${sources}</div>
      <div class="answer" id="stream-body">${formatAnswer(state.streamText) || "…"}</div>
    </article>`;
}

function citeCard(c) {
  return `<div class="cite-card"><strong>${esc(c.source_id)}</strong> · ${esc(c.filename)}${c.pages ? ` · p.${esc(c.pages)}` : ""}<div>${esc(c.snippet || "")}</div></div>`;
}

function formatAnswer(text) {
  return esc(text).replace(/\[(S\d+(?:\s*[,;]\s*S\d+)*)\]/g, (_, group) =>
    group.split(/\s*[,;]\s*/).map((id) => `<button type="button" class="cite" data-action="cite" data-src="${esc(id)}">${esc(id)}</button>`).join("")
  );
}

function compareHtml() {
  const c = state.compare;
  if (!c || c.loading) return `<p class="muted">Comparing documents…</p>`;
  if (c.error) return `<p class="empty">${esc(c.error)}</p><button class="ghost" data-action="nav" data-view="library">Back</button>`;
  const r = c.result;
  const name = (id) => r.documents.find((d) => d.id === id)?.filename || id;
  const diffs = (r.differences || []).map((d) => {
    const cells = Object.entries(d.by_document || {}).map(([id, text]) => `<li><b>${esc(name(id))}:</b> ${esc(text)}</li>`).join("");
    return `<h3>${esc(d.aspect || "Difference")}</h3><ul>${cells}</ul>`;
  }).join("");
  const unique = Object.entries(r.unique_points || {}).map(([id, points]) =>
    `<h3>${esc(name(id))}</h3><ul>${(points || []).map((p) => `<li>${esc(p)}</li>`).join("")}</ul>`
  ).join("");
  return `
    <button type="button" class="ghost" data-action="nav" data-view="library">← Library</button>
    <h1 class="hero" style="font-family:var(--serif)">Comparison</h1>
    <p class="muted">${r.documents.map((d) => esc(d.filename)).join(" · ")}${r.cached ? " · cached" : ""}</p>
    <section class="card"><h2>Overview</h2><p>${esc(r.overview || "")}</p>
      ${r.similarities?.length ? `<h2>Similarities</h2><ul>${r.similarities.map((s) => `<li>${esc(s)}</li>`).join("")}</ul>` : ""}
      ${r.conclusion ? `<p><b>Conclusion.</b> ${esc(r.conclusion)}</p>` : ""}
    </section>
    ${diffs ? `<section class="card" style="margin-top:12px"><h2>Differences</h2>${diffs}</section>` : ""}
    ${unique ? `<section class="card" style="margin-top:12px"><h2>Unique to each document</h2>${unique}</section>` : ""}
    <form class="compare-form" data-form="compare" style="margin-top:12px">
      <label class="field grow">Run again with a focus<input name="focus" placeholder="e.g. termination and liability" /></label>
      <button class="primary" type="submit">Compare again</button>
    </form>`;
}

function metricsHtml() {
  const m = state.metrics;
  if (!m) return `<p class="muted">Loading metrics…</p>`;
  if (m.error) return `<p class="empty">${esc(m.error)}</p>`;
  const docs = m.documents;
  const proc = m.processing;
  const usage = m.usage;
  const stages = Object.entries(proc.stages || {});
  const max = Math.max(...stages.map(([, s]) => s.p95_ms || 0), 1);
  return `
    <section class="metrics">
      <div class="metric-grid">
        <div class="metric"><b>${docs.total_documents}</b><span>documents</span></div>
        <div class="metric"><b>${docs.total_pages}</b><span>pages</span></div>
        <div class="metric"><b>${docs.total_chunks}</b><span>chunks indexed</span></div>
        <div class="metric"><b>${usage.chat.grounded_rate == null ? "–" : Math.round(usage.chat.grounded_rate * 100) + "%"}</b><span>answers with citations</span></div>
      </div>
      <section class="card">
        <h2>Pipeline · last ${proc.window_hours}h</h2>
        <p class="muted">Upload → ready ${fmtMs(proc.end_to_end_ms.upload_to_ready_p50)} p50 · ${fmtMs(proc.end_to_end_ms.upload_to_ready_p95)} p95
          · queue ingest ${proc.queue_depth?.ingest ?? "–"} / analysis ${proc.queue_depth?.analysis ?? "–"}</p>
        ${stages.map(([name, s]) => `
          <div class="bar-row"><span>${esc(name)}</span><span class="bar"><i style="width:${Math.max(4, (s.p95_ms || 0) / max * 100)}%"></i></span>
          <span>p50 ${fmtMs(s.p50_ms)} · p95 ${fmtMs(s.p95_ms)} · ${Math.round((s.success_rate || 0) * 100)}% ok</span></div>`).join("")}
        ${proc.recent_failures?.length ? `<h2>Recent failures</h2><ul>${proc.recent_failures.map((f) => `<li>${esc(f.filename)} · ${esc(f.error_code)}</li>`).join("")}</ul>` : ""}
      </section>
      <section class="card">
        <h2>AI usage · last ${Math.round(usage.window_hours / 24)} days</h2>
        <p class="muted">${usage.total_calls} calls · ${usage.total_tokens} tokens · ${Number(usage.total_cost_usd).toFixed(4)} USD
          ${state.provider === "fake" ? "· offline provider, so cost stays at zero" : ""}</p>
        <table>
          <tr><th>Operation</th><th>Model</th><th>Calls</th><th>Cache hits</th><th>Tokens</th><th>Avg latency</th></tr>
          ${(usage.breakdown || []).map((b) => `<tr><td>${esc(b.operation)}</td><td>${esc(b.model)}</td><td>${b.calls}</td><td>${b.cache_hits}</td><td>${b.prompt_tokens + b.completion_tokens}</td><td>${fmtMs(b.avg_latency_ms)}</td></tr>`).join("")}
        </table>
        <p class="muted">Chat latency p50 ${fmtMs(usage.chat.latency_p50_ms)} · p95 ${fmtMs(usage.chat.latency_p95_ms)}</p>
      </section>
    </section>`;
}

function scrollThread() {
  const thread = $("#thread");
  if (thread) thread.scrollTop = thread.scrollHeight;
}

async function uploadOne(file) {
  const body = new FormData();
  body.append("file", file, file.name);
  const result = await api("/documents", { method: "POST", body });
  toast(result.duplicate ? `${file.name} is already in the vault` : `Uploaded ${file.name}`, "ok");
}

async function uploadFiles(files) {
  for (const file of files) {
    try {
      await uploadOne(file);
    } catch (err) {
      toast(`${file.name}: ${err.message}`);
    }
  }
  await loadDocs();
  renderDocList();
  if (state.view === "library") renderMain();
}

async function loadSamples(button) {
  button.disabled = true;
  try {
    for (const name of SAMPLES) {
      const res = await fetch("/static/samples/" + name);
      if (!res.ok) throw new Error("Sample file missing: " + name);
      const blob = await res.blob();
      await uploadOne(new File([blob], name, { type: blob.type }));
    }
    await loadDocs();
    renderDocList();
  } catch (err) {
    toast(err.message);
  } finally {
    button.disabled = false;
  }
}

async function openDoc(id) {
  state.view = "doc";
  state.detail = { loading: true };
  renderMain();
  try {
    const [doc, insights] = await Promise.all([
      api("/documents/" + id),
      api("/documents/" + id + "/insights"),
    ]);
    state.detail = { doc, insights, chunks: null, summary: null };
  } catch (err) {
    state.detail = { error: err.message };
  }
  renderMain();
  renderDocList();
}

async function createSession(documentIds) {
  state.session = await api("/chat/sessions", { method: "POST", json: { document_ids: documentIds } });
  state.messages = [];
  state.sessions = [state.session, ...state.sessions.filter((s) => s.id !== state.session.id)];
}

async function openSession(id) {
  state.session = state.sessions.find((s) => s.id === id) || await api("/chat/sessions/" + id);
  const page = await api(`/chat/sessions/${id}/messages?limit=200`);
  state.messages = page.items;
  state.view = "chat";
  renderMain();
}

async function ensureSession(documentIds = null) {
  if (state.session && documentIds === null) return;
  await createSession(documentIds || []);
}

async function ask(question, documentIds = null) {
  const q = question.trim();
  if (!q) return;
  state.view = "chat";
  try {
    if (documentIds) await createSession(documentIds);
    else await ensureSession(null);
    renderMain();
    await send(q);
  } catch (err) {
    toast(err.message);
  }
}

function parseSSE(buffer) {
  const events = [];
  let rest = buffer.replace(/\r\n/g, "\n");
  let idx;
  while ((idx = rest.indexOf("\n\n")) >= 0) {
    const block = rest.slice(0, idx);
    rest = rest.slice(idx + 2);
    const name = (block.match(/^event: ?(.*)$/m) || [])[1] || "message";
    const data = block.split("\n").filter((line) => line.startsWith("data:")).map((line) => line.slice(5).trim()).join("\n");
    if (data) events.push({ event: name, data: JSON.parse(data) });
  }
  return { events, rest };
}

async function send(question) {
  if (state.abort) state.abort.abort();
  const controller = new AbortController();
  state.abort = controller;
  state.streaming = true;
  state.streamText = "";
  state.streamSources = [];
  state.messages = state.messages.concat([{ role: "user", content: question, citations: [], suggested_questions: [] }]);
  renderMain();

  let res;
  try {
    res = await fetch(`/api/v1/chat/sessions/${state.session.id}/messages`, {
      method: "POST",
      headers: { "X-API-Key": state.key, "Content-Type": "application/json" },
      body: JSON.stringify({ question, stream: true }),
      signal: controller.signal,
    });
  } catch (err) {
    state.streaming = false;
    if (err.name !== "AbortError") toast(err.message);
    renderMain();
    return;
  }
  if (!res.ok) {
    state.streaming = false;
    const data = await res.json().catch(() => null);
    toast(data?.error?.message || "Chat failed");
    renderMain();
    return;
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const parsed = parseSSE(buffer);
      buffer = parsed.rest;
      for (const item of parsed.events) {
        if (item.event === "meta") state.streamSources = item.data.sources || [];
        if (item.event === "token") state.streamText += item.data.delta || "";
        if (item.event === "error") toast(item.data.message || "Stream failed");
        const body = $("#stream-body");
        if (body && item.event === "token") body.innerHTML = formatAnswer(state.streamText);
        if (item.event === "meta") {
          const host = $("#stream-sources");
          if (host) host.innerHTML = state.streamSources.map((s) => `<span class="pill">${esc(s.source_id)} · ${esc(s.filename)}</span>`).join(" ");
        }
        scrollThread();
      }
    }
  } catch (err) {
    if (err.name !== "AbortError") toast(err.message);
  } finally {
    state.streaming = false;
    try {
      const page = await api(`/chat/sessions/${state.session.id}/messages?limit=200`);
      state.messages = page.items;
      const detail = await api("/chat/sessions/" + state.session.id);
      state.session = detail;
      state.sessions = state.sessions.map((s) => (s.id === detail.id ? { ...s, title: detail.title, message_count: detail.message_count } : s));
    } catch (err) {
      toast(err.message);
    }
    renderMain();
  }
}

async function runSearch(query) {
  state.search = { query, hits: [], took_ms: 0, loading: true };
  state.view = "library";
  renderMain();
  try {
    const data = await api("/search", { method: "POST", json: { query, top_k: 8 } });
    state.search = data;
  } catch (err) {
    state.search = null;
    toast(err.message);
  }
  renderMain();
}

async function runCompare(focus) {
  const ids = [...state.checked];
  if (ids.length < 2 || ids.length > 5) {
    toast("Select 2 to 5 documents to compare");
    return;
  }
  state.view = "compare";
  state.compare = { loading: true };
  renderMain();
  try {
    const json = { document_ids: ids };
    if (focus) json.focus = focus;
    state.compare = { result: await api("/documents/compare", { method: "POST", json }) };
  } catch (err) {
    state.compare = { error: err.message };
  }
  renderMain();
}

async function runSummary(form) {
  const out = $("#summary-result");
  out.textContent = "Writing summary…";
  try {
    const summary = await api(`/documents/${state.detail.doc.id}/summaries`, {
      method: "POST",
      json: {
        length: form.length.value,
        tone: form.tone.value,
        focus: form.focus.value || null,
        bullet_points: form.bullet_points.checked,
      },
    });
    state.detail.summary = summary;
    out.innerHTML = summaryHtml(summary);
  } catch (err) {
    out.textContent = err.message;
  }
}

function showCitation(button) {
  const idx = button.closest(".msg")?.dataset.idx;
  const msg = idx != null ? state.messages[Number(idx)] : null;
  const cite = (msg?.citations || []).find((c) => c.source_id === button.dataset.src)
    || state.streamSources.find((s) => s.source_id === button.dataset.src);
  const pop = $("#popover");
  if (!cite) {
    pop.hidden = true;
    return;
  }
  pop.innerHTML = `<b>${esc(cite.source_id)} · ${esc(cite.filename || "")}${cite.pages ? " · p." + esc(cite.pages) : ""}</b>${esc(cite.snippet || "Source used for this answer.")}`;
  const rect = button.getBoundingClientRect();
  pop.hidden = false;
  pop.style.left = Math.min(rect.left, window.innerWidth - 440) + "px";
  pop.style.top = rect.bottom + 8 + "px";
}

function bind() {
  $("#api-key").value = state.key;
  $("#api-key").addEventListener("change", async (event) => {
    state.key = event.target.value.trim() || "dev-key";
    localStorage.setItem("vault-key", state.key);
    try {
      await loadDocs();
      renderDocList();
      renderMain();
      toast("API key updated", "ok");
    } catch (err) {
      toast(err.message);
    }
  });
  $("#doc-filter").addEventListener("input", (event) => {
    state.filter = event.target.value;
    renderDocList();
  });
  $("#file").addEventListener("change", (event) => {
    uploadFiles([...event.target.files]);
    event.target.value = "";
  });
  const drop = $("#drop");
  ["dragenter", "dragover"].forEach((name) => drop.addEventListener(name, (event) => {
    event.preventDefault();
    drop.classList.add("over");
  }));
  ["dragleave", "drop"].forEach((name) => drop.addEventListener(name, (event) => {
    event.preventDefault();
    drop.classList.remove("over");
  }));
  drop.addEventListener("drop", (event) => uploadFiles([...event.dataTransfer.files]));

  document.body.addEventListener("click", async (event) => {
    if (!event.target.closest("#popover") && !event.target.closest(".cite")) $("#popover").hidden = true;
    const el = event.target.closest("[data-action]");
    if (!el) return;
    const { action, id, view, question } = el.dataset;
    try {
      if (action === "nav") {
        state.view = view;
        if (view === "chat" && !state.sessions.length) state.sessions = await api("/chat/sessions");
        if (view === "metrics") await loadMetrics();
        renderMain();
      } else if (action === "load-samples") await loadSamples(el);
      else if (action === "open-doc") await openDoc(id);
      else if (action === "chat-selected") {
        await createSession([...state.checked]);
        state.view = "chat";
        renderMain();
        document.querySelector(".composer textarea")?.focus();
      } else if (action === "clear-search") {
        state.search = null;
        renderMain();
      }
      else if (action === "new-chat") {
        await createSession([]);
        state.view = "chat";
        renderMain();
      } else if (action === "open-session") await openSession(id);
      else if (action === "delete-session") {
        await api("/chat/sessions/" + id, { method: "DELETE" });
        state.sessions = state.sessions.filter((s) => s.id !== id);
        if (state.session?.id === id) {
          state.session = null;
          state.messages = [];
        }
        renderMain();
      } else if (action === "delete-doc") {
        if (!confirm("Delete this document and its index?")) return;
        await api("/documents/" + id, { method: "DELETE" });
        state.checked.delete(id);
        state.view = "library";
        state.detail = null;
        await loadDocs();
        renderDocList();
        renderMain();
      } else if (action === "reprocess") {
        await api(`/documents/${id}/reprocess`, { method: "POST" });
        toast("Reprocessing started", "ok");
        await loadDocs();
        renderDocList();
        if (state.detail?.doc?.id === id) await openDoc(id);
      } else if (action === "chunks") {
        state.detail.chunks = await api(`/documents/${id}/chunks?limit=200`);
        $("#chunks").innerHTML = state.detail.chunks.map(chunkHtml).join("") || `<p class="empty">No chunks yet.</p>`;
      } else if (action === "ask") await ask(question);
      else if (action === "ask-doc") await ask(question, [id]);
      else if (action === "compare") await runCompare("");
      else if (action === "cite") showCitation(el);
    } catch (err) {
      toast(err.message);
    }
  });

  document.body.addEventListener("change", (event) => {
    const box = event.target.closest('[data-action="check"]');
    if (!box) return;
    if (box.checked) state.checked.add(box.dataset.id);
    else state.checked.delete(box.dataset.id);
    renderSelection();
  });

  document.body.addEventListener("submit", async (event) => {
    const form = event.target.closest("form");
    if (!form) return;
    event.preventDefault();
    try {
      if (form.dataset.form === "ask") {
        const question = form.question.value;
        form.question.value = "";
        await ask(question);
      } else if (form.dataset.form === "search") await runSearch(form.query.value.trim());
      else if (form.dataset.form === "summary") await runSummary(form);
      else if (form.dataset.form === "compare") await runCompare(form.focus.value.trim());
    } catch (err) {
      toast(err.message);
    }
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") $("#popover").hidden = true;
    if (event.key === "Enter" && !event.shiftKey && event.target.matches(".composer textarea")) {
      event.preventDefault();
      event.target.form?.requestSubmit();
    }
  });
}

async function loadMetrics() {
  state.metrics = null;
  renderMain();
  try {
    const [documents, processing, usage] = await Promise.all([
      api("/metrics/documents"),
      api("/metrics/processing"),
      api("/metrics/usage"),
    ]);
    state.metrics = { documents, processing, usage };
  } catch (err) {
    state.metrics = { error: err.message };
  }
  if (state.view === "metrics") renderMain();
}

function watchProcessing() {
  setInterval(async () => {
    const pending = state.docs.some((d) =>
      d.status === "queued" || d.status === "processing" || (d.status === "ready" && ["pending", "processing"].includes(d.analysis_status))
    );
    if (!pending) return;
    const before = new Map(state.docs.map((d) => [d.id, `${d.status}:${d.analysis_status}`]));
    try {
      await loadDocs();
    } catch {
      return;
    }
    for (const doc of state.docs) {
      const prev = before.get(doc.id);
      if (prev && prev !== "ready:completed" && doc.status === "ready" && doc.analysis_status === "completed") {
        toast(`${doc.filename} is ready to chat`, "ok");
      }
    }
    renderDocList();
    if (state.view === "doc" && state.detail?.doc && !document.activeElement?.closest("form")) {
      const fresh = state.docs.find((d) => d.id === state.detail.doc.id);
      const prev = before.get(state.detail.doc.id);
      if (fresh && prev && prev !== `${fresh.status}:${fresh.analysis_status}`) openDoc(fresh.id);
    }
  }, 2000);
}

async function boot() {
  bind();
  await refreshHealth();
  try {
    await loadDocs();
    state.sessions = await api("/chat/sessions");
  } catch (err) {
    toast(err.message);
  }
  renderDocList();
  renderMain();
  watchProcessing();
}

boot();

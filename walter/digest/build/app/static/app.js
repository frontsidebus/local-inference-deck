/* Walter digest UI - vanilla JS, no dependencies, no external CDNs. */
(() => {
  "use strict";

  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));

  const ACCENT = { default: "red", "ai-security": "magenta", "ai-research": "cyan" };
  const STAGE_ORDER = ["collecting", "curating", "done"];

  const state = {
    watches: [],      // [{slug,name,running,latest}]
    es: null,         // active EventSource for the run view
    current: null,    // {watch, runId}
    raw: "",          // raw markdown of the loaded run (for the raw toggle)
    failed: false,    // the open run ended in an error
    poll: null,       // history poll timer
  };

  /* ---------------- tiny safe markdown renderer (no deps) ---------------- */
  const esc = (s) =>
    s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");

  const safeUrl = (u) => {
    const t = u.trim();
    return /^(https?:\/\/|\/)/i.test(t) ? t : "#";
  };

  function inline(s) {
    s = esc(s);
    s = s.replace(/`([^`]+)`/g, "<code>$1</code>");
    s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    s = s.replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>");
    s = s.replace(
      /\[([^\]]+)\]\(([^)\s]+)\)/g,
      (_m, txt, url) => `<a href="${safeUrl(url)}" rel="noopener noreferrer" target="_blank">${txt}</a>`
    );
    return s;
  }

  function renderMarkdown(md) {
    const lines = String(md || "").replace(/\r\n/g, "\n").split("\n");
    const out = [];
    let list = null; // "ul" | "ol"
    let inCode = false;
    let code = [];
    const closeList = () => {
      if (list) {
        out.push(`</${list}>`);
        list = null;
      }
    };
    for (const raw of lines) {
      if (raw.trim().startsWith("```")) {
        if (inCode) {
          out.push("<pre><code>" + esc(code.join("\n")) + "</code></pre>");
          code = [];
          inCode = false;
        } else {
          closeList();
          inCode = true;
        }
        continue;
      }
      if (inCode) {
        code.push(raw);
        continue;
      }
      const t = raw.trim();
      if (!t) {
        closeList();
        continue;
      }
      const h = t.match(/^(#{1,4})\s+(.*)$/);
      if (h) {
        closeList();
        out.push(`<h${h[1].length}>${inline(h[2])}</h${h[1].length}>`);
        continue;
      }
      if (/^(-{3,}|\*{3,})$/.test(t)) {
        closeList();
        out.push("<hr>");
        continue;
      }
      if (t.startsWith("> ")) {
        closeList();
        out.push(`<blockquote>${inline(t.slice(2))}</blockquote>`);
        continue;
      }
      const li = t.match(/^[-*]\s+(.*)$/);
      if (li) {
        if (list !== "ul") {
          closeList();
          out.push("<ul>");
          list = "ul";
        }
        out.push(`<li>${inline(li[1])}</li>`);
        continue;
      }
      const oli = t.match(/^\d+[.)]\s+(.*)$/);
      if (oli) {
        if (list !== "ol") {
          closeList();
          out.push("<ol>");
          list = "ol";
        }
        out.push(`<li>${inline(oli[1])}</li>`);
        continue;
      }
      closeList();
      out.push(`<p>${inline(t)}</p>`);
    }
    if (inCode) out.push("<pre><code>" + esc(code.join("\n")) + "</code></pre>");
    closeList();
    return out.join("\n");
  }

  /* ---------------- helpers ---------------- */
  const fmtTs = (iso) => {
    if (!iso) return "—";
    const d = new Date(iso);
    return isNaN(d) ? esc(iso) : d.toISOString().replace("T", " ").slice(0, 19) + "Z";
  };

  function setPill(cls, text) {
    const p = $("#status-pill");
    p.className = "pill " + cls;
    p.querySelector(".pill-text").textContent = text;
  }

  /* ---------------- landing: watch cards ---------------- */
  function renderCards() {
    const box = $("#cards");
    box.innerHTML = "";
    for (const w of state.watches) {
      const card = document.createElement("article");
      card.className = "card";
      card.dataset.accent = ACCENT[w.slug] || "cyan";
      const meta = w.latest
        ? `last run <b>${fmtTs(w.latest.generated_at)}</b> · ${w.latest.items ?? 0} items`
        : "no runs yet";
      card.innerHTML = `
        <div class="card-name">${esc(w.name)}</div>
        <div class="card-slug mono">${esc(w.slug)}</div>
        <div class="card-meta">${meta}</div>
        <div class="card-foot">
          <span class="card-state ${w.running ? "running" : "idle"}">${w.running ? "RUNNING" : "IDLE"}</span>
          <button class="btn" type="button" data-run="${esc(w.slug)}" ${w.running ? "disabled" : ""}>RUN NOW</button>
        </div>`;
      box.append(card);
    }
    $$("#cards [data-run]").forEach((b) =>
      b.addEventListener("click", () => startRun(b.dataset.run))
    );
  }

  /* ---------------- landing: history table ---------------- */
  async function loadHistory() {
    const rows = [];
    for (const w of state.watches) {
      try {
        const r = await fetch(`/api/runs/${encodeURIComponent(w.slug)}`, { cache: "no-store", credentials: "same-origin" });
        if (!r.ok) continue;
        const d = await r.json();
        for (const run of d.runs || []) rows.push({ ...run, watch: w.slug, name: w.name });
      } catch {
        /* server unreachable: keep old rows */
      }
    }
    rows.sort((a, b) => String(b.run_id).localeCompare(String(a.run_id)));
    const tbody = $("#hist-tbl tbody");
    tbody.innerHTML = "";
    if (!rows.length) {
      tbody.innerHTML = `<tr><td colspan="5" class="empty">no runs yet — hit RUN NOW on a watch</td></tr>`;
      return;
    }
    for (const r of rows) {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td><span class="watch-chip ${esc(r.watch)}">${esc(r.name)}</span></td>
        <td>${esc(r.run_id)}</td>
        <td>${fmtTs(r.generated_at)}</td>
        <td class="r">${r.items ?? 0}</td>
        <td class="r"><span class="muted">open &rarr;</span></td>`;
      tr.addEventListener("click", () => openRun(r.watch, r.run_id));
      tbody.append(tr);
    }
  }

  async function refreshLanding() {
    try {
      const r = await fetch("/api/watches", { cache: "no-store", credentials: "same-origin" });
      if (!r.ok) throw new Error(String(r.status));
      state.watches = (await r.json()).watches || [];
      setPill("pill-ok", "ONLINE");
    } catch {
      setPill("pill-bad", "OFFLINE");
    }
    renderCards();
    await loadHistory();
  }

  /* ---------------- run view ---------------- */
  function closeStream() {
    if (state.es) {
      state.es.close();
      state.es = null;
    }
  }

  function setStage(name, cls) {
    const li = $(`#stages [data-stage="${name}"]`);
    if (li) li.className = "stage " + cls;
  }

  function resetStages() {
    for (const s of STAGE_ORDER) setStage(s, "stage-wait");
    $("#digest-md").hidden = true;
    $("#digest-raw").hidden = true;
    $("#raw-toggle").hidden = true;
  }

  function showDigest(markdown) {
    state.raw = String(markdown || "");
    $("#digest-md").innerHTML = renderMarkdown(markdown);
    $("#digest-md").hidden = false;
    $("#raw-toggle").hidden = false;
  }

  function openRun(watch, runId, live) {
    closeStream();
    state.current = { watch, runId };
    $("#view-landing").hidden = true;
    $("#view-run").hidden = false;
    const w = state.watches.find((x) => x.slug === watch);
    $("#run-title").textContent = w ? w.name : watch;
    $("#run-id-tag").textContent = runId;
    resetStages();
    state.failed = false;
    if (live) {
      streamRun(watch, runId);
    } else {
      for (const s of STAGE_ORDER) setStage(s, "stage-done");
      loadRun(watch, runId);
    }
  }

  async function loadRun(watch, runId) {
    try {
      const r = await fetch(`/api/runs/${encodeURIComponent(watch)}/${encodeURIComponent(runId)}`, {
        cache: "no-store",
        credentials: "same-origin",
      });
      if (!r.ok) throw new Error(String(r.status));
      const d = await r.json();
      showDigest(d.markdown);
    } catch {
      if (state.failed) return; // pipeline error already shown; don't clobber it
      $("#digest-md").hidden = true;
      $("#digest-md").innerHTML = "";
      $("#digest-raw").textContent = "failed to load run artifact";
      $("#digest-raw").hidden = false;
    }
  }

  function streamRun(watch, runId) {
    const url = `/api/runs/${encodeURIComponent(watch)}/${encodeURIComponent(runId)}/stream`;
    const es = new EventSource(url);
    state.es = es;
    const onStage = (name) => (ev) => {
      setStage(name, "stage-active");
      if (name === "done") {
        for (const s of STAGE_ORDER) setStage(s, "stage-done");
        finishRun(watch, runId);
      }
    };
    es.addEventListener("collecting", onStage("collecting"));
    es.addEventListener("curating", onStage("curating"));
    es.addEventListener("done", onStage("done"));
    es.addEventListener("error", (ev) => {
      // SSE "error" event from the pipeline (data carries the message), not a transport error
      if (ev.data) {
        try {
          const d = JSON.parse(ev.data);
          state.failed = true;
          setStage("done", "stage-error");
          $("#digest-raw").textContent = "run failed: " + (d.error || "unknown error");
          $("#digest-raw").hidden = false;
          closeStream();
          scheduleLandingRefresh();
        } catch {
          /* fall through to transport handling */
        }
      }
    });
    es.onerror = () => {
      // transport error: if the run already finished on disk, recover; otherwise retry (SSE auto)
      if (state.failed) return;
      if (state.current && state.current.runId === runId) {
        loadRun(watch, runId).then(() => {
          if (!$("#digest-md").hidden) {
            for (const s of STAGE_ORDER) setStage(s, "stage-done");
            closeStream();
          }
        });
      }
    };
  }

  async function finishRun(watch, runId) {
    closeStream();
    await loadRun(watch, runId);
    scheduleLandingRefresh();
  }

  let refreshTimer = null;
  function scheduleLandingRefresh() {
    if (refreshTimer) clearTimeout(refreshTimer);
    refreshTimer = setTimeout(refreshLanding, 1500);
  }

  async function startRun(watch) {
    const btn = $(`#cards [data-run="${watch}"]`);
    if (btn) {
      btn.disabled = true;
      btn.textContent = "STARTING…";
    }
    try {
      const r = await fetch(`/api/runs/${encodeURIComponent(watch)}/now`, {
        method: "POST",
        credentials: "same-origin",
      });
      const d = await r.json().catch(() => ({}));
      if (r.status === 409) {
        // a run is already in progress: follow it
        if (d.run_id) {
          openRun(watch, d.run_id, true);
          return;
        }
        alert("A run of this watch is already in progress.");
        scheduleLandingRefresh();
        return;
      }
      if (!r.ok || !d.run_id) {
        alert(`run failed to start: ${d.error || r.status}`);
        scheduleLandingRefresh();
        return;
      }
      openRun(watch, d.run_id, true);
    } catch {
      alert("could not reach the digest service");
      scheduleLandingRefresh();
    }
  }

  /* ---------------- boot ---------------- */
  $("#back-btn").addEventListener("click", () => {
    closeStream();
    state.current = null;
    $("#view-run").hidden = true;
    $("#view-landing").hidden = false;
    refreshLanding();
  });

  $("#raw-toggle").addEventListener("click", () => {
    const raw = $("#digest-raw");
    const md = $("#digest-md");
    if (raw.hidden) {
      raw.textContent = state.raw;
      raw.hidden = false;
      md.hidden = true;
      $("#raw-toggle").textContent = "view rendered digest";
    } else {
      raw.hidden = true;
      md.hidden = false;
      $("#raw-toggle").textContent = "view raw markdown";
    }
  });

  refreshLanding();
  state.poll = setInterval(() => {
    if (!$("#view-landing").hidden) refreshLanding();
  }, 30000);
})();

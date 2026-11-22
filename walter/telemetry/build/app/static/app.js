/* Walter telemetry dashboard - vanilla JS, no dependencies. */
(() => {
  "use strict";

  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const STALE_MS = 8000;

  const state = {
    snap: null,
    prev: null, // previous snapshot (mode transitions)
    lastTs: 0, // server snapshot ts (s)
    lastRecv: 0, // client receive time (ms)
    hist: {}, // key -> [[t, v], ...]
    window: 1800,
    es: null,
    connected: false,
  };

  /* ---------------- formatting ---------------- */
  const fmt = {
    int: (v) => (v == null ? "—" : Math.round(v).toLocaleString("en-US")),
    n1: (v) => (v == null ? "—" : (Math.abs(v) >= 100 ? Math.round(v).toLocaleString("en-US") : v.toFixed(1))),
    n2: (v) => (v == null ? "—" : v >= 100 ? Math.round(v).toString() : v >= 10 ? v.toFixed(1) : v.toFixed(2)),
    si: (v) => {
      if (v == null) return "—";
      const a = Math.abs(v);
      if (a >= 1e9) return (v / 1e9).toFixed(2) + "G";
      if (a >= 1e6) return (v / 1e6).toFixed(a >= 1e7 ? 1 : 2) + "M";
      if (a >= 1e4) return (v / 1e3).toFixed(1) + "k";
      return Math.round(v).toLocaleString("en-US");
    },
    bytes: (v) => {
      if (v == null) return "—";
      const u = ["B", "KB", "MB", "GB", "TB"];
      let i = 0;
      while (Math.abs(v) >= 1024 && i < u.length - 1) { v /= 1024; i++; }
      return (v >= 100 || i === 0 ? Math.round(v) : v.toFixed(1)) + " " + u[i];
    },
    rate: (v) => (v == null ? "—" : fmt.bytes(v) + "/s"),
    secs: (v) => (v == null ? "—" : v < 1 ? Math.round(v * 1000) + " ms" : v < 100 ? v.toFixed(2) + " s" : Math.round(v) + " s"),
    age: (s) => {
      if (s == null) return "—";
      if (s < 90) return Math.round(s) + "s";
      if (s < 5400) return Math.round(s / 60) + "m";
      if (s < 172800) return (s / 3600).toFixed(1) + "h";
      return (s / 86400).toFixed(1) + "d";
    },
  };

  const el = (tag, attrs = {}, ...kids) => {
    const n = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (v == null || v === false) continue;
      if (k === "class") n.className = v;
      else if (k === "text") n.textContent = v;
      else n.setAttribute(k, v);
    }
    for (const k of kids) if (k != null) n.append(k);
    return n;
  };
  const SVGNS = "http://www.w3.org/2000/svg";
  const svg = (tag, attrs = {}) => {
    const n = document.createElementNS(SVGNS, tag);
    for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
    return n;
  };

  /* ---------------- animated numbers ---------------- */
  const tweens = new WeakMap();
  function setNum(node, value, formatter) {
    if (!node) return;
    const prev = node._v;
    node._v = value;
    if (value == null || prev == null || reduced || typeof value !== "number" || prev === value) {
      node.textContent = formatter(value);
      if (prev != null && value != null && prev !== value && !reduced) pulse(node);
      return;
    }
    const start = performance.now(), dur = 700, from = prev;
    const old = tweens.get(node);
    if (old) cancelAnimationFrame(old);
    const step = (t) => {
      const k = Math.min(1, (t - start) / dur);
      const e = 1 - Math.pow(1 - k, 3);
      node.textContent = formatter(from + (value - from) * e);
      if (k < 1) tweens.set(node, requestAnimationFrame(step));
      else node.textContent = formatter(value);
    };
    tweens.set(node, requestAnimationFrame(step));
  }
  function setText(node, text) {
    if (node && node.textContent !== text) node.textContent = text;
  }
  function pulse(node) {
    node.classList.remove("flash");
    void node.offsetWidth;
    node.classList.add("flash");
  }

  /* ---------------- sparklines ---------------- */
  const COLORS = { cyan: "#00f0ff", magenta: "#ff2a6d", purple: "#b026ff", green: "#39ff14" };
  function drawSpark(wrap, series, opts = {}) {
    if (!wrap) return;
    const color = COLORS[wrap.dataset.color] || COLORS.cyan;
    const unit = wrap.dataset.unit || "";
    const now = Date.now() / 1000;
    const pts = (series || []).filter((p) => p[1] != null && p[0] >= now - state.window);
    let s = wrap._svg;
    if (!s) {
      s = svg("svg", { viewBox: "0 0 100 40", preserveAspectRatio: "none", role: "img" });
      const id = "g" + Math.random().toString(36).slice(2, 8);
      const defs = svg("defs");
      const lg = svg("linearGradient", { id, x1: "0", y1: "0", x2: "0", y2: "1" });
      lg.append(svg("stop", { offset: "0", "stop-color": color, "stop-opacity": "1" }));
      lg.append(svg("stop", { offset: "1", "stop-color": color, "stop-opacity": "0" }));
      defs.append(lg);
      s.append(defs);
      s._area = svg("path", { class: "spark-area", fill: `url(#${id})` });
      s._base = svg("line", { class: "spark-base", x1: 0, x2: 100, y1: 40, y2: 40 });
      s._line = svg("path", { class: "spark-line", stroke: color });
      s._x = svg("line", { class: "spark-x", y1: 0, y2: 40, visibility: "hidden" });
      s.append(s._area, s._base, s._line, s._x);
      wrap.append(s);
      wrap._dot = el("span", { class: "spark-dotc" });
      wrap._tip = el("div", { class: "spark-tip", hidden: "" });
      wrap._meta = el("div", { class: "spark-meta" });
      wrap._empty = el("div", { class: "spark-empty", text: "no data yet" });
      // end-of-line marker drawn in HTML so it stays round with preserveAspectRatio=none
      wrap._dot.style.cssText = `position:absolute;width:8px;height:8px;border-radius:50%;background:${color};box-shadow:0 0 10px ${color};transform:translate(-50%,-50%);pointer-events:none;border:2px solid var(--cp-panel)`;
      wrap.append(wrap._dot, wrap._tip, wrap._meta, wrap._empty);
      wrap._svg = s;
      const move = (ev) => hover(wrap, ev);
      wrap.addEventListener("pointermove", move);
      wrap.addEventListener("pointerdown", move);
      wrap.addEventListener("pointerleave", () => {
        wrap._tip.hidden = true;
        s._x.setAttribute("visibility", "hidden");
      });
    }
    wrap._pts = pts;
    wrap._unit = unit;
    wrap._empty.hidden = pts.length > 1;
    if (pts.length < 2) {
      s._line.setAttribute("d", "");
      s._area.setAttribute("d", "");
      wrap._dot.style.display = "none";
      wrap._meta.textContent = "";
      return;
    }
    const t0 = now - state.window, t1 = now;
    let max = opts.max != null ? opts.max : Math.max(...pts.map((p) => p[1]));
    if (!(max > 0)) max = 1;
    max *= opts.max != null ? 1 : 1.12;
    wrap._scale = { t0, t1, max };
    const X = (t) => ((t - t0) / (t1 - t0)) * 100;
    const Y = (v) => 40 - (v / max) * 38 - 1;
    let d = "";
    pts.forEach((p, i) => { d += (i ? "L" : "M") + X(p[0]).toFixed(2) + " " + Y(p[1]).toFixed(2); });
    s._line.setAttribute("d", d);
    s._area.setAttribute("d", d + `L${X(pts[pts.length - 1][0]).toFixed(2)} 40L${X(pts[0][0]).toFixed(2)} 40Z`);
    const last = pts[pts.length - 1];
    wrap._dot.style.display = "";
    wrap._dot.style.left = X(last[0]) + "%";
    wrap._dot.style.top = (Y(last[1]) / 40) * 100 + "%";
    const peak = Math.max(...pts.map((p) => p[1]));
    wrap._meta.textContent = `30m · peak ${fmt.n2(peak)}${unit === "%" ? "%" : ""}`;
    s.setAttribute("aria-label", `${unit} over the last 30 minutes, latest ${fmt.n2(last[1])}, peak ${fmt.n2(peak)}`);
  }
  function hover(wrap, ev) {
    const pts = wrap._pts, sc = wrap._scale;
    if (!pts || pts.length < 2 || !sc) return;
    const r = wrap.getBoundingClientRect();
    const fx = Math.min(1, Math.max(0, (ev.clientX - r.left) / r.width));
    const t = sc.t0 + fx * (sc.t1 - sc.t0);
    let best = pts[0];
    for (const p of pts) if (Math.abs(p[0] - t) < Math.abs(best[0] - t)) best = p;
    const x = ((best[0] - sc.t0) / (sc.t1 - sc.t0)) * 100;
    wrap._svg._x.setAttribute("x1", x);
    wrap._svg._x.setAttribute("x2", x);
    wrap._svg._x.setAttribute("visibility", "visible");
    const ago = Math.max(0, Date.now() / 1000 - best[0]);
    wrap._tip.textContent = `${fmt.n2(best[1])} ${wrap._unit} · ${ago < 60 ? Math.round(ago) + "s" : Math.round(ago / 60) + "m"} ago`;
    wrap._tip.hidden = false;
    wrap._tip.style.left = Math.min(Math.max(x, 18), 82) + "%";
  }

  function pushHist(key, t, v) {
    if (v == null) return;
    const arr = (state.hist[key] = state.hist[key] || []);
    if (arr.length && t <= arr[arr.length - 1][0]) return;
    arr.push([t, v]);
    const cut = t - state.window - 60;
    while (arr.length && arr[0][0] < cut) arr.shift();
  }

  /* ---------------- GPU cards ---------------- */
  const ARC = 270, R = 66, C = 2 * Math.PI * R, ARCLEN = (C * ARC) / 360;
  // util gauge; `attr` names the data attribute the card looks its fields up by (data-f / data-g)
  function makeGauge(attr) {
    const gauge = el("div", { class: "gauge", [attr]: "gauge" });
    const g = svg("svg", { viewBox: "0 0 160 160", "aria-hidden": "true" });
    const common = { cx: 80, cy: 80, r: R, "stroke-dasharray": `${ARCLEN} ${C}` };
    g.append(svg("circle", { ...common, class: "g-track" }));
    g.append(svg("circle", { cx: 80, cy: 80, r: R, class: "g-tick", "stroke-dasharray": `1 ${(ARCLEN / 27 - 1).toFixed(2)}` }));
    const val = svg("circle", { ...common, class: "g-val", "stroke-dashoffset": ARCLEN });
    g.append(val);
    gauge.append(g, el("div", { class: "gauge-c" },
      el("div", { class: "num", [attr]: "util" }, "—"),
      el("div", { class: "kpi-l mono" }, "util %")));
    gauge._val = val;
    return gauge;
  }
  function setGauge(gauge, util) {
    const u = Math.max(0, Math.min(100, util || 0));
    gauge._val.setAttribute("stroke-dashoffset", (ARCLEN * (1 - u / 100)).toFixed(1));
    gauge.classList.toggle("hot", u >= 90);
  }
  // PCIe chip: label/level/title are decided server-side (main.py pcie_describe); nothing is assumed about the slot here.
  function setPcie(pc, p) {
    if (p && typeof p === "object") {
      setText(pc, p.label || `PCIe ${p.gen ? `Gen${p.gen} ` : ""}x${p.width}`);
      pc.title = p.title || "PCIe link";
      pc.classList.toggle("chip-note", p.level === "info" || p.level === "muted");
      pc.classList.toggle("chip-warn", p.level === "warn");
    } else {
      setText(pc, "PCIe n/a");
      pc.title = "PCIe link: not readable";
      pc.classList.remove("chip-note", "chip-warn");
    }
  }
  function buildGpu(card, idx) {
    card.replaceChildren();
    const h = el("div", { class: "gpu-h" },
      el("h2", { id: `gpu${idx}-h` }, `GPU${idx}`, el("span", { class: "gpu-name", "data-f": "name" })),
      el("span", { class: "chip chip-info", "data-f": "pcie", title: "PCIe link" }, "PCIe —"));
    const gauge = makeGauge("data-f");
    const vram = el("div", {},
      el("div", { class: "meter-l" }, el("span", {}, "VRAM"), el("span", { "data-f": "vramtxt" }, "—")),
      el("div", { class: "meter", "data-f": "vram" }, el("i")));
    const stat = (k, label, unit) => el("div", { "data-s": k }, el("dt", {}, label), el("dd", {}, el("span", { "data-f": k }, "—"), el("small", {}, unit)));
    const stats = el("dl", { class: "gpu-stats" }, stat("temp", "temp", "°C"), stat("power", "power", "W"), stat("fan", "fan", "%"), stat("clock", "sm clk", "MHz"));
    const side = el("div", { class: "gpu-side" }, vram, stats);
    const main = el("div", { class: "gpu-main" }, gauge, side);
    const foot = el("div", { class: "gpu-foot" }, el("span", { class: "lbl" }, "loaded"), el("span", { "data-f": "models" }));
    const spark = el("div", { class: "gpu-spark" }, el("div", { class: "spark-wrap", "data-spark": `gpu${idx}`, "data-unit": "%", "data-color": idx === 0 ? "cyan" : "purple" }));
    card.append(h, main, foot, spark);
    if (card._lent) card.append(card._lent); // keep the lent/missing placeholder (built once, see setGpuSlot)
    card._built = true;
  }

  function renderGpu(g) {
    const card = $(`#gpu${g.index}`);
    if (!card) return;
    if (!card._built) buildGpu(card, g.index); // not firstChild: the lent placeholder may be there first
    const f = (k) => card.querySelector(`[data-f="${k}"]`);
    setText(f("name"), g.name ? "· " + g.name.replace("NVIDIA GeForce ", "") : "");
    setNum(f("util"), g.util, fmt.int);
    setGauge(f("gauge"), g.util);

    const used = g.vram_used_mib, tot = g.vram_total_mib;
    const pct = used != null && tot ? (used / tot) * 100 : 0;
    setText(f("vramtxt"), used != null && tot ? `${(used / 1024).toFixed(1)} / ${(tot / 1024).toFixed(1)} GiB · ${Math.round(pct)}%` : "—");
    const m = f("vram");
    m.firstChild.style.width = pct.toFixed(1) + "%";
    m.classList.toggle("hot", pct >= 95);

    setNum(f("temp"), g.temp_c, fmt.int);
    setNum(f("power"), g.power_w, fmt.int);
    setNum(f("fan"), g.fan_pct, fmt.int);
    setNum(f("clock"), g.sm_clock_mhz, fmt.int);
    const tempBox = card.querySelector('[data-s="temp"]');
    tempBox.classList.toggle("hot", g.temp_c >= 78 && g.temp_c < 85);
    tempBox.classList.toggle("crit", g.temp_c >= 85);
    card.classList.toggle("crit", g.temp_c >= 85);

    setPcie(f("pcie"), g.pcie);

    const mw = f("models");
    const ids = (g.models || []).map((id) => {
      const mm = (state.snap.models || []).find((x) => x.id === id);
      return mm && mm.alias ? `${mm.alias}` : id;
    });
    const key = ids.join("|");
    if (mw._k !== key) {
      mw._k = key;
      mw.replaceChildren(...(ids.length ? ids.map((t, i) => el("span", { class: "chip chip-model", title: g.models[i] }, t)) : [el("span", { class: "muted mono", text: "— idle —" })]));
      mw.style.cssText = "display:inline-flex;flex-wrap:wrap;gap:6px";
    }
  }

  /* ---------------- hypervisor: GPU mode + gaming VM ---------------- */
  // snapshot.hypervisor is optional (older backends omit it): without it the mode pill and gaming panel stay hidden.
  const MODES = ["ai", "game", "switching", "unknown"];
  const fin = (v) => (typeof v === "number" && isFinite(v) ? v : null);
  const str = (v) => (typeof v === "string" && v ? v : null);
  const hvOf = (s) => (s && s.hypervisor && typeof s.hypervisor === "object" ? s.hypervisor : null);
  const hvMode = (hv) => (hv && MODES.includes(hv.mode) ? hv.mode : "unknown");
  const lending = (hv) => !!hv && (hv.mode === "game" || hv.mode === "switching");
  // Walter (which serves this page) restarts during a GPU mode switch; if the stream drops while the last snapshot
  // said switching or game, the drop is most likely that restart, not an outage.
  const switchSuspected = () => lending(hvOf(state.snap));
  // seconds since a server unix ts, measured on the server clock (no client clock skew)
  const sinceAge = (ts) => (fin(ts) == null || !state.lastRecv ? null : Math.max(0, state.lastTs - ts + (Date.now() - state.lastRecv) / 1000));
  const dirText = (d) => (d === "to-game" ? "→ game" : d === "to-ai" ? "→ AI" : null);

  function modeTitle(hv) {
    const age = sinceAge(hv.since);
    return [
      `GPU mode: ${hvMode(hv)}${hv.up ? "" : " (hypervisor exporter not reachable)"}`,
      `phase: ${str(hv.phase) || "—"}`,
      `direction: ${dirText(hv.direction) || "—"}`,
      `since: ${age == null ? "—" : fmt.age(age) + " ago · " + new Date(hv.since * 1000).toLocaleTimeString("en-GB", { hour12: false })}`,
      `host CPUs (system.slice): ${str(hv.host_cpus) || "—"}`,
    ].join("\n");
  }

  function renderMode(s) {
    const p = $("#mode-pill");
    const hv = hvOf(s);
    p.hidden = !hv;
    if (!hv) return;
    const mode = hvMode(hv);
    p.className = `pill mode mode-${mode}`;
    setText($(".pill-text", p), mode === "unknown" ? "HYPERVISOR OFFLINE" : str(hv.label) || `${mode.toUpperCase()} MODE`);
    p.title = modeTitle(hv);
  }

  /* Walter GPU slots. index.html has one static section per Walter GPU (gpu0, gpu1). In game mode the hypervisor
     passes one card through to the gaming VM and Walter's DCGM then reports only the other one. Assumption: a static
     slot whose index is absent from snapshot.gpus while mode is game/switching is the lent card; which index that is
     comes from the data, not from here. Absent outside game mode = "not reported" (only when some GPU is reported,
     so a Prometheus outage keeps the last values like before). */
  function setGpuSlot(card, kind, s) {
    const idx = card.dataset.gpu;
    if (!kind && !card._lent) return;
    if (!card._lent) {
      card._lent = el("a", { class: "gpu-lent" },
        el("div", { class: "gpu-h" },
          el("h2", { id: `gpu${idx}-lent-h` }, `GPU${idx}`, el("span", { class: "gpu-name", "data-l": "name" })),
          el("span", { class: "chip chip-lend", "data-l": "chip" })),
        el("div", { class: "lent-body" },
          el("div", { class: "lent-big", "data-l": "big" }),
          el("p", { class: "lent-sub mono", "data-l": "sub" }),
          el("div", { class: "lent-live mono", "data-l": "live" }),
          el("span", { class: "lent-cta mono", "data-l": "cta", text: "view gaming VM ↓" })));
      card.append(card._lent);
    }
    const L = (k) => card._lent.querySelector(`[data-l="${k}"]`);
    card.classList.toggle("is-lent", kind === "lent");
    card.classList.toggle("is-missing", kind === "missing");
    card.setAttribute("aria-labelledby", kind ? `gpu${idx}-lent-h` : `gpu${idx}-h`);
    if (!kind) return;
    card.classList.remove("crit");
    const hv = hvOf(s);
    if (kind === "lent") {
      card._lent.setAttribute("href", "#gaming");
      const switching = hv.mode === "switching";
      setText(L("name"), switching ? "· in transit" : "· lent");
      setText(L("chip"), "VFIO → gaming VM");
      setText(L("big"), `GPU${idx} → gaming VM`);
      setText(L("sub"), switching
        ? `GPU mode switch in progress (${str(hv.phase) || "—"}${dirText(hv.direction) ? ", " + dirText(hv.direction) : ""}). Walter runs on the remaining GPU until it is back.`
        : "Passed through to the gaming VM. Walter serves models from the remaining GPU until the switch back to AI mode.");
      const g = hv.gpu && typeof hv.gpu === "object" ? hv.gpu : null;
      const vm = hv.vm && typeof hv.vm === "object" ? hv.vm : null;
      setText(L("live"), g && vm && vm.guest_up
        ? `in guest · ${fmt.int(fin(g.util))}% util · ${fmt.int(fin(g.temp))} °C · ${fmt.int(fin(g.power))} W`
        : vm && vm.running ? "guest booting / unreachable" : `VM ${str(vm && vm.state) || "not running"}`);
      L("cta").hidden = false;
    } else {
      card._lent.removeAttribute("href");
      setText(L("name"), "· not reported");
      setText(L("chip"), "no data");
      setText(L("big"), `GPU${idx} not reported`);
      setText(L("sub"), `DCGM reports ${(s.gpus || []).length} GPU(s) and not this one${hv && hv.mode === "unknown" ? "; GPU mode unknown (hypervisor offline), it may be lent to the gaming VM" : ""}. The card comes back when the GPU does.`);
      setText(L("live"), "");
      L("cta").hidden = true;
    }
  }
  function renderGpuSlots(s) {
    const gpus = s.gpus || [];
    const seen = new Set(gpus.map((g) => String(g.index)));
    const lent = lending(hvOf(s));
    for (const card of $$("section.gpu[data-gpu]")) {
      const absent = !seen.has(card.dataset.gpu);
      setGpuSlot(card, absent ? (lent ? "lent" : gpus.length ? "missing" : null) : null, s);
    }
  }

  function setChip(node, cls, text, title) {
    node.className = `chip chip-st ${cls}`;
    setText(node, text);
    node.title = title || "";
  }
  function setMeter(m, pct, hotAt) {
    const v = pct == null ? 0 : Math.max(0, Math.min(100, pct));
    m.firstChild.style.width = v.toFixed(1) + "%";
    m.classList.toggle("hot", pct != null && pct >= hotAt);
  }

  function renderGaming(s) {
    const sec = $("#gaming");
    const hv = hvOf(s);
    const vm = hv && hv.vm && typeof hv.vm === "object" ? hv.vm : null;
    const show = !!hv && (lending(hv) || (!!vm && vm.running === true));
    sec.hidden = !show;
    if (!show) return;
    const G = (k) => sec.querySelector(`[data-g="${k}"]`);
    if (!sec._gauge) {
      sec._gauge = makeGauge("data-g");
      G("gauge-slot").replaceWith(sec._gauge);
    }
    const running = !!vm && vm.running === true;
    const guestUp = running && vm.guest_up === true;

    // header
    setText(G("tag"), vm ? `${str(vm.state) || "?"} · ${fmt.int(fin(vm.vcpus))} vCPU` : "VM not reported");
    const sw = G("switch");
    sw.hidden = hv.mode !== "switching";
    if (hv.mode === "switching") {
      const lbl = str(hv.label) || "switching", ph = str(hv.phase);
      setText(sw, `GPU mode switch in progress · ${lbl}${ph && !lbl.includes(ph) ? " · phase " + ph : ""}`);
    }

    // VM (host-side virsh stats: valid whenever the VM runs, guest or not)
    if (!running) setChip(G("guest"), "chip-unk", `VM ${str(vm && vm.state) || "not running"}`);
    else if (guestUp) setChip(G("guest"), "chip-on", "guest up", "last guest poll succeeded");
    else setChip(G("guest"), "chip-wait", "guest booting / unreachable", "the VM runs but the guest did not answer the last poll");
    const tri = (node, v, on, off, unk) => {
      if (!guestUp || v == null) setChip(node, "chip-unk", unk, guestUp ? "not reported" : "needs the guest");
      else setChip(node, v ? on[0] : off[0], v ? on[1] : off[1]);
    };
    tri(G("sunshine"), vm && vm.sunshine, ["chip-on", "sunshine"], ["chip-off", "sunshine down"], "sunshine —");
    tri(G("stream"), vm && vm.streaming, ["chip-live", "streaming"], ["chip-idle", "no client"], "stream —");
    setNum(G("cpu"), running ? fin(vm.cpu_pct) : null, fmt.n1);
    setText(G("cpu-u"), `% of ${fmt.int(fin(vm && vm.vcpus))} vCPU`);
    setNum(G("mem"), running ? fin(vm.mem_gib) : null, fmt.n1);
    setText(G("disk-r"), fmt.rate(running ? fin(vm.disk_read_bps) : null));
    setText(G("disk-w"), fmt.rate(running ? fin(vm.disk_write_bps) : null));
    setText(G("net-rx"), fmt.rate(running ? fin(vm.net_rx_bps) : null));
    setText(G("net-tx"), fmt.rate(running ? fin(vm.net_tx_bps) : null));
    const age = hv.mode === "game" ? sinceAge(hv.since) : null;
    setText(G("since"), age == null ? "—" : fmt.age(age));

    // GPU (from inside the guest: only present while the guest is up; no zeros when it is not)
    const g = guestUp && hv.gpu && typeof hv.gpu === "object" ? hv.gpu : null;
    G("gpu").classList.toggle("is-off", !g);
    G("off").hidden = !!g;
    G("gpu-main").hidden = !g;
    if (!g) setText(G("off"), !running ? `VM ${str(vm && vm.state) || "not running"}: no GPU telemetry`
      : !guestUp ? "guest booting / unreachable · GPU telemetry comes from inside the guest"
      : "guest up, GPU not reported");
    setText(G("gpu-name"), g && str(g.name) ? "· " + g.name.replace("NVIDIA GeForce ", "") : "");
    const util = g ? fin(g.util) : null;
    setNum(G("util"), util, fmt.int);
    setGauge(sec._gauge, util);
    const used = g ? fin(g.mem_used_gib) : null, tot = g ? fin(g.mem_total_gib) : null;
    const vpct = used != null && tot ? (used / tot) * 100 : null;
    setText(G("vramtxt"), vpct != null ? `${used.toFixed(1)} / ${tot.toFixed(1)} GiB · ${Math.round(vpct)}%` : "—");
    setMeter(G("vram"), vpct, 95);
    const enc = g ? fin(g.enc_util) : null;
    setText(G("enctxt"), enc == null ? "—" : `${Math.round(enc)}%`);
    setMeter(G("enc"), enc, 90);
    const temp = g ? fin(g.temp) : null;
    setNum(G("temp"), temp, fmt.int);
    setNum(G("power"), g ? fin(g.power) : null, fmt.int);
    const lim = g ? fin(g.power_limit) : null;
    setText(G("power-u"), lim == null ? "W" : ` / ${Math.round(lim)} W`);
    setNum(G("fan"), g ? fin(g.fan) : null, fmt.int);
    setNum(G("clock"), g ? fin(g.sm_clock) : null, fmt.int);
    const tb = sec.querySelector('[data-s="temp"]');
    tb.classList.toggle("hot", temp != null && temp >= 78 && temp < 85);
    tb.classList.toggle("crit", temp != null && temp >= 85);
    setPcie(G("pcie"), g ? g.pcie : null);
    if (!g) G("pcie").title = "PCIe link: needs the guest";
    sec.classList.toggle("crit", temp != null && temp >= 85);
    sec.classList.toggle("warn", running && !guestUp && !(temp != null && temp >= 85));
  }

  /* ---------------- models ---------------- */
  function renderModels(s) {
    const tb = $("#models-tbl tbody");
    const rows = (s.models || []).map((m) => {
      const loaded = m.state !== "unloaded";
      const busy = (m.processing || 0) > 0;
      const decode = m.decode_tps != null ? m.decode_tps : null;
      return el("tr", { class: loaded ? null : "dim" },
        el("td", { title: m.name }, el("span", { class: "m-id", text: m.id }), m.alias ? el("span", { class: "m-alias", text: m.alias }) : null),
        el("td", {}, el("span", { class: `st st-${["ready", "starting", "stopping", "unloaded"].includes(m.state) ? m.state : "starting"}`, title: m.state, text: m.state })),
        el("td", { class: "c-gpu", text: (m.gpus || []).map((x) => "G" + x).join("+") || "—" }),
        el("td", { class: "r" },
          decode != null ? fmt.n1(decode) : "—"),
        el("td", { class: "r", text: m.prompt_tps != null ? fmt.n1(m.prompt_tps) : "—" }),
        el("td", { class: "r", title: busy && m.decode_calls_s ? "live llama-server decode steps per second (token counters only update when a request finishes)" : null },
          loaded ? el("span", { class: busy ? "busy" : "muted" }, `${m.processing ?? 0}${m.deferred ? " +" + m.deferred : ""}`,
            busy && m.decode_calls_s ? el("span", { class: "sts", text: ` · ${fmt.n1(m.decode_calls_s)} st/s` }) : null) : "—"));
    });
    if (!rows.length) rows.push(el("tr", { class: "empty-row" }, el("td", { colspan: 6, text: "no models reported" })));
    tb.replaceChildren(...rows);
    const ready = (s.models || []).filter((m) => m.state === "ready").length;
    setText($("#models-tag"), `${ready} loaded · tok/s over 10m`);
    setNum($('[data-k="tps"]'), s.spark.tps, fmt.n1);
  }

  /* ---------------- gateway ---------------- */
  function renderGateway(s) {
    const g = s.gateway || {};
    setNum($('[data-k="rpm"]'), g.rpm, fmt.n1);
    setNum($('[data-k="inflight"]'), g.in_flight, fmt.int);
    setText($('[data-k="p50"]'), fmt.secs(g.p50_s));
    setText($('[data-k="p95"]'), fmt.secs(g.p95_s));
    setText($('[data-k="ttft"]'), fmt.secs(g.ttft_p50_s));
    setNum($('[data-k="req1h"]'), g.requests_1h, fmt.int);
    setNum($('[data-k="err1h"]'), g.errors_1h, fmt.int);
    $('[data-k="err1h"]').parentElement.classList.toggle("bad", (g.errors_1h || 0) > 0);
    setText($('[data-k="tin"]'), fmt.si(g.tokens_in_1h));
    setText($('[data-k="tout"]'), fmt.si(g.tokens_out_1h));
    const since = g.today_since ? new Date(g.today_since) : null;
    setText($("#today-since"), since ? `· since ${since.toISOString().slice(11, 16)} UTC` : "");
    const tb = $("#keys-tbl tbody");
    const rows = (g.keys_today || []).map((k) => el("tr", {},
      el("td", { text: k.alias }),
      el("td", { class: "r", text: fmt.int(k.requests) }),
      el("td", { class: "r" }, el("span", { class: k.errors ? "busy" : "muted", text: fmt.int(k.errors) })),
      el("td", { class: "r", text: fmt.si(k.tokens_in) }),
      el("td", { class: "r", text: fmt.si(k.tokens_out) })));
    if (!rows.length) rows.push(el("tr", { class: "empty-row" }, el("td", { colspan: 5, text: "no requests today" })));
    tb.replaceChildren(...rows);
  }

  /* ---------------- host ---------------- */
  function bar(label, used, total, extra) {
    const pct = total ? (used / total) * 100 : 0;
    const m = el("div", { class: "meter" + (pct >= 90 ? " crit" : pct >= 80 ? " hot" : "") }, el("i"));
    m.firstChild.style.width = pct.toFixed(1) + "%";
    return el("div", {},
      el("div", { class: "meter-l" }, el("span", { text: label }), el("span", {}, el("b", { text: `${fmt.bytes(used)}` }), ` / ${fmt.bytes(total)} · ${Math.round(pct)}%${extra || ""}`)),
      m);
  }
  function renderHost(s) {
    const h = s.host || {};
    setNum($('[data-k="cpu"]'), h.cpu_pct, fmt.n1);
    const bars = [];
    if (h.mem_total) bars.push(bar("RAM", h.mem_used, h.mem_total));
    for (const d of h.disks || []) bars.push(bar(`disk ${d.mount}`, d.used, d.size));
    // update widths in place to keep the transition
    const wrap = $("#host-bars");
    if (wrap.children.length === bars.length) {
      bars.forEach((b, i) => {
        const old = wrap.children[i];
        old.querySelector(".meter-l").replaceWith(b.querySelector(".meter-l"));
        const om = old.querySelector(".meter"), nm = b.querySelector(".meter");
        om.className = nm.className;
        om.firstChild.style.width = nm.firstChild.style.width;
      });
    } else wrap.replaceChildren(...bars);
    const net = $("#host-net");
    const cells = [el("div", {}, el("dt", { text: "load 1m" }), el("dd", { class: "num-s", text: h.load1 != null ? h.load1.toFixed(2) : "—" }))];
    for (const n of h.net || []) {
      cells.push(el("div", { title: `${n.dev}: receive / transmit` }, el("dt", { text: `${n.dev} ↓ / ↑` }),
        el("dd", { class: "num-s net" }, el("span", { text: fmt.rate(n.rx_bps) }), el("span", { class: "muted", text: fmt.rate(n.tx_bps) }))));
    }
    net.replaceChildren(...cells);
  }

  /* ---------------- services, alerts ---------------- */
  function renderServices(s) {
    const list = $("#svc-list");
    const items = (s.services || []).map((v) => el("li", { class: "svc" + (v.up ? "" : " down"), title: v.cert_days != null ? `TLS cert: ${Math.round(v.cert_days)} days left` : null },
      el("span", { class: "ic", "aria-hidden": "true", text: v.up ? "▲" : "✕" }),
      el("span", { class: "nm" }, `${v.name}`, el("small", { text: v.scope })),
      el("span", { class: "lat", text: v.up ? `${v.latency_ms < 10 ? v.latency_ms.toFixed(1) : Math.round(v.latency_ms)} ms` : `DOWN${v.status ? " " + v.status : ""}` }),
      el("span", { class: "sr-only", hidden: "", text: v.up ? "up" : "down" })));
    if (!items.length) items.push(el("li", { class: "muted mono", text: "no probe data" }));
    list.replaceChildren(...items);
    const t = s.targets || {};
    setText($("#targets-tag"), `${t.up ?? "—"}/${t.total ?? "—"} targets up`);
    $("#targets-tag").style.color = t.down && t.down.length ? "var(--cp-red)" : "";
    $("#targets-tag").title = (t.down || []).join(", ");

    const al = $("#alert-list");
    const firing = (s.alerts && s.alerts.firing) || [];
    const pending = (s.alerts && s.alerts.pending) || [];
    const rows = [...firing, ...pending].map((a) => el("li", { class: `alert ${a.state === "pending" ? "pending" : a.severity === "critical" ? "" : "warning"}` },
      el("span", { class: "ic", "aria-hidden": "true", text: a.state === "pending" ? "◷" : "⚠" }),
      el("div", {}, el("b", { text: a.name }), el("span", { class: "sev", text: `${a.severity} · ${a.state}` })),
      el("p", { text: a.summary || "" })));
    if (!rows.length) rows.push(el("li", { class: "all-clear", text: "no alerts firing or pending" }));
    al.replaceChildren(...rows);
    $(".services").classList.toggle("crit", firing.length > 0 || (s.services || []).some((v) => !v.up));
  }

  /* ---------------- backup + sources ---------------- */
  function renderBackup(s) {
    const b = s.backup;
    const tag = $("#bk-tag");
    if (b) {
      setText($("#bk-name"), b.name);
      setText($("#bk-age"), fmt.age(b.age_s));
      const cls = b.age_s < 36 * 3600 ? "bk-ok" : b.age_s < 72 * 3600 ? "bk-old" : "bk-bad";
      tag.className = "tag mono " + cls;
      setText(tag, cls === "bk-ok" ? "✓ fresh" : cls === "bk-old" ? "⚠ aging" : "✕ overdue");
    } else {
      setText($("#bk-name"), "unavailable");
      setText($("#bk-age"), "—");
      tag.className = "tag mono bk-bad";
      setText(tag, "✕ unknown");
    }
    const src = $("#sources");
    src.replaceChildren(...Object.entries(s.sources || {}).map(([k, v]) =>
      el("span", { class: "src" + (v.ok ? "" : " bad"), title: v.ok ? (v.latency_ms != null ? `${v.latency_ms} ms` : "ok") : v.error || "error", text: k.replace("_", "-") })));
  }

  /* ---------------- status pill / freshness ---------------- */
  function renderStatus(s) {
    const p = $("#status-pill");
    const lvl = s.status.level;
    const label = { ok: "ALL SYSTEMS NOMINAL", degraded: "DEGRADED", alert: "ALERTS FIRING" }[lvl] || lvl.toUpperCase();
    p.className = `pill pill-${lvl}`;
    setText($(".pill-text", p), label);
    p.title = s.status.reasons.join("\n") || "all checks passing";
    const ban = $("#banner");
    if (!document.body.classList.contains("stale")) ban.classList.remove("banner-switch");
    if (lvl !== "ok" && s.status.reasons.length && !document.body.classList.contains("stale")) {
      ban.hidden = false;
      setText(ban, s.status.reasons.join(" · "));
      ban.style.color = lvl === "alert" ? "var(--cp-red)" : "";
      ban.style.borderColor = lvl === "alert" ? "var(--cp-red)" : "";
    } else if (!document.body.classList.contains("stale")) ban.hidden = true;
  }

  function tickFresh() {
    const f = $("#fresh"), t = $("#fresh-text");
    if (!state.lastRecv) return;
    const age = (Date.now() - state.lastRecv) / 1000;
    const stale = age * 1000 > STALE_MS || !state.connected;
    f.classList.toggle("live", !stale);
    f.classList.toggle("stale", stale);
    document.body.classList.toggle("stale", stale);
    const at = new Date(state.lastTs * 1000).toLocaleTimeString("en-GB", { hour12: false });
    const hv = hvOf(state.snap);
    const mp = $("#mode-pill");
    if (stale) {
      setText(t, `${state.connected ? "STALE" : "RECONNECTING"} · last ${at} (${fmt.age(age)} ago)`);
      const ban = $("#banner");
      ban.hidden = false;
      ban.style.color = ban.style.borderColor = "";
      const sw = switchSuspected();
      ban.classList.toggle("banner-switch", sw);
      if (sw) {
        const last = str(hv.phase) || (hv.mode === "game" ? "game mode" : "—");
        const dir = dirText(hv.direction);
        setText(ban, `Walter restarting — GPU mode switch in progress (last phase: ${last}${dir ? ", " + dir : ""}). This takes a few minutes; reconnecting automatically. Values shown are from ${at}.`);
        mp.className = "pill mode mode-switching";
        setText($(".pill-text", mp), "WALTER RESTARTING");
      } else {
        setText(ban, `Live stream ${state.connected ? "stalled" : "disconnected"}; reconnecting. Values shown are from ${at} and may be out of date.`);
      }
      const p = $("#status-pill");
      p.className = "pill pill-wait";
      setText($(".pill-text", p), "STALE DATA");
    } else {
      setText(t, `live · updated ${age < 1.5 ? "just now" : Math.round(age) + "s ago"}`);
    }
    // relative "since" times keep counting between snapshots
    if (hv && !mp.hidden) mp.title = modeTitle(hv);
    const gs = $('#gaming [data-g="since"]');
    if (hv && gs) { const a = hv.mode === "game" ? sinceAge(hv.since) : null; setText(gs, a == null ? "—" : fmt.age(a)); }
  }

  /* ---------------- main render ---------------- */
  function render(s, fromStream) {
    state.prev = state.snap;
    state.snap = s;
    state.lastTs = s.ts;
    state.lastRecv = Date.now();
    if (fromStream) for (const [k, v] of Object.entries(s.spark || {})) pushHist(k, Math.floor(s.ts), v);
    // gaming GPU util is not in snapshot.spark; extend history.gpu_gaming from the live value (pushHist dedupes by ts)
    const hv = hvOf(s), prevMode = hvOf(state.prev) && state.prev.hypervisor.mode;
    // entering game mode without a reload: fetch the gaming GPU's 30 min history once
    if (fromStream && hv && hv.mode === "game" && prevMode && prevMode !== "game") refreshHistory();
    if (fromStream && hv && hv.gpu && hv.vm && hv.vm.guest_up) pushHist("gpu_gaming", Math.floor(s.ts), fin(hv.gpu.util));
    document.body.classList.remove("stale");
    renderStatus(s);
    renderMode(s);
    (s.gpus || []).forEach(renderGpu);
    renderGpuSlots(s);
    renderGaming(s);
    renderModels(s);
    renderGateway(s);
    renderHost(s);
    renderServices(s);
    renderBackup(s);
    for (const w of $$("[data-spark]")) {
      const k = w.dataset.spark;
      drawSpark(w, state.hist[k], { max: k.startsWith("gpu") || k === "cpu" ? 100 : null });
    }
    setText($("#foot-seq"), `seq ${s.seq}`);
    tickFresh();
  }

  /* ---------------- data ---------------- */
  function applyHistory(h) {
    if (!h || !h.series) return;
    state.window = h.window_s || state.window;
    for (const [k, arr] of Object.entries(h.series)) if (Array.isArray(arr)) state.hist[k] = arr.filter((p) => p[1] != null);
  }
  // after a long outage (e.g. Walter restarted for a GPU mode switch) refill the sparkline gaps from Prometheus
  async function refreshHistory() {
    try {
      const r = await fetch("/api/v1/telemetry", { cache: "no-store", credentials: "same-origin" });
      if (!r.ok) return;
      const d = await r.json();
      const live = { ...state.hist };
      applyHistory(d.history);
      // keep live points newer than the fetched history
      for (const [k, arr] of Object.entries(live)) {
        const h = state.hist[k];
        if (h === arr) continue;
        const lastT = h.length ? h[h.length - 1][0] : -Infinity;
        for (const p of arr) if (p[0] > lastT) h.push(p);
      }
    } catch (e) {
      console.warn("history refresh failed:", e.message);
    }
  }

  async function loadInitial() {
    try {
      const r = await fetch("/api/v1/telemetry", { cache: "no-store", credentials: "same-origin" });
      if (!r.ok) throw new Error("HTTP " + r.status);
      const d = await r.json();
      if (d.viewer && (d.viewer.user || d.viewer.email)) {
        const v = $("#viewer");
        v.hidden = false;
        v.textContent = d.viewer.email || d.viewer.user;
        v.title = "signed in via Pocket-ID";
      }
      applyHistory(d.history);
      if (d.snapshot && d.snapshot.ts) render(d.snapshot, true);
    } catch (e) {
      console.warn("initial load failed:", e.message);
    }
  }

  let backoff = 1000, retryTimer = 0, downSince = 0;
  function connect() {
    clearTimeout(retryTimer);
    retryTimer = 0;
    if (state.es) state.es.close();
    const es = new EventSource("/api/v1/stream");
    state.es = es;
    es.addEventListener("open", () => {
      state.connected = true;
      backoff = 1000;
      if (downSince && Date.now() - downSince > 60000) refreshHistory();
      downSince = 0;
      tickFresh();
    });
    es.addEventListener("snapshot", (ev) => {
      state.connected = true;
      try { render(JSON.parse(ev.data), true); } catch (e) { console.warn("bad snapshot", e); }
    });
    es.addEventListener("error", () => {
      if (es !== state.es) return;
      state.connected = false;
      if (!downSince) downSince = Date.now();
      tickFresh();
      if (es.readyState === EventSource.CLOSED && !retryTimer) {
        // browser gave up (e.g. edge returned non-2xx): retry ourselves with backoff. During a GPU mode switch
        // Walter is expected back within minutes, so poll more eagerly (cap 5 s instead of 30 s).
        retryTimer = setTimeout(connect, backoff);
        backoff = Math.min(backoff * 2, switchSuspected() ? 5000 : 30000);
      }
    });
  }

  document.addEventListener("DOMContentLoaded", async () => {
    await loadInitial();
    connect();
    setInterval(tickFresh, 1000);
    // redraw sparklines every 10 s so the time axis keeps sliding while stale
    setInterval(() => { if (state.snap) for (const w of $$("[data-spark]")) drawSpark(w, state.hist[w.dataset.spark], { max: w.dataset.spark.startsWith("gpu") || w.dataset.spark === "cpu" ? 100 : null }); }, 10000);
  });
})();

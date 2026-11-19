"use strict";
/* Trio log viewer (served by `trio-logs web`).
 *
 * Log text is untrusted: every piece of it reaches the page as a DOM text node (see h()). The
 * chart's SVG markup is built from numbers and fixed labels only.
 */

// ------------------------------------------------------------------ helpers

const $ = (s, r = document) => r.querySelector(s);

function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  if (attrs) {
    for (const [k, v] of Object.entries(attrs)) {
      if (v == null || v === false) continue;
      if (k === "class") el.className = v;
      else if (k === "style") el.style.cssText = v;
      else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
      else el.setAttribute(k, v === true ? "" : v);
    }
  }
  for (const k of kids.flat(Infinity)) {
    if (k == null || k === false) continue;
    el.append(k instanceof Node ? k : String(k));
  }
  return el;
}

const pad = (n, w = 2) => String(n).padStart(w, "0");
const local = (t, off) => new Date(t + off * 60000);
function fmtClock(t, off, ms = false) {
  const d = local(t, off);
  const s = `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}:${pad(d.getUTCSeconds())}`;
  return ms ? `${s}.${pad(d.getUTCMilliseconds(), 3)}` : s;
}
function fmtDay(t, off) {
  const d = local(t, off);
  return `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())}`;
}
const WEEKDAY = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const fmtWeekday = (t, off) => WEEKDAY[local(t, off).getUTCDay()];
const fmtFull = (t, off, ms = true) => `${fmtDay(t, off)} ${fmtClock(t, off, ms)}`;
function fmtOff(off) {
  const a = Math.abs(off);
  return `${off < 0 ? "-" : "+"}${pad(Math.floor(a / 60))}${pad(a % 60)}`;
}
function fmtSpan(ms) {
  const m = Math.round(ms / 60000);
  if (m < 1) return `${Math.round(ms / 1000)} s`;
  if (m < 60) return `${m} min`;
  const hh = Math.floor(m / 60);
  if (hh < 48) return m % 60 ? `${hh} h ${m % 60} min` : `${hh} h`;
  return `${(hh / 24).toFixed(1)} days`;
}
function num(v, digits = 2) {
  if (v == null || v === "") return "";
  if (typeof v !== "number") return String(v);
  return Number.isInteger(v) ? String(v) : String(+v.toFixed(digits));
}
const thousands = (n) => n.toLocaleString("en-US");

function lowerBound(arr, v, key) {
  let lo = 0, hi = arr.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if ((key ? key(arr[mid]) : arr[mid]) < v) lo = mid + 1;
    else hi = mid;
  }
  return lo;
}

function debounce(fn, ms) {
  let id = 0;
  return (...a) => { clearTimeout(id); id = setTimeout(() => fn(...a), ms); };
}

function srcBadge(name) {
  const known = ["Trio", "Pump", "CGM", "Algorithm", "Watch"].includes(name);
  return h("span", { class: `src src-${known ? name.toLowerCase() : "other"}` }, name);
}
const sevBadge = (sev) => h("span", { class: `sev ${sev}` }, sev);

const LEVEL_RANK = { WARN: 1, WARNING: 1, TORN: 1, ORPHAN: 1, ERROR: 2, FAULT: 2 };
const isBad = (s) => /fail|timeout|timed out|blocked|refused|misdated|no outcome|cancelled before|disconnect|not on the pump/i.test(s || "");

// ------------------------------------------------------------------ state

const S = {
  catalog: null,
  key: null,
  D: null, // the loaded view (see viewer.bundle)
  full: [0, 1],
  view: [0, 1],
  cursor: null,
  tab: "log",
  windowOnly: true,
  history: null,
  lower: null, // lowercased log text, built on first search
  sel: {}, // tab id -> selected row
  f: {
    log: { q: "", src: null, level: 0, cat: -1 },
    doses: { q: "", who: new Set(["Trio", "Driver", "Pump"]) },
    cgm: { q: "" },
    oref: { q: "", kind: "all" },
    comms: { q: "", req: "", outcome: "all", minMs: 0 },
    history: { q: "", type: "" },
    findings: { q: "", sev: "all", check: "" },
  },
};

const lineOff = (i) => (i != null && S.D ? S.D.lines.o[i] : S.D ? S.D.tz : 0);
const rowOff = (row) => lineOff(row && row.line);
const zoomed = () => S.view[0] > S.full[0] || S.view[1] < S.full[1];

function inWindow(t) {
  return !S.windowOnly || !zoomed() || (t >= S.view[0] && t <= S.view[1]);
}

function lineText(i) {
  const L = S.D.lines;
  const x = L.extra[i];
  return x ? `${L.m[i]}\n${x}` : L.m[i];
}

function matcher(q) {
  q = (q || "").trim();
  if (!q) return null;
  const m = /^\/(.+)\/([a-z]*)$/.exec(q);
  if (m) {
    try {
      const rx = new RegExp(m[1], m[2].includes("i") ? m[2] : m[2] + "i");
      return (s) => rx.test(s);
    } catch {
      return () => false;
    }
  }
  const phrase = q.toLowerCase();
  return (s) => s.toLowerCase().includes(phrase);
}

// ------------------------------------------------------------------ virtual table

class VTable {
  constructor(root) {
    this.root = root;
    this.headRow = h("div", { class: "vt-headrow" });
    this.head = h("div", { class: "vt-head" }, this.headRow);
    this.body = h("div", { class: "vt-body", tabindex: "0" });
    this.spacer = h("div", { class: "vt-spacer" });
    this.win = h("div", { class: "vt-win" });
    this.body.append(this.spacer, this.win);
    root.replaceChildren(this.head, this.body);
    this.rh = 20;
    this.rows = [];
    this.cols = [];
    this.selIndex = -1;
    this.onSelect = null;
    this.onTop = null;
    this.rowClass = null;
    this.raf = 0;
    this.body.addEventListener("scroll", () => { this.head.scrollLeft = this.body.scrollLeft; this.schedule(); });
    new ResizeObserver(() => this.schedule()).observe(this.body);
    this.body.addEventListener("click", (e) => {
      const r = e.target.closest(".vt-row");
      if (r) this.select(+r.dataset.i, { scroll: false });
    });
    this.body.addEventListener("keydown", (e) => {
      const page = Math.max(1, Math.floor(this.body.clientHeight / this.rh) - 1);
      const step = { ArrowDown: 1, j: 1, ArrowUp: -1, k: -1, PageDown: page, PageUp: -page }[e.key];
      if (step && this.rows.length) {
        e.preventDefault();
        this.select(Math.max(0, Math.min(this.rows.length - 1, (this.selIndex < 0 ? 0 : this.selIndex + step))));
      } else if (e.key === "Home" || e.key === "End") {
        e.preventDefault();
        if (this.rows.length) this.select(e.key === "Home" ? 0 : this.rows.length - 1);
      }
    });
  }

  set(cols, rows, selected, empty) {
    this.cols = cols;
    this.rows = rows;
    this.empty = empty;
    this.root.style.setProperty("--cols", cols.map((c) => c.w).join(" "));
    // narrowest the columns may get before the table scrolls sideways
    const minW = cols.reduce((sum, c) => sum + (+(/(\d+(?:\.\d+)?)px/.exec(c.w) || [0, 160])[1]), 0) + 10 * (cols.length - 1) + 20;
    this.root.style.setProperty("--minw", `${minW}px`);
    this.headRow.replaceChildren(...cols.map((c) => h("div", { class: `vt-cell ${c.num ? "num" : ""}`, title: c.title || null }, c.label)));
    this.spacer.style.height = `${rows.length * this.rh}px`;
    this.selIndex = selected == null ? -1 : rows.indexOf(selected);
    this.render();
  }

  schedule() {
    if (!this.raf) this.raf = requestAnimationFrame(() => { this.raf = 0; this.render(); });
  }

  render() {
    if (!this.rows.length) {
      this.win.style.transform = "";
      this.win.replaceChildren(h("div", { class: "vt-empty" }, this.empty || "Nothing to show."));
      return;
    }
    const top = this.body.scrollTop;
    const height = this.body.clientHeight || 600;
    const first = Math.max(0, Math.floor(top / this.rh) - 8);
    const last = Math.min(this.rows.length, Math.ceil((top + height) / this.rh) + 8);
    this.win.style.transform = `translateY(${first * this.rh}px)`;
    const frag = document.createDocumentFragment();
    for (let i = first; i < last; i++) frag.append(this.renderRow(i));
    this.win.replaceChildren(frag);
    if (this.onTop) this.onTop(this.rows[Math.min(this.rows.length - 1, Math.floor(top / this.rh))]);
  }

  renderRow(i) {
    const row = this.rows[i];
    let cls = "vt-row";
    if (this.rowClass) cls += ` ${this.rowClass(row) || ""}`;
    if (i === this.selIndex) cls += " sel";
    const el = h("div", { class: cls, "data-i": i });
    for (const c of this.cols) {
      const v = c.get(row);
      el.append(h("div", { class: `vt-cell ${c.cls || ""} ${c.num ? "num" : ""}`, title: c.tip ? c.tip(row) : null }, v));
    }
    return el;
  }

  select(i, { scroll = true, quiet = false } = {}) {
    if (i < 0 || i >= this.rows.length) return;
    this.selIndex = i;
    if (scroll) this.scrollTo(i);
    this.render();
    if (!quiet && this.onSelect) this.onSelect(this.rows[i], i);
  }

  scrollTo(i, center = false) {
    const y = i * this.rh;
    const top = this.body.scrollTop;
    const height = this.body.clientHeight;
    if (center || y < top || y + this.rh > top + height) {
      this.body.scrollTop = Math.max(0, y - height / 2 + this.rh / 2);
    }
  }
}

// ------------------------------------------------------------------ tabs

function windowSlice(rows, key = (r) => r.t) {
  if (!S.windowOnly || !zoomed()) return rows;
  return rows.slice(lowerBound(rows, S.view[0], key), lowerBound(rows, S.view[1] + 1, key));
}

function chips(values, isOn, toggle) {
  return h("span", { class: "chips" }, values.map(([value, label]) =>
    h("button", { class: isOn(value) ? "on" : null, title: "click to show/hide · ⌥- or ⌘-click to show only this",
      onclick: (e) => toggle(value, e.altKey || e.metaKey) }, label)));
}

function search(f, placeholder) {
  const input = h("input", { type: "search", placeholder: placeholder || "filter (text, or /regex/)", value: f.q });
  input.addEventListener("input", debounce(() => { f.q = input.value; refresh(); }, 180));
  input.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { input.value = ""; f.q = ""; refresh(); }
    if (e.key === "Enter" || e.key === "ArrowDown") { e.preventDefault(); table.body.focus(); if (table.selIndex < 0) table.select(0); }
  });
  input.dataset.search = "1";
  return input;
}

function selectBox(options, value, onchange) {
  const sel = h("select", { onchange: (e) => onchange(e.target.value) },
    options.map(([v, label]) => h("option", { value: v, selected: v === value ? true : null }, label)));
  return sel;
}

function countBy(rows, key) {
  const m = new Map();
  for (const r of rows) m.set(key(r), (m.get(key(r)) || 0) + 1);
  return [...m.entries()].sort((a, b) => b[1] - a[1]);
}

const TABS = [
  {
    id: "log",
    label: "Log",
    count: () => S.D.lines.t.length,
    cols: () => [
      { label: "time", w: "96px", cls: "mono", get: (i) => fmtClock(S.D.lines.t[i], S.D.lines.o[i], true), tip: (i) => fmtFull(S.D.lines.t[i], S.D.lines.o[i]) },
      { label: "source", w: "70px", get: (i) => srcBadge(S.D.lines.sources[S.D.lines.s[i]]) },
      { label: "level", w: "60px", get: (i) => { const l = S.D.lines.levels[S.D.lines.l[i]]; const r = LEVEL_RANK[l] || 0; return r ? h("span", { class: r > 1 ? "lvl-err" : "lvl-warn" }, l) : ""; } },
      { label: "category", w: "120px", get: (i) => S.D.lines.cats[S.D.lines.c[i]] },
      { label: "message", w: "minmax(0, 1fr)", cls: "msg", get: (i) => (S.D.lines.extra[i] ? `${S.D.lines.m[i]}  ⏎` : S.D.lines.m[i]) },
    ],
    rowClass: (i) => { const r = LEVEL_RANK[S.D.lines.levels[S.D.lines.l[i]]] || 0; return r > 1 ? "err" : r ? "warn" : ""; },
    time: (i) => S.D.lines.t[i],
    line: (i) => i,
    rows() {
      const L = S.D.lines, f = S.f.log;
      const n = L.t.length;
      let i0 = 0, i1 = n;
      if (S.windowOnly && zoomed()) { i0 = lowerBound(L.t, S.view[0]); i1 = lowerBound(L.t, S.view[1] + 1); }
      const srcOn = L.sources.map((s) => !f.src || f.src.has(s));
      const m = matcher(f.q);
      if (m && !S.lower) S.lower = L.m.map((_, i) => lineText(i));
      const out = [];
      for (let i = i0; i < i1; i++) {
        if (!srcOn[L.s[i]]) continue;
        if (f.level && (LEVEL_RANK[L.levels[L.l[i]]] || 0) < f.level) continue;
        if (f.cat >= 0 && L.c[i] !== f.cat) continue;
        if (m && !m(S.lower[i])) continue;
        out.push(i);
      }
      return out;
    },
    toolbar() {
      const L = S.D.lines, f = S.f.log;
      const srcCounts = new Map();
      for (const s of L.s) srcCounts.set(s, (srcCounts.get(s) || 0) + 1);
      const catCounts = new Map();
      for (const c of L.c) catCounts.set(c, (catCounts.get(c) || 0) + 1);
      const cats = [...catCounts.entries()].filter(([c]) => L.cats[c]).sort((a, b) => L.cats[a[0]].localeCompare(L.cats[b[0]]));
      return [
        search(f),
        chips(L.sources.map((s, i) => [s, `${s} ${thousands(srcCounts.get(i) || 0)}`]),
          (s) => !f.src || f.src.has(s),
          (s, solo) => {
            const all = new Set(L.sources);
            const cur = f.src ? new Set(f.src) : all;
            if (solo) f.src = cur.size === 1 && cur.has(s) ? null : new Set([s]);
            else if (cur.has(s)) { cur.delete(s); f.src = cur; }
            else { cur.add(s); f.src = cur.size === all.size ? null : cur; }
            refresh(true);
          }),
        selectBox([["0", "all levels"], ["1", "warnings + errors"], ["2", "errors"]], String(f.level), (v) => { f.level = +v; refresh(); }),
        selectBox([["-1", "all categories"], ...cats.map(([c, n]) => [String(c), `${L.cats[c]} (${thousands(n)})`])], String(f.cat), (v) => { f.cat = +v; refresh(); }),
      ];
    },
    detail: (i) => lineDetail(i),
    empty: "No log lines match.",
  },
  {
    id: "doses",
    label: "Insulin",
    count: () => S.D.doses.length,
    cols: () => [
      { label: "time", w: "96px", cls: "mono", get: (r) => fmtClock(r.t, rowOff(r), true), tip: (r) => fmtFull(r.t, rowOff(r)) },
      { label: "from", w: "60px", get: (r) => r.who, title: "Trio = the app; Driver = TandemKit's log lines; Pump = the request on the wire" },
      { label: "action", w: "150px", get: (r) => r.action },
      { label: "amount", w: "minmax(120px, 1.2fr)", cls: "mono", get: (r) => r.amount },
      { label: "outcome", w: "minmax(120px, 1fr)", get: (r) => h("span", { class: isBad(r.outcome) ? "bad" : "" }, r.outcome) },
      { label: "dose id / ms", w: "minmax(90px, 0.8fr)", cls: "mono", get: (r) => r.raw || (r.ms != null ? `${r.ms} ms` : "") },
    ],
    rowClass: (r) => (isBad(r.outcome) ? "err" : ""),
    rows() {
      const f = S.f.doses, m = matcher(f.q);
      return windowSlice(S.D.doses).filter((r) => f.who.has(r.who) &&
        (!m || m(`${r.who} ${r.action} ${r.amount} ${r.outcome} ${r.raw || ""} ${r.problem || ""}`)));
    },
    toolbar() {
      const f = S.f.doses;
      const counts = new Map(countBy(S.D.doses, (r) => r.who));
      return [search(f), chips(["Trio", "Driver", "Pump"].map((w) => [w, `${w} ${counts.get(w) || 0}`]), (w) => f.who.has(w),
        (w, solo) => {
          if (solo) f.who = new Set([w]);
          else if (f.who.has(w)) f.who.delete(w);
          else f.who.add(w);
          refresh(true);
        })];
    },
    detail: (r) => rowDetail(r, { title: `${r.who}: ${r.action}`, skip: ["t", "line", "rline"] }),
    empty: "No insulin delivery actions in this view.",
  },
  {
    id: "cgm",
    label: "CGM",
    count: () => S.D.glucose.length,
    cols: () => [
      { label: "reading time", w: "96px", cls: "mono", get: (r) => fmtClock(r.t, rowOff(r)), tip: (r) => fmtFull(r.t, rowOff(r), false) },
      { label: "BG", w: "50px", num: true, get: (r) => h("span", { class: bgClass(r.v) === "g-in" ? "" : "bad" }, num(r.v)) },
      { label: "Δ", w: "50px", num: true, get: (r) => (r.delta == null ? "" : (r.delta > 0 ? "+" : "") + num(r.delta)) },
      { label: "kind", w: "80px", get: (r) => r.kind + (r.display_only ? " (display only)" : "") },
      { label: "logged at", w: "96px", cls: "mono", get: (r) => (r.line != null ? fmtClock(S.D.lines.t[r.line], lineOff(r.line)) : "") },
      { label: "", w: "minmax(0, 1fr)", get: () => "" },
    ],
    rows() {
      const m = matcher(S.f.cgm.q);
      return windowSlice(S.D.glucose).filter((r) => !m || m(`${r.v} ${r.kind}`));
    },
    toolbar() {
      const kinds = countBy(S.D.glucose, (r) => r.kind).map(([k, n]) => `${n} ${k}`).join(", ");
      const note = S.D.glucose.length && S.D.glucose[0].kind === "loop"
        ? "No sensor messages this viewer can read (it reads Dexcom G7's); showing the BG each loop started from."
        : "Readings dated by the sensor (live: log time − age; backfill: session start + sensor time).";
      return [search(S.f.cgm, "filter"), h("span", { class: "muted" }, kinds ? `${kinds}. ${note}` : note)];
    },
    detail: (r) => rowDetail(r, { title: `BG ${num(r.v)} (${r.kind})`, skip: ["t", "line"] }),
    empty: "No CGM readings in this view.",
  },
  {
    id: "oref",
    label: "Loop & oref",
    count: () => S.D.determinations.length,
    cols: () => [
      { label: "time", w: "96px", cls: "mono", get: (r) => fmtClock(r.t, rowOff(r), true), tip: (r) => fmtFull(r.t, rowOff(r)) },
      { label: "result", w: "68px", get: (r) => r.kind },
      { label: "BG", w: "40px", num: true, get: (r) => num(r.bg) },
      { label: "IOB", w: "48px", num: true, get: (r) => num(r.iob) },
      { label: "COB", w: "40px", num: true, get: (r) => num(r.cob) },
      { label: "eventual", w: "56px", num: true, get: (r) => num(r.eventual) },
      { label: "temp", w: "86px", num: true, get: (r) => (r.rate != null ? `${num(r.rate)}/${r.duration}m` : "") },
      { label: "SMB", w: "40px", num: true, get: (r) => num(r.smb) },
      { label: "ISF", w: "40px", num: true, get: (r) => num(r.isf) },
      { label: "CR", w: "40px", num: true, get: (r) => num(r.cr) },
      { label: "target", w: "46px", num: true, get: (r) => num(r.target) },
      { label: "sens", w: "40px", num: true, get: (r) => num(r.sens), title: "autosens ratio" },
      { label: "insulinReq", w: "64px", num: true, get: (r) => num(r.insulin_req) },
      { label: "what Trio did", w: "minmax(160px, 1fr)", get: (r) => h("span", { class: isBad(r.outcome) ? "bad" : "" }, r.outcome) },
    ],
    rowClass: (r) => (isBad(r.outcome) ? "err" : ""),
    rows() {
      const f = S.f.oref, m = matcher(f.q);
      return windowSlice(S.D.determinations).filter((r) => (f.kind === "all" || r.kind === f.kind) && (!m || m(`${r.reason} ${r.outcome}`)));
    },
    toolbar() {
      const f = S.f.oref;
      return [search(f, "filter reason / outcome"),
        selectBox([["all", "suggested + enacted"], ["enacted", "enacted"], ["suggested", "suggested"]], f.kind, (v) => { f.kind = v; refresh(); }),
        h("span", { class: "muted" }, "oref's result per loop as Trio logs it, then what Trio did with it")];
    },
    detail: (r) => rowDetail(r, { title: `oref ${r.kind}`, skip: ["t", "line", "reason"], pre: [["reason", r.reason]] }),
    empty: "No oref determinations in this view (Trio logs them from 1.0.2 on).",
  },
  {
    id: "comms",
    label: "Pump comms",
    count: () => S.D.comms.length,
    cols: () => [
      { label: "time", w: "96px", cls: "mono", get: (r) => fmtClock(r.t, rowOff(r), true), tip: (r) => fmtFull(r.t, rowOff(r)) },
      { label: "request", w: "minmax(150px, 1fr)", get: (r) => r.request },
      { label: "outcome", w: "120px", get: (r) => h("span", { class: r.outcome === "response" ? "" : "bad" }, r.outcome) },
      { label: "ms", w: "56px", num: true, get: (r) => (r.ms != null ? thousands(r.ms) : "") },
      { label: "response", w: "minmax(0, 2fr)", cls: "mono", get: (r) => (r.response_fields ? fieldText(r.response_fields) : "") },
    ],
    rowClass: (r) => (r.outcome !== "response" ? "err" : r.ms > 5000 ? "warn" : ""),
    rows() {
      const f = S.f.comms, m = matcher(f.q);
      return windowSlice(S.D.comms).filter((r) => (!f.req || r.request === f.req) &&
        (f.outcome === "all" || (f.outcome === "problems" ? r.outcome !== "response" : r.outcome === f.outcome)) &&
        (!f.minMs || (r.ms || 0) >= f.minMs) &&
        (!m || m(`${r.request} ${fieldText(r.fields)} ${r.response || ""} ${r.response_fields ? fieldText(r.response_fields) : ""}`)));
    },
    toolbar() {
      const f = S.f.comms;
      const minMs = h("input", { type: "number", min: "0", step: "100", value: f.minMs || "", placeholder: "0" });
      minMs.addEventListener("input", debounce(() => { f.minMs = +minMs.value || 0; refresh(); }, 250));
      return [search(f),
        selectBox([["", "all requests"], ...countBy(S.D.comms, (r) => r.request).map(([k, n]) => [k, `${k} (${thousands(n)})`])], f.req, (v) => { f.req = v; refresh(); }),
        selectBox([["all", "all outcomes"], ["problems", "no response"], ...countBy(S.D.comms, (r) => r.outcome).map(([k, n]) => [k, `${k} (${thousands(n)})`])], f.outcome, (v) => { f.outcome = v; refresh(); }),
        h("label", null, "≥", minMs, "ms")];
    },
    detail: (r) => rowDetail(r, { title: r.request, skip: ["t", "line", "rline", "fields", "response_fields"],
      kvs: [["request fields", r.fields], ["response fields", r.response_fields]], extraLine: r.rline, extraLabel: "Outcome line" }),
    empty: "No pump requests in this view.",
  },
  {
    id: "history",
    label: "Pump history",
    count: () => (S.history && S.history.records ? S.history.records.length : S.D.history_packets ? "…" : 0),
    cols: () => [
      { label: "seq", w: "76px", num: true, cls: "mono", get: (r) => (r.seq == null ? "" : String(r.seq)) },
      { label: "pump time", w: "140px", cls: "mono", get: (r) => r.pump_time || "", title: "the pump's own clock" },
      { label: "read at", w: "96px", cls: "mono", get: (r) => fmtClock(r.t, rowOff(r)), tip: (r) => fmtFull(r.t, rowOff(r)) },
      { label: "type", w: "200px", get: (r) => r.type },
      { label: "fields", w: "minmax(0, 1fr)", cls: "mono", get: (r) => fieldText(r.fields) },
      { label: "reads", w: "40px", num: true, get: (r) => (r.reads > 1 ? String(r.reads) : "") },
    ],
    time: (r) => r.t,
    unsorted: true,
    rows() {
      if (!S.history || !S.history.records) return [];
      const f = S.f.history, m = matcher(f.q);
      return S.history.records.filter((r) => inWindow(r.t) && (!f.type || r.type === f.type) &&
        (!m || m(`${r.type} ${fieldText(r.fields)} ${r.seq}`)));
    },
    toolbar() {
      const f = S.f.history, hs = S.history;
      if (!hs || !hs.records) {
        return [h("span", { class: "spinner" }), h("span", { class: "muted" }, hs ? hs.message : "loading…")];
      }
      return [search(f),
        selectBox([["", "all types"], ...countBy(hs.records, (r) => r.type).map(([k, n]) => [k, `${k} (${thousands(n)})`])], f.type, (v) => { f.type = v; refresh(); }),
        h("span", { class: hs.decoded ? "muted" : "note warn" }, `${thousands(hs.packets)} stream packets · ${hs.message || ""}`)];
    },
    detail: (r) => rowDetail(r, { title: `${r.type} #${r.seq}`, skip: ["t", "line", "fields", "cargo"], kvs: [["fields", r.fields]], pre: [["record bytes", r.cargo]] }),
    empty: "No pump history records in this view.",
  },
  {
    id: "findings",
    label: "Findings",
    count: () => S.D.findings.length,
    cols: () => [
      { label: "time", w: "96px", cls: "mono", get: (r) => fmtClock(r.t, rowOff(r)), tip: (r) => fmtFull(r.t, rowOff(r)) },
      { label: "severity", w: "60px", get: (r) => sevBadge(r.severity) },
      { label: "check", w: "220px", get: (r) => r.check },
      { label: "summary", w: "minmax(0, 1fr)", get: (r) => r.summary },
    ],
    rows() {
      const f = S.f.findings, m = matcher(f.q);
      return windowSlice(S.D.findings).filter((r) => (f.sev === "all" || r.severity === f.sev) && (!f.check || r.check === f.check) &&
        (!m || m(`${r.check} ${r.summary}`)));
    },
    toolbar() {
      const f = S.f.findings;
      return [search(f),
        selectBox([["all", "all severities"], ["high", "high"], ["warn", "warn"], ["info", "info"]], f.sev, (v) => { f.sev = v; refresh(); }),
        selectBox([["", "all checks"], ...countBy(S.D.findings, (r) => r.check).map(([k, n]) => [k, `${k} (${n})`])], f.check, (v) => { f.check = v; refresh(); })];
    },
    detail: (r) => rowDetail(r, { title: r.check, skip: ["t", "line", "data", "summary", "check"], pre: [["summary", r.summary]], kvs: [["data", r.data]] }),
    empty: "No findings in this view.",
  },
  {
    id: "info",
    label: "Export & builds",
    count: () => S.D.headers.length || "",
    page: () => infoPage(),
  },
];
const TAB = Object.fromEntries(TABS.map((t) => [t.id, t]));

function fieldText(fields) {
  if (!fields) return "";
  return Object.entries(fields).map(([k, v]) => `${k}=${typeof v === "object" ? JSON.stringify(v) : v}`).join(", ");
}

// ------------------------------------------------------------------ detail panel

function copyButton(label, text) {
  const b = h("button", { onclick: async () => {
    try { await navigator.clipboard.writeText(text); b.textContent = "copied"; } catch { b.textContent = "copy failed"; }
    setTimeout(() => { b.textContent = label; }, 1200);
  } }, label);
  return b;
}

function showCommand(i) {
  const L = S.D.lines;
  return `${S.D.files[L.f[i]].ref} ${L.n[i]}`;
}

function context(i, radius = 12) {
  const L = S.D.lines;
  const box = h("div", { class: "ctx" });
  for (let j = Math.max(0, i - radius); j <= Math.min(L.t.length - 1, i + radius); j++) {
    box.append(h("div", { class: j === i ? "here" : null, title: "show in Log", onclick: () => showInLog(j) },
      h("span", null, fmtClock(L.t[j], L.o[j], true)), h("span", null, srcBadge(L.sources[L.s[j]])),
      h("span", null, L.m[j])));
  }
  return box;
}

function kvTable(obj) {
  const t = h("table", { class: "kv" });
  for (const [k, v] of Object.entries(obj || {})) {
    if (v == null || v === "") continue;
    t.append(h("tr", null, h("td", null, k), h("td", null, typeof v === "object" ? JSON.stringify(v) : String(v))));
  }
  return t;
}

function lineMeta(i) {
  const L = S.D.lines;
  const level = L.levels[L.l[i]];
  return h("div", { class: "meta" }, srcBadge(L.sources[L.s[i]]),
    h("span", { class: "mono" }, `${fmtFull(L.t[i], L.o[i])} ${fmtOff(L.o[i])}`),
    level && level !== "INFO" ? h("span", { class: (LEVEL_RANK[level] || 0) > 1 ? "lvl-err" : (LEVEL_RANK[level] ? "lvl-warn" : "") }, level) : null,
    L.cats[L.c[i]] ? h("span", null, `[${L.cats[L.c[i]]}]`) : null,
    h("span", { class: "muted" }, `${S.D.files[L.f[i]].name}:${L.n[i]}`));
}

function eventsOf(i) {
  if (!S.eventsByLine) {
    S.eventsByLine = new Map();
    for (const [line, kind, data] of S.D.events) {
      if (!S.eventsByLine.has(line)) S.eventsByLine.set(line, []);
      S.eventsByLine.get(line).push([kind, data]);
    }
  }
  return S.eventsByLine.get(i) || [];
}

function lineDetail(i) {
  const evs = eventsOf(i);
  return [
    h("h3", null, "Log line"),
    lineMeta(i),
    h("pre", null, lineText(i)),
    h("div", { class: "actions" }, copyButton("copy line", lineText(i)), copyButton("copy `trio-logs show` command", showCommand(i))),
    evs.length ? [h("h3", null, "Parsed as"), evs.map(([kind, data]) => [h("div", { class: "meta" }, h("strong", null, kind)), kvTable(data)])] : null,
    h("h3", null, "Around it"),
    context(i),
  ];
}

function rowDetail(r, { title, skip = [], kvs = [], pre = [], extraLine = null, extraLabel = "" }) {
  const own = Object.fromEntries(Object.entries(r).filter(([k]) => !skip.includes(k)));
  const out = [h("h3", null, title)];
  out.push(h("div", { class: "meta" }, h("span", { class: "mono" }, `${fmtFull(r.t, rowOff(r))} ${fmtOff(rowOff(r))}`)));
  for (const [label, text] of pre) if (text) out.push(h("div", { class: "muted" }, label), h("pre", null, text));
  out.push(kvTable(own));
  for (const [label, obj] of kvs) if (obj && Object.keys(obj).length) out.push(h("h3", null, label), kvTable(obj));
  if (r.line != null) {
    out.push(h("h3", null, "Log line"), lineMeta(r.line), h("pre", null, lineText(r.line)),
      h("div", { class: "actions" }, h("button", { onclick: () => showInLog(r.line) }, "show in Log"),
        copyButton("copy `trio-logs show` command", showCommand(r.line))),
      context(r.line, 8));
  }
  if (extraLine != null) {
    out.push(h("h3", null, extraLabel), lineMeta(extraLine), h("pre", null, lineText(extraLine)),
      h("div", { class: "actions" }, h("button", { onclick: () => showInLog(extraLine) }, "show in Log")));
  }
  return out;
}

function infoPage() {
  const D = S.D;
  const out = [h("h2", null, "Files"), h("table", null,
    h("tr", null, h("th", null, "file"), h("th", null, "line references")),
    D.files.map((f) => h("tr", null, h("td", null, f.name), h("td", { class: "mono" }, `${f.ref} <line>`))))];
  out.push(h("h2", null, "Builds"));
  out.push(D.builds.length ? h("table", null, D.builds.map((b) => h("tr", null, h("td", { class: "mono" }, fmtFull(b.t, D.tz, false)), h("td", null, b.label))))
    : h("p", { class: "muted" }, "No build information (no launch line or export header)."));
  out.push(h("h2", null, D.headers.length > 1 ? "Export headers" : "Export header"));
  if (!D.headers.length) out.push(h("p", { class: "muted" }, "No export header for these days (older log files, or headers not organized)."));
  for (const text of D.headers) out.push(h("pre", null, text));
  return out;
}

// ------------------------------------------------------------------ table + tabs wiring

let table;
let refreshing = false;

function renderTabs() {
  const nav = $("#tabs");
  nav.replaceChildren(...TABS.map((t) => h("button", {
    role: "tab", "aria-selected": String(S.tab === t.id), onclick: () => setTab(t.id),
  }, t.label, h("span", { class: "n" }, S.D ? (typeof t.count() === "number" ? thousands(t.count()) : t.count()) : ""))));
}

function setTab(id) {
  S.tab = id;
  if (id === "history" && !S.history) loadHistory();
  writeHash();
  renderTabs();
  refresh(true);
  const tab = TAB[id];
  const row = S.sel[id];
  if (!tab.page && row != null && table.selIndex >= 0) {
    $("#detail").replaceChildren(...[tab.detail(row)].flat(Infinity).filter(Boolean));
    table.scrollTo(table.selIndex, true);
  } else {
    $("#detail").replaceChildren(h("p", { class: "muted" }, tab.page ? "" : "Select a row to see its details and the log lines around it."));
    if (!tab.page) {
      const i = S.cursor != null ? indexAt(S.cursor) : -1;
      if (i >= 0) table.scrollTo(i, true);
      else table.body.scrollTop = 0;
    }
  }
  table.render();
}

function refresh(toolbarToo = true) {
  if (!S.D || refreshing) return;
  refreshing = true;
  try {
    const tab = TAB[S.tab];
    const tb = $("#toolbar");
    if (tab.page) {
      $("#table").hidden = true;
      $("#page").hidden = false;
      $("#page").replaceChildren(...[tab.page()].flat(Infinity).filter(Boolean));
      tb.replaceChildren(h("span", { class: "muted" }, "What the export says about the phone, pump, CGM and settings when it was made."));
      return;
    }
    $("#table").hidden = false;
    $("#page").hidden = true;
    const rows = tab.rows();
    const focused = document.activeElement && document.activeElement.dataset && document.activeElement.dataset.search;
    if (toolbarToo || !tb.childElementCount) {
      const win = h("label", { title: "Show only rows inside the chart's time window" },
        h("input", { type: "checkbox", checked: S.windowOnly ? true : null, onchange: (e) => { S.windowOnly = e.target.checked; refresh(); } }),
        "chart window only");
      tb.replaceChildren(...[tab.toolbar()].flat(Infinity).filter(Boolean), h("span", { class: "spacer" }), win,
        h("span", { class: "count", id: "count" }), h("span", { class: "count mono", id: "topday", title: "day of the top row" }));
      if (focused) { const s = tb.querySelector("[data-search]"); if (s) { s.focus(); s.setSelectionRange(s.value.length, s.value.length); } }
    }
    const total = tab.count();
    $("#count").textContent = `${thousands(rows.length)}${typeof total === "number" && total !== rows.length ? ` of ${thousands(total)}` : ""}`;
    table.rowClass = tab.rowClass || null;
    table.onTop = tab.unsorted ? null : (row) => updateTopDay(tab, row);
    table.set(tab.cols(), rows, S.sel[tab.id], tab.empty);
    if (table.selIndex >= 0) table.scrollTo(table.selIndex);
  } finally {
    refreshing = false;
  }
}

function updateTopDay(tab, row) {
  if (row == null) return;
  const t = tab.time ? tab.time(row) : row.t;
  const off = tab.id === "log" ? S.D.lines.o[row] : rowOff(row);
  const el = $("#topday");
  if (el) el.textContent = `${fmtWeekday(t, off)} ${fmtDay(t, off)}`;
}

function rowTime(tab, row) {
  return tab.time ? tab.time(row) : row.t;
}

function onSelect(row) {
  const tab = TAB[S.tab];
  S.sel[tab.id] = row;
  $("#detail").replaceChildren(...[tab.detail(row)].flat(Infinity).filter(Boolean));
  $("#detail").scrollTop = 0;
  setCursor(rowTime(tab, row), true);
}

function indexAt(t) {
  // the current table's row at (or nearest to) time t, or -1
  const tab = TAB[S.tab];
  if (tab.page || !table.rows.length) return -1;
  if (!tab.unsorted) return Math.min(table.rows.length - 1, lowerBound(table.rows, t, (r) => rowTime(tab, r)));
  let best = Infinity, i = -1;
  table.rows.forEach((r, k) => { const d = Math.abs(rowTime(tab, r) - t); if (d < best) { best = d; i = k; } });
  return i;
}

function jumpToTime(t) {
  const i = indexAt(t);
  if (i < 0) return;
  table.select(i, { scroll: false });
  table.scrollTo(i, true);
  table.render();
}

function showInLog(i) {
  const f = S.f.log;
  if (S.tab !== "log") { S.tab = "log"; renderTabs(); writeHash(); }
  let rows = TAB.log.rows();
  if (!rows.includes(i)) {
    f.q = ""; f.src = null; f.level = 0; f.cat = -1;
    if (!inWindow(S.D.lines.t[i])) S.windowOnly = false;
    rows = TAB.log.rows();
  }
  S.sel.log = i;
  refresh(true);
  const k = table.rows.indexOf(i);
  if (k >= 0) { table.select(k, { scroll: false }); table.scrollTo(k, true); table.render(); }
}

// ------------------------------------------------------------------ chart

const ZOOMS = [["All", 0], ["24h", 24], ["12h", 12], ["6h", 6], ["3h", 3], ["1h", 1], ["15m", 0.25]];
const TICK_STEPS = [5e3, 1e4, 15e3, 3e4, 6e4, 12e4, 3e5, 6e5, 9e5, 18e5, 36e5, 72e5, 108e5, 216e5, 432e5, 864e5, 1728e5];
const MIN_SPAN = 30e3;

const chart = {
  el: null,
  drag: null,
  hoverX: null,

  init() {
    this.el = $("#chart");
    $("#zoom-btns").replaceChildren(...ZOOMS.map(([label, hours]) => h("button", { onclick: () => {
      if (!hours) return setView(S.full);
      const span = hours * 36e5;
      const c = S.cursor != null && S.cursor >= S.view[0] && S.cursor <= S.view[1] ? S.cursor : (S.view[0] + S.view[1]) / 2;
      setView([c - span / 2, c + span / 2]);
    } }, label)));
    $("#pan-left").onclick = () => pan(-0.5);
    $("#pan-right").onclick = () => pan(0.5);
    $("#zoom-out").onclick = () => zoomAround((S.view[0] + S.view[1]) / 2, 2);
    this.el.addEventListener("mousedown", (e) => {
      if (e.button !== 0 || !S.D) return;
      e.preventDefault();
      this.drag = { x0: this.localX(e), moved: false, y: e.offsetY };
    });
    window.addEventListener("mousemove", (e) => {
      if (!this.drag) return;
      const x = this.localX(e);
      if (Math.abs(x - this.drag.x0) > 4) this.drag.moved = true;
      if (this.drag.moved) {
        const b = $("#brush"), g = this.geom();
        const a = Math.max(g.L, Math.min(this.drag.x0, x)), z = Math.min(g.W - g.R, Math.max(this.drag.x0, x));
        Object.assign(b.style, { left: `${a}px`, width: `${z - a}px`, top: `${this.el.offsetTop + g.top}px`, height: `${g.bottom - g.top}px` });
        b.hidden = false;
      }
    });
    window.addEventListener("mouseup", (e) => {
      if (!this.drag) return;
      const d = this.drag;
      this.drag = null;
      $("#brush").hidden = true;
      const x = this.localX(e);
      if (d.moved) {
        const a = this.tAt(Math.min(d.x0, x)), z = this.tAt(Math.max(d.x0, x));
        setView([a, Math.max(z, a + MIN_SPAN)]);
      } else {
        this.click(x, d.y);
      }
    });
    this.el.addEventListener("mousemove", (e) => this.hover(e));
    this.el.addEventListener("mouseleave", () => { $("#tip").hidden = true; this.hoverX = null; this.drawHover(); });
    this.el.addEventListener("dblclick", () => setView(S.full));
    this.el.addEventListener("wheel", (e) => {
      if (!S.D) return;
      e.preventDefault();
      if (Math.abs(e.deltaX) > Math.abs(e.deltaY)) {
        pan(e.deltaX / Math.max(200, this.el.clientWidth));
      } else {
        zoomAround(this.tAt(this.localX(e)), e.deltaY > 0 ? 1.25 : 0.8);
      }
    }, { passive: false });
    new ResizeObserver(() => this.render()).observe(this.el);
  },

  localX(e) {
    return e.clientX - this.el.getBoundingClientRect().left;
  },

  geom() {
    const W = this.el.clientWidth || 800, H = this.el.clientHeight || 300;
    const L = 52, R = 46, top = 6, axis = 18, gap = 8, evRow = 11;
    const ev = evRow * 3;
    const avail = H - top - axis - ev - gap * 3;
    const g = Math.round(avail * 0.5), ins = Math.round(avail * 0.27), iob = avail - g - ins;
    const lanes = {};
    let y = top;
    lanes.g = [y, y + g]; y += g + gap;
    lanes.ins = [y, y + ins]; y += ins + gap;
    lanes.iob = [y, y + iob]; y += iob + gap;
    lanes.ev = [y, y + ev]; y += ev;
    return { W, H, L, R, top, bottom: y, lanes, evRow };
  },

  tAt(x) {
    const g = this.geom();
    const [t0, t1] = S.view;
    return t0 + ((x - g.L) / (g.W - g.L - g.R)) * (t1 - t0);
  },

  render() {
    const D = S.D;
    if (!D) return;
    const g = this.geom();
    const { W, H, L, R, lanes } = g;
    const [t0, t1] = S.view;
    const k = (W - L - R) / (t1 - t0);
    const X = (t) => L + (t - t0) * k;
    const margin = (t1 - t0) * 0.02;
    const vis = (arr, key = (r) => r.t) => arr.slice(Math.max(0, lowerBound(arr, t0 - margin - 36e5, key)), lowerBound(arr, t1 + margin, key));
    const f1 = (v) => v.toFixed(1);
    const out = [];
    const off = D.tz;

    out.push(`<defs><clipPath id="plot"><rect x="${L}" y="0" width="${W - L - R}" height="${H}"/></clipPath></defs>`);
    for (const [, [a, b]] of Object.entries(lanes)) out.push(`<rect class="lane-bg" x="${L}" y="${a}" width="${W - L - R}" height="${b - a}" opacity="0.5"/>`);

    // time grid
    const span = t1 - t0;
    const step = TICK_STEPS.find((s) => span / s <= (W - L - R) / 90) || TICK_STEPS[TICK_STEPS.length - 1];
    const offMs = off * 60000;
    for (let tt = Math.ceil((t0 + offMs) / step) * step - offMs; tt <= t1; tt += step) {
      const x = f1(X(tt));
      const midnight = (tt + offMs) % 864e5 === 0;
      out.push(`<line class="grid" x1="${x}" x2="${x}" y1="${g.top}" y2="${g.bottom}"${midnight ? ' style="stroke-width:1.6"' : ""}/>`);
      const label = midnight || step >= 864e5 ? fmtDay(tt, off).slice(5) : step < 6e4 ? fmtClock(tt, off) : fmtClock(tt, off).slice(0, 5);
      out.push(`<text class="axis-t${midnight ? " day" : ""}" x="${x}" y="${H - 5}" text-anchor="middle">${label}</text>`);
    }

    out.push(`<g clip-path="url(#plot)">`);
    // builds
    for (const b of D.builds) if (b.t >= t0 && b.t <= t1) out.push(`<line class="build" x1="${f1(X(b.t))}" x2="${f1(X(b.t))}" y1="${g.top}" y2="${g.bottom}"/>`);

    // glucose
    const [ga, gb] = lanes.g;
    const [gLo, gHi] = S.gRange;
    const Yg = (v) => gb - ((Math.max(gLo, Math.min(gHi, v)) - gLo) / (gHi - gLo)) * (gb - ga);
    const [lowT, highT] = S.mmol ? [3.9, 10] : [70, 180];
    out.push(`<rect class="band" x="${L}" y="${f1(Yg(highT))}" width="${W - L - R}" height="${f1(Yg(lowT) - Yg(highT))}"/>`);
    out.push(`<line class="thr" x1="${L}" x2="${W - R}" y1="${f1(Yg(lowT))}" y2="${f1(Yg(lowT))}"/><line class="thr" x1="${L}" x2="${W - R}" y1="${f1(Yg(highT))}" y2="${f1(Yg(highT))}"/>`);
    const gv = vis(D.glucose);
    const r = gv.length > 1500 ? 1.4 : gv.length > 600 ? 2 : 2.6;
    for (const p of gv) {
      const cls = bgClass(p.v);
      out.push(p.kind === "backfill"
        ? `<circle class="bf ${cls}" cx="${f1(X(p.t))}" cy="${f1(Yg(p.v))}" r="${r}"/>`
        : `<circle class="${cls}" cx="${f1(X(p.t))}" cy="${f1(Yg(p.v))}" r="${r}"/>`);
    }

    // insulin: suspended, temp basals, boluses
    const [ia, ib] = lanes.ins;
    const Yb = (v) => ib - (v / S.basalMax) * (ib - ia - 4);
    for (const [a, b] of S.suspended) {
      if (b < t0 || a > t1) continue;
      out.push(`<rect class="susp" x="${f1(X(a))}" y="${ia}" width="${f1(Math.max(1, X(b) - X(a)))}" height="${ib - ia}"/>`);
    }
    const bv = vis(D.basal);
    const top = [];
    for (let n = 0; n < bv.length; n++) {
      const b = bv[n];
      const next = D.basal[lowerBound(D.basal, b.t + 1, (x) => x.t)];
      const end = Math.min(b.t + b.minutes * 60000, next ? next.t : Infinity);
      const xa = X(b.t), xb = X(end);
      out.push(`<rect class="basal" x="${f1(xa)}" y="${f1(Yb(b.rate))}" width="${f1(Math.max(0.5, xb - xa))}" height="${f1(ib - Yb(b.rate))}"/>`);
      top.push(`M${f1(xa)},${f1(Yb(b.rate))}H${f1(xb)}`);
    }
    if (top.length) out.push(`<path class="basal-top" d="${top.join("")}"/>`);
    const Ybo = (u) => ib - (u / S.bolusMax) * (ib - ia - 4);
    for (const b of vis(S.boluses)) {
      const x = X(b.t);
      if (b.ok) out.push(`<rect class="${b.smb ? "smb" : "bolus"}" x="${f1(x - 1.5)}" y="${f1(Ybo(b.units))}" width="3" height="${f1(ib - Ybo(b.units))}"/>`);
      else out.push(`<path class="fail" d="M${f1(x - 4)},${ia + 2}l8,8M${f1(x + 4)},${ia + 2}l-8,8"/>`);
    }
    for (const b of vis(S.failedTemps)) {
      const x = X(b.t);
      out.push(`<path class="fail" d="M${f1(x - 4)},${ia + 2}l8,8M${f1(x + 4)},${ia + 2}l-8,8"/>`);
    }

    // IOB / COB
    const [oa, ob] = lanes.iob;
    const [iLo, iHi] = S.iobRange;
    const Yi = (v) => ob - ((v - iLo) / (iHi - iLo)) * (ob - oa - 2);
    const Yc = (v) => ob - (v / S.cobMax) * (ob - oa - 2);
    if (iLo < 0) out.push(`<line class="zero" x1="${L}" x2="${W - R}" y1="${f1(Yi(0))}" y2="${f1(Yi(0))}"/>`);
    const dv = vis(D.determinations);
    out.push(`<path class="iob" d="${polyline(dv, (d) => d.iob, X, Yi)}"/>`);
    out.push(`<path class="cob" d="${polyline(dv, (d) => d.cob, X, Yc)}"/>`);
    out.push(`<path class="piob" d="${polyline(vis(D.pump_iob), (p) => p.v, X, Yi)}"/>`);

    // event rows: loop, pump, findings
    const [ea] = lanes.ev;
    const row = (n) => [ea + n * g.evRow + 1, ea + (n + 1) * g.evRow - 1];
    const [la, lb] = row(0), [pa, pb] = row(1), [fa, fb] = row(2);
    const mv = vis(D.markers);
    for (const m of mv) {
      const x = f1(X(m.t));
      if (m.lane === "loop") {
        out.push(m.kind === "loop_failed" ? `<line class="m-fail" x1="${x}" x2="${x}" y1="${la}" y2="${lb}"/>` : `<line class="m-ok" x1="${x}" x2="${x}" y1="${la + 4}" y2="${lb}"/>`);
      } else if (m.lane === "app") {
        out.push(`<path class="m-app" d="M${x},${pb}l-4,-8h8z"/>`);
      } else {
        const cls = m.kind === "pump_clock_adjusted" ? "m-clock" : m.kind === "timeout" ? "m-warn" : "m-pump";
        out.push(`<line class="${cls}" x1="${x}" x2="${x}" y1="${pa}" y2="${pb}"/>`);
      }
    }
    for (const f of vis(D.findings)) {
      const x = X(f.t), y = (fa + fb) / 2;
      out.push(`<path class="sev-${f.severity}" d="M${f1(x)},${f1(y - 4.5)}l4.5,4.5l-4.5,4.5l-4.5,-4.5z"/>`);
    }

    // cursor
    if (S.cursor != null && S.cursor >= t0 && S.cursor <= t1) {
      out.push(`<line class="cursor" x1="${f1(X(S.cursor))}" x2="${f1(X(S.cursor))}" y1="${g.top}" y2="${g.bottom}"/>`);
    }
    out.push(`</g><line id="hoverline" class="hoverline" x1="-10" x2="-10" y1="${g.top}" y2="${g.bottom}"/>`);

    // axes and lane labels
    const ticksG = S.mmol ? [3.9, 10, 15] : [lowT, highT, 250, 300, 400].filter((v) => v < gHi);
    for (const v of ticksG) out.push(`<text class="axis-t" x="${L - 4}" y="${f1(Yg(v) + 3)}" text-anchor="end">${v}</text>`);
    out.push(`<text class="lane-l" x="4" y="${ga + 10}">BG</text>`);
    out.push(`<text class="lane-l" x="4" y="${ia + 10}">basal</text><text class="axis-t" x="4" y="${ia + 22}">${num(S.basalMax)} U/h</text>`);
    out.push(`<text class="lane-l" x="${W - R + 4}" y="${ia + 10}">bolus</text><text class="axis-t" x="${W - R + 4}" y="${ia + 22}">${num(S.bolusMax)} U</text>`);
    out.push(`<text class="lane-l" x="4" y="${oa + 10}">IOB</text><text class="axis-t" x="4" y="${oa + 22}">${num(iHi)} U</text>`);
    if (iLo < 0) out.push(`<text class="axis-t" x="${L - 4}" y="${f1(Yi(0) + 3)}" text-anchor="end">0</text>`);
    out.push(`<text class="lane-l" x="${W - R + 4}" y="${oa + 10}">COB</text><text class="axis-t" x="${W - R + 4}" y="${oa + 22}">${num(S.cobMax)} g</text>`);
    out.push(`<text class="lane-l" x="4" y="${lb}">loop</text><text class="lane-l" x="4" y="${pb}">pump</text><text class="lane-l" x="4" y="${fb}">findings</text>`);

    this.el.innerHTML = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${W} ${H}" width="${W}" height="${H}">${out.join("")}</svg>`;
    this.drawHover();
    updateViewLabel();
  },

  drawHover() {
    const line = this.el.querySelector("#hoverline");
    if (!line) return;
    const x = this.hoverX == null ? -10 : this.hoverX;
    line.setAttribute("x1", x);
    line.setAttribute("x2", x);
  },

  near(x, y) {
    // markers / findings within a few px of x, when the pointer is in their row
    const g = this.geom();
    const [ea, eb] = g.lanes.ev;
    if (y < ea - 2 || y > eb + 2) return [];
    const rowN = Math.max(0, Math.min(2, Math.floor((y - ea) / g.evRow)));
    const tol = (6 / (g.W - g.L - g.R)) * (S.view[1] - S.view[0]);
    const t = this.tAt(x);
    const pick = (arr) => arr.slice(lowerBound(arr, t - tol, (r) => r.t), lowerBound(arr, t + tol, (r) => r.t));
    if (rowN === 2) return pick(S.D.findings).map((f) => ({ kind: "finding", item: f }));
    const lane = rowN === 0 ? ["loop"] : ["pump", "app"];
    return pick(S.D.markers).filter((m) => lane.includes(m.lane)).map((m) => ({ kind: "marker", item: m }));
  },

  hover(e) {
    if (!S.D || this.drag) return;
    const x = this.localX(e), y = e.offsetY;
    const g = this.geom();
    const tip = $("#tip");
    if (x < g.L || x > g.W - g.R) { tip.hidden = true; this.hoverX = null; this.drawHover(); return; }
    this.hoverX = x;
    this.drawHover();
    const t = this.tAt(x), off = S.D.tz;
    const lines = [h("div", { class: "t" }, `${fmtWeekday(t, off)} ${fmtFull(t, off, false)}`)];
    const gl = nearest(S.D.glucose, t, 6 * 60000);
    if (gl) lines.push(h("div", null, `BG ${num(gl.v)}${gl.kind === "backfill" ? " (backfill)" : ""} at ${fmtClock(gl.t, off).slice(0, 5)}`));
    const b = activeBasal(t);
    if (b) lines.push(h("div", null, `temp basal ${num(b.rate)} U/h (set ${fmtClock(b.t, off).slice(0, 5)} for ${b.minutes} min)`));
    if (S.suspended.some(([a, z]) => t >= a && t <= z)) lines.push(h("div", { class: "bad" }, "pump suspended"));
    const d = before(S.D.determinations, t, 15 * 60000);
    if (d) lines.push(h("div", null, `oref ${fmtClock(d.t, off).slice(0, 5)}: IOB ${num(d.iob)} U · COB ${num(d.cob)} g · eventual ${num(d.eventual)}${d.rate != null ? ` · temp ${num(d.rate)}/${d.duration}m` : ""}${d.smb ? ` · SMB ${num(d.smb)} U` : ""}`));
    const pi = before(S.D.pump_iob, t, 15 * 60000);
    if (pi) lines.push(h("div", null, `pump IOB ${num(pi.v)} U at ${fmtClock(pi.t, off).slice(0, 5)} (the pump's own calculation)`));
    for (const bo of S.boluses) if (Math.abs(bo.t - t) < (6 / (g.W - g.L - g.R)) * (S.view[1] - S.view[0])) {
      lines.push(h("div", { class: bo.ok ? "" : "bad" }, `${bo.label} ${num(bo.units)} U${bo.ok ? "" : " failed"} at ${fmtClock(bo.t, off)}`));
    }
    for (const n of this.near(x, y).slice(0, 6)) {
      const it = n.item;
      if (n.kind === "finding") lines.push(h("div", { class: it.severity === "info" ? "" : "bad" }, `◆ ${it.check}: ${it.summary}`));
      else lines.push(h("div", null, `${fmtClock(it.t, off)} ${it.label}${it.detail ? `: ${it.detail}` : ""}`));
    }
    tip.replaceChildren(...lines);
    tip.hidden = false;
    const wrap = $("#chart-wrap").getBoundingClientRect();
    const left = e.clientX - wrap.left + 14;
    tip.style.left = `${Math.min(left, wrap.width - tip.offsetWidth - 8)}px`;
    tip.style.top = `${e.clientY - wrap.top + 14}px`;
  },

  click(x, y) {
    if (!S.D) return;
    const hit = this.near(x, y)[0];
    if (hit && hit.kind === "finding") {
      S.tab = "findings"; renderTabs(); writeHash();
      S.sel.findings = hit.item;
      refresh(true);
      const k = table.rows.indexOf(hit.item);
      if (k >= 0) { table.select(k, { scroll: false }); table.scrollTo(k, true); table.render(); }
      return;
    }
    if (hit && hit.item.line != null) return showInLog(hit.item.line);
    const t = this.tAt(x);
    setCursor(t, false);
    jumpToTime(t);
  },
};

function polyline(rows, get, X, Y) {
  let d = "", prev = null;
  for (const r of rows) {
    const v = get(r);
    if (v == null || typeof v !== "number") { prev = null; continue; }
    d += `${prev == null || r.t - prev > 30 * 60000 ? "M" : "L"}${X(r.t).toFixed(1)},${Y(v).toFixed(1)}`;
    prev = r.t;
  }
  return d || "M0,0";
}

function bgClass(v) {
  const [lo, hi] = S.mmol ? [3.9, 10] : [70, 180];
  return v < lo ? "g-low" : v > hi ? "g-high" : "g-in";
}

function nearest(arr, t, tol) {
  const i = lowerBound(arr, t, (r) => r.t);
  let best = null;
  for (const r of [arr[i - 1], arr[i]]) if (r && Math.abs(r.t - t) <= tol && (!best || Math.abs(r.t - t) < Math.abs(best.t - t))) best = r;
  return best;
}

function before(arr, t, tol) {
  const i = lowerBound(arr, t + 1, (r) => r.t) - 1;
  return i >= 0 && t - arr[i].t <= tol ? arr[i] : null;
}

function activeBasal(t) {
  const B = S.D.basal;
  const i = lowerBound(B, t + 1, (r) => r.t) - 1;
  if (i < 0) return null;
  const b = B[i];
  return t <= b.t + b.minutes * 60000 ? b : null;
}

function setView([a, b]) {
  const [f0, f1] = S.full;
  let span = Math.max(MIN_SPAN, b - a);
  if (span >= f1 - f0) { a = f0; b = f1; } else {
    if (a < f0) { a = f0; b = a + span; }
    if (b > f1) { b = f1; a = b - span; }
  }
  S.view = [a, b];
  chart.render();
  refreshWindowed();
}

function pan(frac) {
  const span = S.view[1] - S.view[0];
  setView([S.view[0] + span * frac, S.view[1] + span * frac]);
}

function zoomAround(t, factor) {
  const [a, b] = S.view;
  setView([t - (t - a) * factor, t + (b - t) * factor]);
}

function setCursor(t, panToIt) {
  S.cursor = t;
  if (panToIt && (t < S.view[0] || t > S.view[1])) {
    const span = S.view[1] - S.view[0];
    S.view = [t - span / 2, t + span / 2];
    setView(S.view);
  } else {
    chart.render();
  }
}

const refreshWindowed = debounce(() => {
  if (S.windowOnly && !TAB[S.tab].page) refresh(false);
}, 120);

function updateViewLabel() {
  if (!S.D) return;
  const off = S.D.tz;
  const el = $("#view-label");
  const [a, b] = S.view;
  const same = fmtDay(a, off) === fmtDay(b, off);
  el.textContent = `${fmtDay(a, off)} ${fmtClock(a, off).slice(0, 5)} → ${same ? "" : `${fmtDay(b, off)} `}${fmtClock(b, off).slice(0, 5)} (${fmtSpan(b - a)}) · UTC${fmtOff(off)}`;
}

// ------------------------------------------------------------------ loading

async function getJSON(url) {
  const r = await fetch(url, { cache: "no-store" });
  const body = await r.json().catch(() => ({ error: `HTTP ${r.status}` }));
  if (!r.ok || body.error) throw new Error(body.error || `HTTP ${r.status}`);
  return body;
}

function busy(text) {
  $("#loading").hidden = !text;
  if (text) $("#loading-text").textContent = text;
}

function prepare(D) {
  S.D = D;
  S.lower = null;
  S.eventsByLine = null;
  S.history = null;
  S.sel = {};
  const ts = D.lines.t, G = D.glucose;
  const ends = [ts[0], ts[ts.length - 1], G.length ? G[0].t : null, G.length ? G[G.length - 1].t : null].filter((t) => t != null);
  S.full = ends.length ? [Math.min(...ends), Math.max(...ends)] : [0, 1];
  for (let i = 1; i < G.length; i++) if (G[i].t - G[i - 1].t <= 15 * 60000) G[i].delta = +(G[i].v - G[i - 1].v).toFixed(1);
  if (S.full[1] - S.full[0] < MIN_SPAN) S.full[1] = S.full[0] + MIN_SPAN;
  S.view = [...S.full];
  S.cursor = null;
  const gmax = D.glucose.reduce((m, g) => Math.max(m, g.v), 0);
  S.mmol = D.glucose.length > 0 && gmax < 35;
  S.gRange = S.mmol ? [2, Math.max(15, Math.ceil(gmax + 1))] : [40, Math.max(300, Math.ceil((gmax + 20) / 50) * 50)];
  S.basalMax = Math.max(1, ...D.basal.map((b) => b.rate));
  S.boluses = D.doses.filter((r) => r.who === "Trio" && (r.action === "SMB" || r.action.startsWith("bolus (") || (r.action === "bolus")))
    .map((r) => ({ t: r.t, units: r.units || 0, ok: r.ok !== false, smb: r.action === "SMB", label: r.action, line: r.line }));
  S.bolusMax = Math.max(0.5, ...S.boluses.map((b) => b.units));
  S.failedTemps = D.doses.filter((r) => r.who === "Trio" && r.action === "temp basal" && r.ok === false);
  const iobs = [...D.determinations.map((d) => d.iob), ...D.pump_iob.map((p) => p.v)].filter((v) => typeof v === "number");
  S.iobRange = [Math.min(0, ...iobs), Math.max(1, ...iobs)];
  S.cobMax = Math.max(20, ...D.determinations.map((d) => (typeof d.cob === "number" ? d.cob : 0)));
  S.suspended = [];
  let start = null;
  for (const p of D.pump_state) {
    if (p.suspended && start == null) start = p.t;
    else if (!p.suspended && start != null) { S.suspended.push([start, p.t]); start = null; }
  }
  if (start != null) S.suspended.push([start, S.full[1]]);
}

async function load(key) {
  busy("Loading and parsing logs…");
  try {
    const D = await getJSON(`/api/view?key=${encodeURIComponent(key)}`);
    S.key = key;
    prepare(D);
    renderSummary();
    $("#detail").replaceChildren(h("p", { class: "muted" }, "Select a row to see the full log line, what was parsed from it, and the lines around it."));
    renderTabs();
    chart.render();
    if (S.tab === "history") loadHistory();
    refresh(true);
    writeHash();
  } catch (e) {
    $("#detail").replaceChildren(h("div", { class: "note warn" }, `Could not load: ${e.message}`));
  } finally {
    busy(null);
  }
}

async function loadHistory() {
  if (!S.key) return;
  const key = S.key;
  try {
    const r = await getJSON(`/api/history?key=${encodeURIComponent(key)}`);
    if (key !== S.key) return;
    if (r.status === "ready") S.history = r;
    else if (r.status === "error") S.history = { message: `decoding failed: ${r.message}`, records: [], packets: 0 };
    else { S.history = { message: r.message }; setTimeout(loadHistory, 1500); }
  } catch (e) {
    S.history = { message: `decoding failed: ${e.message}`, records: [], packets: 0 };
  }
  renderTabs();
  if (S.tab === "history") refresh(true);
}

function renderSummary() {
  const D = S.D;
  const days = D.days.length > 1 ? `${D.days[0]} → ${D.days[D.days.length - 1]}` : D.days[0] || "";
  const build = D.builds.length ? D.builds[D.builds.length - 1].label : "";
  $("#summary").textContent = [D.person, days, `${thousands(D.lines.t.length)} lines`, build].filter(Boolean).join(" · ");
  const badge = $("#mask-badge");
  badge.className = D.masked ? "masked" : "unmasked";
  badge.textContent = D.masked ? "identifiers masked" : "UNMASKED";
  badge.title = D.masked ? "Pump serials, transmitter ids and emails are masked as `trio-logs show` masks them. Still health data." : "Started with --unmasked: text is shown as logged.";
}

// ------------------------------------------------------------------ source picker & URL state

function readHash() {
  const p = new URLSearchParams(location.hash.slice(1));
  return { key: p.get("key"), tab: p.get("tab") };
}

function writeHash() {
  const p = new URLSearchParams();
  if (S.key) p.set("key", S.key);
  p.set("tab", S.tab);
  history.replaceState(null, "", `#${p}`);
}

function personKey() {
  return `${$("#source").value}/${$("#day").value}/${$("#ndays").value}`;
}

function fillDays(personId, day) {
  const person = S.catalog.people.find((p) => p.id === personId);
  const daySel = $("#day");
  daySel.replaceChildren(...person.days.map((d) => h("option", { value: d.day }, d.kind === "old" ? `${d.day} (older logs)` : d.day)));
  daySel.value = day && person.days.some((d) => d.day === day) ? day : person.days[person.days.length - 1].day;
}

async function init() {
  table = new VTable($("#table"));
  table.onSelect = onSelect;
  chart.init();
  initSplitter();
  document.addEventListener("keydown", (e) => {
    const typing = e.target.matches("input, select, textarea");
    if (e.key === "/" && !typing) {
      const s = $("#toolbar [data-search]");
      if (s) { e.preventDefault(); s.focus(); s.select(); }
    } else if (!typing && !e.target.closest(".vt-body") && (e.key === "ArrowLeft" || e.key === "ArrowRight")) {
      pan(e.key === "ArrowLeft" ? -0.25 : 0.25);
    }
  });
  try {
    S.catalog = await getJSON("/api/catalog");
  } catch (e) {
    $("#detail").replaceChildren(h("div", { class: "note warn" }, `Could not reach the server: ${e.message}`));
    return;
  }
  const C = S.catalog;
  const src = $("#source");
  const groups = [];
  if (C.files.length) groups.push(h("optgroup", { label: "Export files" }, C.files.map((f) => h("option", { value: f.id }, f.label))));
  if (C.people.length) groups.push(h("optgroup", { label: "Testers" }, C.people.map((p) => h("option", { value: p.id }, `${p.label} (${p.days.length} days)`))));
  src.replaceChildren(...groups);
  $("#ndays").replaceChildren(...Array.from({ length: C.max_days }, (_, i) => h("option", { value: String(i + 1) }, i ? `${i + 1} days` : "1 day")));

  const { key, tab } = readHash();
  if (tab && TAB[tab]) S.tab = tab;
  let first = C.files.length ? C.files[0].id : null;
  if (key && C.files.some((f) => f.id === key)) first = key;
  const pm = key && /^(p\d+)\/(\d{4}-\d{2}-\d{2})\/(\d+)$/.exec(key);
  if (pm && C.people.some((p) => p.id === pm[1])) {
    src.value = pm[1];
    fillDays(pm[1], pm[2]);
    $("#ndays").value = pm[3];
    first = null;
  } else if (first) {
    src.value = first;
  } else if (C.people.length) {
    src.value = C.people[0].id;
    fillDays(C.people[0].id);
  }
  const onSource = () => {
    const v = src.value;
    const isPerson = v.startsWith("p");
    $("#day-pick").hidden = !isPerson;
    if (isPerson) {
      if (!$("#day").options.length || !S.key || !S.key.startsWith(`${v}/`)) fillDays(v);
      load(personKey());
    } else {
      load(v);
    }
  };
  src.addEventListener("change", () => { $("#day").replaceChildren(); onSource(); });
  $("#day").addEventListener("change", () => load(personKey()));
  $("#ndays").addEventListener("change", () => load(personKey()));
  $("#day-pick").hidden = !src.value.startsWith("p");
  renderTabs();
  if (!src.value) {
    $("#detail").replaceChildren(h("div", { class: "note" }, "Nothing to show: no export files or organized days."));
    return;
  }
  load(src.value.startsWith("p") ? personKey() : src.value);
}

function initSplitter() {
  const sp = $("#splitter"), detail = $("#detail");
  let start = null;
  sp.addEventListener("mousedown", (e) => { start = { x: e.clientX, w: detail.offsetWidth }; sp.classList.add("drag"); e.preventDefault(); });
  window.addEventListener("mousemove", (e) => {
    if (!start) return;
    const w = Math.max(260, Math.min(window.innerWidth - 320, start.w - (e.clientX - start.x)));
    detail.style.width = `${w}px`;
  });
  window.addEventListener("mouseup", () => { if (start) { start = null; sp.classList.remove("drag"); } });
}

init();

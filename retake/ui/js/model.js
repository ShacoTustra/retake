// The edit decision list: what is cut out of the source. No DOM here, so it can be tested in node.
//
// The source is never changed. An edit is a span of the source with `on` = "cut this out";
// suggestions are edits that start with whatever the rules and the LLM proposed, manual edits
// come from the reviewer. Everything the timeline shows (clips, the result, its length) is derived.

export const KINDS = {
  retake: { name: "Дубль", color: "--k-retake" },
  aside: { name: "Не по уроку", color: "--k-aside" },
  stutter: { name: "Запинка", color: "--k-stutter" },
  filler: { name: "Мычание", color: "--k-filler" },
  pause: { name: "Пауза", color: "--k-pause" },
  manual: { name: "Вручную", color: "--k-manual" },
};

export const isPause = e => e.kind === "pause";
export const isMinor = e => e.kind === "stutter" || e.kind === "filler";
export const isMain = e => !isPause(e) && !isMinor(e);
export const isAiFocus = e => !!e.llm && (!!e.disputed || e.src === "llm");

const MIN_CUT = 0.05;       // seconds; shorter edits make no sense and are hard to grab
const FIRST_MANUAL_ID = 1_000_000;  // above the ids of the rules and of the LLM
const HOP = 0.01;           // the loudness envelope has one value per 10 ms

const clamp = (x, a, b) => Math.min(b, Math.max(a, x));
const ms = x => Math.round(x * 1000) / 1000;

// ---------------------------------------------------------------- formatting

export function fmtTC(t) {  // 1:02:03.4 or 2:03.4
  const n = Math.round(Math.max(0, t) * 10);
  const h = Math.floor(n / 36000), m = Math.floor(n % 36000 / 600), s = Math.floor(n % 600 / 10), d = n % 10;
  const ss = String(s).padStart(2, "0");
  return (h ? `${h}:${String(m).padStart(2, "0")}` : `${m}`) + `:${ss}.${d}`;
}

export function fmtClock(t) {  // 1:02:03 or 2:03
  const n = Math.round(Math.max(0, t));
  const h = Math.floor(n / 3600), m = Math.floor(n % 3600 / 60), s = n % 60;
  return (h ? `${h}:${String(m).padStart(2, "0")}` : `${m}`) + `:${String(s).padStart(2, "0")}`;
}

export function fmtDur(t) {  // 4.2 с, 3 мин 20 с
  t = Math.max(0, t);
  if (t < 60) return `${t.toFixed(1)} с`;
  const n = Math.round(t), h = Math.floor(n / 3600), m = Math.floor(n % 3600 / 60), s = n % 60;
  return (h ? `${h} ч ` : "") + `${m} мин` + (s && !h ? ` ${s} с` : "");
}

// ---------------------------------------------------------------- waveform lookups

/** Max and mean of a byte array over any range, without walking it: two pyramids. */
export class Peaks {
  constructor(values) {
    this.maxes = [values];
    this.means = [values];
    let hi = values, av = values;
    while (hi.length > 1 && this.maxes.length < 16) {
      const n = hi.length >> 1, nh = new Uint8Array(n), na = new Uint8Array(n);
      for (let i = 0; i < n; i++) {
        nh[i] = Math.max(hi[2 * i], hi[2 * i + 1]);
        na[i] = (av[2 * i] + av[2 * i + 1]) >> 1;
      }
      this.maxes.push(nh);
      this.means.push(na);
      hi = nh;
      av = na;
    }
  }

  /** [max, mean] of frames i0..i1. */
  range(i0, i1) {
    i0 = Math.max(0, Math.floor(i0));
    i1 = Math.max(i0 + 1, Math.ceil(i1));
    const k = Math.min(this.maxes.length - 1, Math.floor(Math.log2(i1 - i0)));
    const H = this.maxes[k], A = this.means[k];
    const a = i0 >> k, b = Math.min(H.length, Math.max(a + 1, Math.ceil(i1 / (1 << k))));
    let m = 0, sum = 0;
    for (let i = a; i < b; i++) {
      if (H[i] > m) m = H[i];
      sum += A[i];
    }
    return [m, b > a ? sum / (b - a) : 0];
  }
}

/** Decibels (as bytes, -80..0 dB) to the height of the wave, 0..1. Room noise stays a thin line. */
export const waveHeight = v => Math.pow(Math.max(0, (v / 255 - 0.31) / 0.69), 1.25);

// ---------------------------------------------------------------- the model

export class Model {
  constructor(data, env = null) {
    [this.T0, this.T1] = data.range;
    this.fps = data.fps || 25;
    this.words = data.words || [];
    this.phrases = data.phrases || [];
    this.env = env;
    this.peaks = env ? new Peaks(env) : null;
    this.byId = new Map();
    this.edits = (data.edits || []).map(e => {
      const x = {
        ...e, default_on: e.default_on ?? e.on, orig_start: e.orig_start ?? e.start, orig_end: e.orig_end ?? e.end,
        reviewed: !!e.reviewed,
      };
      this.byId.set(x.id, x);
      return x;
    });
    this.splits = [...(data.splits || [])].sort((a, b) => a - b);
    this.undoStack = [];
    this.redoStack = [];
    this.listeners = new Set();
    this._gesture = null;
    this._recalc();
  }

  setEnv(env) {
    this.env = env;
    this.peaks = env ? new Peaks(env) : null;
    this._notify("live");
  }

  // ---- change notification: "live" while dragging, "commit" for finished changes
  subscribe(fn) { this.listeners.add(fn); return () => this.listeners.delete(fn); }
  _notify(kind) { for (const fn of this.listeners) fn(kind); }

  // ---- derived: merged cuts ("groups") and the kept segments between them
  _recalc() {
    const eps = 2 / this.fps;  // the renderer drops kept slivers shorter than 2 frames
    const on = this.edits.filter(e => e.on && e.end > e.start).sort((x, y) => x.start - y.start);
    const groups = [];
    for (const e of on) {
      const g = groups[groups.length - 1];
      if (g && e.start <= g.b + eps) {
        if (e.end > g.b) { g.b = e.end; g.edgeB = e; }
        g.edits.push(e);
      } else {
        groups.push({ a: e.start, b: e.end, edits: [e], edgeA: e, edgeB: e });
      }
    }
    for (const g of groups) {
      // what the cut is called on the timeline: the longest edit that is not just a pause
      const named = g.edits.filter(e => !isPause(e));
      g.main = (named.length ? named : g.edits).reduce((p, e) => (e.end - e.start > p.end - p.start ? e : p));
    }
    const keep = [];
    let pos = this.T0, out = 0;
    for (const g of groups) {
      if (g.a > pos + eps) {
        keep.push({ a: pos, b: g.a, o: out });
        out += g.a - pos;
      }
      pos = Math.max(pos, g.b);
    }
    if (this.T1 > pos + eps) {
      keep.push({ a: pos, b: this.T1, o: out });
      out += this.T1 - pos;
    }
    this.groups = groups;
    this.keep = keep;
    this.outDuration = out;
  }

  get duration() { return this.T1 - this.T0; }

  stats() {
    const total = this.duration;
    return { total, out: this.outDuration, cut: total - this.outDuration, splices: this.groups.length };
  }

  // first group that ends after t (it may start before or after t)
  groupFrom(t) {
    const G = this.groups;
    let lo = 0, hi = G.length;
    while (lo < hi) {
      const m = (lo + hi) >> 1;
      if (G[m].b <= t) lo = m + 1; else hi = m;
    }
    return G[lo] || null;
  }

  groupAt(t) {
    const g = this.groupFrom(t);
    return g && g.a <= t ? g : null;
  }

  // ---- source time <-> time in the finished video
  srcToOut(t) {
    const K = this.keep;
    let lo = 0, hi = K.length - 1, seg = null;
    while (lo <= hi) {
      const m = (lo + hi) >> 1;
      if (K[m].a <= t) { seg = K[m]; lo = m + 1; } else hi = m - 1;
    }
    if (!seg) return 0;
    return seg.o + Math.min(t, seg.b) - seg.a;
  }

  outToSrc(o) {
    const K = this.keep;
    if (!K.length) return this.T0;
    let lo = 0, hi = K.length - 1, seg = K[0];
    while (lo <= hi) {
      const m = (lo + hi) >> 1;
      if (K[m].o <= o) { seg = K[m]; lo = m + 1; } else hi = m - 1;
    }
    return Math.min(seg.b, seg.a + Math.max(0, o - seg.o));
  }

  /** The kept piece around t, narrowed by the blade marks. */
  clipAt(t) {
    const seg = this.keep.find(k => k.a <= t && t < k.b);
    if (!seg) return null;
    let a = seg.a, b = seg.b;
    for (const s of this.splits) {
      if (s <= t && s > a) a = s;
      if (s > t && s < b) b = s;
    }
    return { a, b };
  }

  /** Where the next/previous edit point is, for the arrow keys. */
  editPoint(t, dir) {
    const pts = [this.T0, this.T1, ...this.splits];
    for (const g of this.groups) pts.push(g.a, g.b);
    pts.sort((a, b) => a - b);
    const eps = 0.5 / this.fps;
    if (dir > 0) return pts.find(p => p > t + eps) ?? this.T1;
    for (let i = pts.length - 1; i >= 0; i--) if (pts[i] < t - eps) return pts[i];
    return this.T0;
  }

  // ---- text
  wordIndexAt(t) {  // last word that starts at or before t, -1 if none
    const W = this.words;
    let lo = 0, hi = W.length - 1, r = -1;
    while (lo <= hi) {
      const m = (lo + hi) >> 1;
      if (W[m].s <= t) { r = m; lo = m + 1; } else hi = m - 1;
    }
    return r;
  }

  wordsBetween(a, b) {  // [i0, i1): words whose middle is inside the span
    const W = this.words;
    let i0 = Math.max(0, this.wordIndexAt(a));
    while (i0 < W.length && (W[i0].s + W[i0].e) / 2 < a) i0++;
    let i1 = i0;
    while (i1 < W.length && (W[i1].s + W[i1].e) / 2 <= b) i1++;
    return [i0, i1];
  }

  textBetween(a, b) {
    const [i0, i1] = this.wordsBetween(a, b);
    return this.words.slice(i0, i1).map(w => w.t).join("").trim();
  }

  textAfter(t, n = 8) {
    const i0 = this.wordIndexAt(t - 0.01) + 1;
    return this.words.slice(i0, i0 + n).map(w => w.t).join("").trim() + (i0 + n < this.words.length ? "…" : "");
  }

  textBefore(t, n = 6) {
    const i1 = this.wordIndexAt(t - 0.01) + 1;
    const i0 = Math.max(0, i1 - n);
    return (i0 > 0 ? "…" : "") + this.words.slice(i0, i1).map(w => w.t).join("").trim();
  }

  // ---- where a cut point should land
  /** The quietest moment near t: a splice there does not click. */
  quietNear(t, radius) {
    if (!this.env) return t;
    const i0 = Math.max(0, Math.floor((t - radius - this.T0) / HOP));
    const i1 = Math.min(this.env.length - 1, Math.ceil((t + radius - this.T0) / HOP));
    const mid = (t - this.T0) / HOP;
    let best = -1, bestV = 1e9, bestD = 1e9;
    for (let i = i0; i <= i1; i++) {
      const v = this.env[i], d = Math.abs(i - mid);
      if (v < bestV || (v === bestV && d < bestD)) { best = i; bestV = v; bestD = d; }
    }
    return best < 0 ? t : ms(this.T0 + best * HOP);
  }

  /** A cut point in the gap after word k (k = -1 is the gap before the first word).
   * In a long pause it stays close to the speech it borders, so a word's tail or the breath
   * before the next word survive: side "start" sits after the left word, "end" before the right one. */
  gapPoint(k, side) {
    const W = this.words;
    const l = k >= 0 ? W[k].e : this.T0;
    const r = k + 1 < W.length ? W[k + 1].s : this.T1;
    const w = Math.max(0, r - l);
    let target = (l + r) / 2;
    if (w > 0.4) target = side === "start" ? l + 0.15 : side === "end" ? r - 0.15 : target;
    return this.quietNear(target, Math.min(0.05, w / 2));
  }

  /** Nearest thing worth sticking to within radius seconds; null if nothing is close. */
  snap(t, radius, { skip = [], extra = [], side = "" } = {}) {
    let best = null, bd = radius;
    const consider = (x, label) => {
      const d = Math.abs(x - t);
      if (d < bd && !skip.some(s => Math.abs(s - x) < 1e-6)) { bd = d; best = { t: x, label }; }
    };
    consider(this.T0, "начало");
    consider(this.T1, "конец");
    for (const g of this.groups) { consider(g.a, "край правки"); consider(g.b, "край правки"); }
    for (const s of this.splits) consider(s, "разрез");
    for (const x of extra) consider(x.t, x.label);
    const i = this.wordIndexAt(t);
    for (let k = Math.max(-1, i - 3); k <= Math.min(this.words.length - 1, i + 2); k++) {
      consider(this.gapPoint(k, side), "между словами");
    }
    return best;
  }

  // ---- undo
  _snapshot() {
    return { e: this.edits.map(x => [x.id, x.start, x.end, x.on, x.reviewed]), s: [...this.splits] };
  }

  _restore(st) {
    this.edits = st.e.map(([id, start, end, on, reviewed]) => {
      const x = this.byId.get(id);
      x.start = start; x.end = end; x.on = on; x.reviewed = reviewed;
      return x;
    });
    this.splits = [...st.s];
    this._recalc();
  }

  /** Every finished change goes through here: one undo step. */
  commit(label, fn) {
    const before = this._gesture ?? this._snapshot();
    this._gesture = null;
    const r = fn ? fn() : undefined;
    this.undoStack.push({ label, state: before });
    if (this.undoStack.length > 500) this.undoStack.shift();
    this.redoStack.length = 0;
    this._recalc();
    this._notify("commit");
    return r;
  }

  /** Dragging: changes come through live() and become one undo step at commit(). */
  beginGesture() { this._gesture = this._snapshot(); }
  live() { this._recalc(); this._notify("live"); }

  cancelGesture() {
    if (!this._gesture) return;
    this._restore(this._gesture);
    this._gesture = null;
    this._notify("commit");
  }

  undo() {
    const x = this.undoStack.pop();
    if (!x) return null;
    this.redoStack.push({ label: x.label, state: this._snapshot() });
    this._restore(x.state);
    this._notify("commit");
    return x.label;
  }

  redo() {
    const x = this.redoStack.pop();
    if (!x) return null;
    this.undoStack.push({ label: x.label, state: this._snapshot() });
    this._restore(x.state);
    this._notify("commit");
    return x.label;
  }

  // ---- changes
  _newManual(a, b) {
    let id = FIRST_MANUAL_ID;
    for (const k of this.byId.keys()) if (k >= id) id = k + 1;
    const e = {
      id, kind: "manual", start: ms(a), end: ms(b), on: true, default_on: true, reviewed: true,
      orig_start: ms(a), orig_end: ms(b), removed: this.textBetween(a, b),
    };
    this.byId.set(id, e);
    this.edits.push(e);
    return e;
  }

  /** Cut out a span chosen by the reviewer. */
  addCut(a, b) {
    a = clamp(a, this.T0, this.T1);
    b = clamp(b, this.T0, this.T1);
    if (b - a < MIN_CUT) return null;
    const g = this.groupAt(a);
    if (g && g.b >= b) return null;  // all of it is already cut
    return this.commit("вырезать", () => this._newManual(a, b));
  }

  /** Decide on a suggestion. A manual cut that gets switched off simply disappears. */
  setOn(e, on) {
    this.commit(on ? "вырезать" : "оставить", () => {
      if (e.kind === "manual" && !on) this.edits = this.edits.filter(x => x !== e);
      else { e.on = on; e.reviewed = true; }
    });
  }

  markReviewed(e) {
    if (e.reviewed) return;
    this.commit("отметить проверенным", () => { e.reviewed = true; });
  }

  /** Set the state of many edits at once. */
  setMany(list, on, label) {
    this.commit(label, () => {
      for (const e of list) { e.on = on; e.reviewed = true; }
    });
  }

  /** Move one end of an edit (not an undo step by itself: see commit/beginGesture). */
  setEdge(e, side, t) {
    if (side === "start") e.start = ms(clamp(t, this.T0, e.end - MIN_CUT));
    else e.end = ms(clamp(t, e.start + MIN_CUT, this.T1));
  }

  moveEdge(e, side, t, label = "граница правки") {
    this.commit(label, () => { this.setEdge(e, side, t); e.reviewed = true; });
  }

  resetEdit(e) {
    this.commit("вернуть как было", () => {
      e.start = e.orig_start; e.end = e.orig_end; e.on = e.default_on; e.reviewed = false;
    });
  }

  resetAll() {
    this.commit("вернуть всё как было", () => {
      this.edits = this.edits.filter(e => e.kind !== "manual");
      for (const e of this.edits) {
        e.start = e.orig_start; e.end = e.orig_end; e.on = e.default_on; e.reviewed = false;
      }
      this.splits = [];
    });
  }

  /** Put material back: everything cut inside [a, b] returns, the rest of those cuts stays. */
  restoreRange(a, b) {
    this.commit("вернуть", () => {
      for (const e of [...this.edits]) {
        if (!e.on || e.end <= a || e.start >= b) continue;
        e.reviewed = true;
        if (a <= e.start && e.end <= b) {
          if (e.kind === "manual") this.edits = this.edits.filter(x => x !== e);
          else e.on = false;
        } else if (a <= e.start) {
          e.start = ms(b);
        } else if (e.end <= b) {
          e.end = ms(a);
        } else {
          const end = e.end;
          e.end = ms(a);
          this._newManual(b, end);
        }
      }
    });
  }

  /** The blade: a mark on the kept material, or a cut split in two so one part can be returned. */
  blade(t) {
    const inside = this.edits.filter(e => e.on && e.start + MIN_CUT < t && t < e.end - MIN_CUT);
    if (inside.length) {
      this.commit("разрезать правку", () => {
        for (const e of inside) {
          const end = e.end;
          e.end = ms(t);
          this._newManual(t, end);
        }
      });
      return "cut";
    }
    const near = this.splits.findIndex(s => Math.abs(s - t) < 1 / this.fps);
    this.commit(near >= 0 ? "убрать разрез" : "разрезать", () => {
      if (near >= 0) this.splits.splice(near, 1);
      else { this.splits.push(ms(t)); this.splits.sort((a, b) => a - b); }
    });
    return near >= 0 ? "removed" : "split";
  }

  // ---- what the server stores
  decisions() {
    const overrides = {}, manual = [];
    for (const e of this.edits) {
      if (e.kind === "manual") {
        manual.push({ id: e.id, start: e.start, end: e.end, on: e.on, removed: e.removed });
      } else if (e.on !== e.default_on || e.start !== e.orig_start || e.end !== e.orig_end || e.reviewed) {
        overrides[e.id] = {
          on: e.on, start: e.start, end: e.end, orig_start: e.orig_start, orig_end: e.orig_end, reviewed: e.reviewed,
        };
      }
    }
    return { overrides, manual, splits: this.splits };
  }
}

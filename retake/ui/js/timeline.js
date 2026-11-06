// The timeline: lanes for suggestions, text, picture and sound, drawn on a canvas.
//
// Two views of the same data. "source": the whole recording with the cuts shaded. "result": cuts
// collapsed, this is the finished video. All drawing goes through x(t) and srcAt(x), so both views
// share the code and every gesture works in both.
//
// There are no tool modes: what a drag does depends on where it starts (like Resolve's cut page).
// Ruler: move the playhead. Edge of a cut: trim it. Anywhere else: select a range.

import { KINDS, fmtClock, fmtDur, isPause, waveHeight } from "./model.js";

const RULER_H = 24;
const AI_H = 34;
const TEXT_H = 26;
const VIDEO_H = 52;
const LANE_GAP = 2;
const EDGE_PX = 6;       // how close to an edge counts as grabbing it
const SNAP_PX = 10;
const MAX_PPS = 500;     // pixels per second at the closest zoom: 2 px per 10 ms of sound
const WORD_PPS = 80;     // from this zoom on single words get their own box

const clamp = (x, a, b) => Math.min(b, Math.max(a, x));
const cssVar = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();

export class Timeline {
  constructor(app) {
    this.app = app;
    this.m = app.model;
    this.stage = document.querySelector("#tlStage");
    this.cv = document.querySelector("#tlCanvas");
    this.ctx = this.cv.getContext("2d", { alpha: false });
    this.ph = document.querySelector("#playhead");
    this.hoverEl = document.querySelector("#hoverLine");
    this.hoverThumb = document.querySelector("#hoverThumb");
    this.heads = document.querySelector("#tlHeads");
    this.ov = document.querySelector("#ovCanvas");
    this.ovCtx = this.ov.getContext("2d", { alpha: false });
    this.ovHead = document.querySelector("#ovHead");
    this.mode = "source";
    this.pps = 1;
    this.fit = true;
    this.scroll = 0;       // seconds of display time left of the visible part
    this.W = 0;
    this.H = 0;
    this.dpr = 1;
    this.dirty = true;
    this.ovDirty = true;
    this.drag = null;
    this.snapMark = null;
    this.hover = null;
    this.chips = [];
    this.thumbs = null;
    this.playheadT = this.m.T0;
    this.font = getComputedStyle(document.body).fontFamily;
    this.c = {};
    for (const k of ["bg", "panel", "panel-2", "panel-3", "line", "line-2", "ink", "ink-2", "muted", "faint", "accent",
      "cut", "sel", "ai", "wave", "wave-dim", "wave-cut", "wave-cut-2", "lane"]) this.c[k] = cssVar("--" + k);
    this.kind = {};
    for (const [k, v] of Object.entries(KINDS)) this.kind[k] = cssVar(v.color);
    this.hatch = this._makeHatch();
    new ResizeObserver(() => this.resize()).observe(this.stage);
    new ResizeObserver(() => this.resize()).observe(this.ov);
    this._bindPointer();
    this._bindOverview();
    this.resize();
    const loop = () => {
      if (this.dirty) { this.dirty = false; this.draw(); }
      if (this.ovDirty) { this.ovDirty = false; this.drawOverview(); }
      requestAnimationFrame(loop);
    };
    requestAnimationFrame(loop);
  }

  // ---------------------------------------------------------------- coordinates
  get duration() { return this.mode === "source" ? this.m.duration : this.m.outDuration; }
  srcToD(t) { return this.mode === "source" ? t - this.m.T0 : this.m.srcToOut(t); }
  dToSrc(d) { return this.mode === "source" ? this.m.T0 + d : this.m.outToSrc(d); }
  x(t) { return (this.srcToD(t) - this.scroll) * this.pps; }
  srcAt(x) { return this.dToSrc(this.scroll + x / this.pps); }
  get minPps() { return this.W / Math.max(1, this.duration); }

  invalidate() { this.dirty = true; this.ovDirty = true; }

  resize() {
    const r = this.stage.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    if (r.width < 2 || r.height < 2) return;
    this.W = r.width;
    this.H = r.height;
    this.dpr = dpr;
    this.cv.width = Math.round(r.width * dpr);
    this.cv.height = Math.round(r.height * dpr);
    const o = this.ov.getBoundingClientRect();
    this.ov.width = Math.round(o.width * dpr);
    this.ov.height = Math.round(o.height * dpr);
    this._layout();
    if (this.fit) this.pps = this.minPps;
    this._clampScroll();
    this.invalidate();
    this.setPlayhead(this.playheadT);
    this.app.onZoom?.();
  }

  _layout() {
    const fixed = RULER_H + AI_H + TEXT_H + 3 * LANE_GAP;
    const left = Math.max(60, this.H - fixed);
    const video = Math.min(VIDEO_H, Math.round(left * 0.45));
    const audio = left - video - LANE_GAP;
    let y = 0;
    const lane = h => { const l = { y, h }; y += h + LANE_GAP; return l; };
    this.lanes = { ruler: lane(RULER_H), ai: lane(AI_H), text: lane(TEXT_H), video: lane(video), audio: lane(audio) };
    for (const [k, l] of Object.entries(this.lanes)) {
      const el = this.heads.querySelector(`[data-lane="${k}"]`);
      if (el) { el.style.top = l.y + "px"; el.style.height = l.h + "px"; }
    }
  }

  _clampScroll() {
    const span = this.W / this.pps;
    this.scroll = clamp(this.scroll, 0, Math.max(0, this.duration - span));
  }

  // ---------------------------------------------------------------- zoom, scroll, view
  setZoom(pps, anchorX = this.W / 2) {
    const d = this.scroll + anchorX / this.pps;
    this.pps = clamp(pps, this.minPps, MAX_PPS);
    this.fit = this.pps <= this.minPps * 1.001;
    this.scroll = d - anchorX / this.pps;
    this._clampScroll();
    this.invalidate();
    this.setPlayhead(this.playheadT);
    this.app.onZoom?.();
  }

  zoomBy(f, anchorX) { this.setZoom(this.pps * f, anchorX ?? clamp(this.x(this.playheadT), 0, this.W)); }

  zoomFit() {
    this.fit = true;
    this.pps = this.minPps;
    this.scroll = 0;
    this.invalidate();
    this.setPlayhead(this.playheadT);
    this.app.onZoom?.();
  }

  /** 0..1 on a log scale, for the slider. */
  get zoomPos() { return Math.log(this.pps / this.minPps) / Math.log(MAX_PPS / this.minPps); }
  set zoomPos(v) {
    const x = clamp(this.x(this.playheadT), 0, this.W);
    this.setZoom(this.minPps * Math.pow(MAX_PPS / this.minPps, v), x);
  }

  zoomToSpan(a, b) {
    const da = this.srcToD(a), db = this.srcToD(b);
    const span = Math.max(0.5, db - da);
    this.pps = clamp(this.W * 0.7 / span, this.minPps, MAX_PPS);
    this.fit = this.pps <= this.minPps * 1.001;
    this.scroll = (da + db) / 2 - this.W / this.pps / 2;
    this._clampScroll();
    this.invalidate();
    this.setPlayhead(this.playheadT);
    this.app.onZoom?.();
  }

  setMode(mode) {
    if (mode === this.mode) return;
    const px = this.x(this.playheadT);
    this.mode = mode;
    if (this.fit) { this.pps = this.minPps; this.scroll = 0; }
    else { this.pps = clamp(this.pps, this.minPps, MAX_PPS); this.scroll = this.srcToD(this.playheadT) - px / this.pps; }
    this._clampScroll();
    this.invalidate();
    this.setPlayhead(this.playheadT);
    this.app.onZoom?.();
  }

  /** Keep a span in view without needless jumps. */
  reveal(a, b = a) {
    const x0 = this.x(a), x1 = this.x(b);
    if (x0 >= 40 && x1 <= this.W - 40) return;
    this.scroll = (this.srcToD(a) + this.srcToD(b)) / 2 - this.W / this.pps / 2;
    this._clampScroll();
    this.invalidate();
    this.setPlayhead(this.playheadT);
  }

  setPlayhead(t, { follow = false } = {}) {
    this.playheadT = t;
    let x = this.x(t);
    if (follow && !this.drag && this.W && !this.fit) {
      if (x > this.W * 0.92 || x < 0) {  // turn the page, the way NLEs do
        this.scroll = this.srcToD(t) - (this.W * 0.08) / this.pps;
        this._clampScroll();
        this.invalidate();
        x = this.x(t);
      }
    }
    const vis = x >= -1 && x <= this.W + 1;
    this.ph.style.display = vis ? "" : "none";
    if (vis) this.ph.style.transform = `translateX(${x}px)`;
    const d = this.srcToD(t) / Math.max(1, this.duration);
    this.ovHead.style.transform = `translateX(${d * this.ov.clientWidth}px)`;
  }

  setThumbs(meta, url) {
    const img = new Image();
    img.onload = () => { this.thumbs = { ...meta, img }; this.invalidate(); };
    img.src = url;
    this.thumbUrl = url;
    this.thumbMeta = meta;
  }

  // ---------------------------------------------------------------- drawing
  _makeHatch() {
    const c = document.createElement("canvas");
    c.width = c.height = 8;
    const g = c.getContext("2d");
    g.strokeStyle = "rgba(255,255,255,.07)";
    g.lineWidth = 1.5;
    g.beginPath();
    for (const o of [-8, 0, 8]) { g.moveTo(o, 8); g.lineTo(o + 8, 0); }
    g.stroke();
    return this.ctx.createPattern(c, "repeat");
  }

  /** The pieces of source visible now, in drawing order. */
  _pieces() {
    const out = [];
    const visible = (a, b) => this.x(b) > 0 && this.x(a) < this.W;
    for (const k of this.m.keep) if (visible(k.a, k.b)) out.push({ a: k.a, b: k.b, kept: true });
    if (this.mode === "source") {
      for (const g of this.m.groups) if (visible(g.a, g.b)) out.push({ a: g.a, b: g.b, kept: false });
    }
    return out;
  }

  draw() {
    const { ctx, W, H, dpr, lanes: L } = this;
    if (!W || !L) return;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.fillStyle = this.c.bg;
    ctx.fillRect(0, 0, W, H);
    ctx.fillStyle = this.c.lane;
    for (const k of ["ai", "text", "video", "audio"]) ctx.fillRect(0, L[k].y, W, L[k].h);
    const tail = this.x(this.m.T1);  // past the end of the material
    if (tail < W) { ctx.fillStyle = this.c.bg; ctx.fillRect(tail, RULER_H, W - tail, H - RULER_H); }
    const pieces = this._pieces();
    this._drawVideo(L.video);
    this._drawAudio(L.audio, pieces);
    this._drawText(L.text);
    if (this.mode === "source") this._drawCuts(L);
    else this._drawSplices(L);
    this._drawSplits(L);
    this._drawClipSelection(L);
    this._drawAI(L.ai);
    this._drawRange(L);
    this._drawSelectedEdges(L);
    this._drawRuler(L.ruler);
    this._drawSnap();
  }

  _drawVideo(L) {
    const { ctx, W } = this;
    const th = this.thumbs;
    ctx.save();
    ctx.beginPath();
    ctx.rect(0, L.y, Math.min(W, this.x(this.m.T1)), L.h);
    ctx.clip();
    if (th) {
      const tw = Math.max(24, L.h * th.w / th.h);
      const off = (this.scroll * this.pps) % tw;
      for (let x = -off; x < W; x += tw) {
        const t = this.srcAt(x + tw / 2);
        const i = clamp(Math.floor(t / th.interval), 0, th.count - 1);
        ctx.drawImage(th.img, (i % th.cols) * th.w, Math.floor(i / th.cols) * th.h, th.w, th.h, x, L.y, tw + 0.5, L.h);
      }
      ctx.fillStyle = "rgba(10,12,18,.18)";
      ctx.fillRect(0, L.y, W, L.h);
    } else {
      ctx.fillStyle = this.c["panel-3"];
      ctx.fillRect(0, L.y, W, L.h);
    }
    ctx.restore();
  }

  /** A mirrored wave for spans of source; the outline is the peak, the body the average.
   * Single rectangles: one path with thousands of them is many times slower to fill. */
  _wave(ctx, spans, X, secPerPx, mid, half, colors) {
    const peaks = this.m.peaks, T0 = this.m.T0, step = 1 / this.dpr;
    const framesPerStep = secPerPx * step * 100;  // 100 values per second
    const body = [];
    ctx.fillStyle = colors[0];
    for (const p of spans) {
      const base = X(p.a);
      const x0 = Math.max(0, base), x1 = Math.min(this.W, X(p.b));
      for (let x = x0; x < x1; x += step) {
        const i = (p.a + (x - base) * secPerPx - T0) * 100;
        const [mx, av] = peaks.range(i, i + framesPerStep);
        const hp = Math.max(step / 2, waveHeight(mx) * half);
        ctx.fillRect(x, mid - hp, step, 2 * hp);
        const ha = waveHeight(av) * half;
        if (ha > step / 2) body.push(x, ha);
      }
    }
    ctx.fillStyle = colors[1];
    for (let k = 0; k < body.length; k += 2) ctx.fillRect(body[k], mid - body[k + 1], step, 2 * body[k + 1]);
  }

  _drawAudio(L, pieces) {
    if (!this.m.peaks) return;
    const mid = L.y + L.h / 2, half = L.h / 2 - 2;
    const X = t => this.x(t);
    this._wave(this.ctx, pieces.filter(p => p.kept), X, 1 / this.pps, mid, half, [this.c["wave-dim"], this.c.wave]);
    this._wave(this.ctx, pieces.filter(p => !p.kept), X, 1 / this.pps, mid, half, [this.c["wave-cut"], this.c["wave-cut-2"]]);
  }

  _drawText(L) {
    const { ctx, W, pps } = this;
    const m = this.m;
    ctx.save();
    ctx.beginPath();
    ctx.rect(0, L.y, W, L.h);
    ctx.clip();
    ctx.font = `11px ${this.font}`;
    ctx.textBaseline = "middle";
    const y = L.y + L.h / 2 + 0.5;
    const tStart = this.srcAt(0), tEnd = this.srcAt(W);
    if (pps >= WORD_PPS) {
      // word bar: every word in its own box, at its real position
      let i = Math.max(0, m.wordIndexAt(tStart));
      for (; i < m.words.length; i++) {
        const w = m.words[i];
        if (w.s > tEnd) break;
        const x0 = this.x(w.s), x1 = this.x(w.e);
        const inCut = !!m.groupAt((w.s + w.e) / 2);
        if (this.mode === "result" && inCut) continue;
        ctx.fillStyle = inCut ? "rgba(255,255,255,.03)" : "rgba(255,255,255,.08)";
        ctx.fillRect(x0 + 0.5, L.y + 3, Math.max(1, x1 - x0 - 1), L.h - 6);
        ctx.save();
        ctx.beginPath();
        ctx.rect(x0 + 2, L.y, Math.max(0, x1 - x0 - 3), L.h);
        ctx.clip();
        ctx.fillStyle = inCut ? this.c.muted : this.c["ink-2"];
        ctx.fillText(w.t.trim(), x0 + 3, y);
        ctx.restore();
      }
    } else if (pps > 3) {
      // phrases: as much of the text as fits before the next phrase
      for (let k = 0; k < m.phrases.length; k++) {
        const p = m.phrases[k];
        if (p.end < tStart) continue;
        if (p.start > tEnd) break;
        const mid = (p.start + p.end) / 2;
        const inCut = !!m.groupAt(mid);
        if (this.mode === "result" && inCut) continue;
        const x0 = this.x(p.start);
        const next = m.phrases[k + 1];
        const x1 = Math.min(this.x(next ? next.start : p.end), x0 + Math.max(this.x(p.end) - x0, 0) + 40);
        if (x1 - x0 < 14) continue;
        ctx.fillStyle = inCut ? "rgba(255,255,255,.03)" : "rgba(255,255,255,.07)";
        ctx.fillRect(x0 + 0.5, L.y + 3, Math.max(1, this.x(p.end) - x0 - 1), L.h - 6);
        ctx.save();
        ctx.beginPath();
        ctx.rect(x0 + 2, L.y, x1 - x0 - 4, L.h);
        ctx.clip();
        ctx.fillStyle = inCut ? this.c.muted : this.c["ink-2"];
        ctx.fillText(m.words.slice(p.w0, p.w1).map(w => w.t).join("").trim(), x0 + 3, y);
        ctx.restore();
      }
    } else {
      // far away: speech as a density bar
      ctx.fillStyle = "rgba(255,255,255,.10)";
      for (const p of m.phrases) {
        if (p.end < tStart) continue;
        if (p.start > tEnd) break;
        if (this.mode === "result" && m.groupAt((p.start + p.end) / 2)) continue;
        const x0 = this.x(p.start);
        ctx.fillRect(x0, L.y + 8, Math.max(1, this.x(p.end) - x0), L.h - 16);
      }
    }
    ctx.restore();
  }

  _groupColor(g) { return this.kind[g.main.kind]; }

  /** Source view: cut material stays visible, shaded, so it is clear what goes away. */
  _drawCuts(L) {
    const { ctx, W } = this;
    const top = L.text.y, bottom = L.audio.y + L.audio.h;
    const sel = this.app.selEdit;
    for (const g of this.m.groups) {
      const x0 = this.x(g.a), x1 = this.x(g.b);
      if (x1 < 0 || x0 > W) continue;
      const w = Math.max(1, x1 - x0);
      ctx.fillStyle = "rgba(9,10,14,.66)";
      ctx.fillRect(x0, L.text.y, w, L.text.h);
      ctx.fillRect(x0, L.video.y, w, L.video.h);
      ctx.fillStyle = this.hatch;
      ctx.fillRect(x0, top, w, bottom - top);
      const col = this._groupColor(g);
      ctx.fillStyle = col;
      ctx.globalAlpha = 0.9;
      ctx.fillRect(x0, L.video.y, w, 2);
      ctx.globalAlpha = 1;
      if (w > 64) {
        ctx.font = `600 10.5px ${this.font}`;
        ctx.textBaseline = "middle";
        ctx.fillStyle = col;
        const label = `${KINDS[g.main.kind].name}  −${fmtDur(g.b - g.a)}`;
        ctx.save();
        ctx.beginPath();
        ctx.rect(x0 + 5, L.video.y, w - 8, L.video.h);
        ctx.clip();
        ctx.fillText(label, x0 + 6, L.video.y + L.video.h / 2);
        ctx.restore();
      }
      const isSel = sel && g.edits.includes(sel);
      if (!isSel && w > 3) {
        ctx.fillStyle = "rgba(255,255,255,.22)";
        ctx.fillRect(x0, top, 1, bottom - top);
        ctx.fillRect(x1 - 1, top, 1, bottom - top);
      }
    }
  }

  /** Result view: a cut is only a seam between two clips. From far away there are many of them,
   * so they are just notches on the picture; closer up they become full lines. */
  _drawSplices(L) {
    const { ctx, W } = this;
    const close = this.pps > 3;
    const top = close ? L.text.y : L.video.y;
    const bottom = close ? L.audio.y + L.audio.h : L.video.y + L.video.h;
    for (const g of this.m.groups) {
      const x = this.x(g.a);
      if (x < -2 || x > W + 2) continue;
      if (close) {
        ctx.fillStyle = "rgba(9,10,14,.9)";
        ctx.fillRect(x - 1, top, 2, bottom - top);
        ctx.fillStyle = this._groupColor(g);
        ctx.fillRect(x - 0.5, L.video.y, 1, L.video.h);
      } else {
        ctx.fillStyle = "rgba(9,10,14,.55)";
        ctx.fillRect(x - 0.5, top, 1, bottom - top);
      }
    }
  }

  _drawSplits(L) {
    const { ctx } = this;
    ctx.save();
    ctx.setLineDash([3, 3]);
    ctx.strokeStyle = this.c.ink;
    ctx.lineWidth = 1;
    for (const s of this.m.splits) {
      if (this.m.groupAt(s)) continue;
      const x = this.x(s);
      if (x < 0 || x > this.W) continue;
      ctx.beginPath();
      ctx.moveTo(x + 0.5, L.video.y);
      ctx.lineTo(x + 0.5, L.audio.y + L.audio.h);
      ctx.stroke();
    }
    ctx.restore();
  }

  _drawClipSelection(L) {
    const c = this.app.selClip;
    if (!c) return;
    const { ctx } = this;
    const x0 = this.x(c.a), x1 = this.x(c.b);
    ctx.strokeStyle = this.c.sel;
    ctx.lineWidth = 2;
    ctx.strokeRect(x0 + 1, L.video.y + 1, Math.max(2, x1 - x0 - 2), L.audio.y + L.audio.h - L.video.y - 2);
  }

  _drawAI(L) {
    const { ctx, W } = this;
    const main = { y: L.y + 3, h: L.h - 12 };
    const low = { y: L.y + L.h - 7, h: 5 };
    const edits = this.m.edits
      .filter(e => this.x(e.end) >= -8 && this.x(e.start) <= W + 8)
      .filter(e => e.on || e === this.app.selEdit || !this._insideCut(e))  // no decision to make there
      .sort((a, b) => (isPause(b) - isPause(a)) || (b.end - b.start) - (a.end - a.start));
    const sel = this.app.selEdit;
    const hov = this.hover?.edit;
    this.chips = [];
    ctx.font = `600 10.5px ${this.font}`;
    ctx.textBaseline = "middle";
    for (const e of edits) {
      const pause = isPause(e);
      const R = pause ? low : main;
      const col = this.kind[e.kind];
      let x0 = this.x(e.start), x1 = this.x(e.end);
      const collapsed = this.mode === "result" && e.on;
      if (collapsed) { x0 -= 4; x1 += 4; }
      const w = Math.max(pause ? 1.5 : 3, x1 - x0);
      this.chips.push({ e, x0, x1: x0 + w, y0: R.y, y1: R.y + R.h, collapsed });
      if (pause) {
        ctx.globalAlpha = e.on ? 0.85 : 0.28;
        ctx.fillStyle = col;
        ctx.fillRect(x0, R.y, w, R.h);
        ctx.globalAlpha = 1;
        if (e === sel) {
          ctx.strokeStyle = this.c.sel;
          ctx.lineWidth = 1.5;
          ctx.strokeRect(x0 - 0.5, R.y - 0.5, w + 1, R.h + 1);
        }
        continue;
      }
      const r = Math.min(4, w / 2);
      ctx.beginPath();
      ctx.roundRect(x0, R.y, w, R.h, r);
      if (e.on) {
        ctx.fillStyle = col;
        ctx.globalAlpha = e === hov || e === sel ? 1 : 0.9;
        ctx.fill();
        ctx.globalAlpha = 1;
      } else {
        ctx.fillStyle = col;
        ctx.globalAlpha = 0.14;
        ctx.fill();
        ctx.globalAlpha = 1;
        ctx.strokeStyle = col;
        ctx.lineWidth = 1;
        ctx.setLineDash([3, 2]);
        ctx.stroke();
        ctx.setLineDash([]);
      }
      if (w > 30) {
        const ai = e.llm ? "✦ " : "";
        ctx.save();
        ctx.beginPath();
        ctx.rect(x0 + 4, R.y, w - 8, R.h);
        ctx.clip();
        ctx.fillStyle = e.on ? "#14151a" : col;
        ctx.fillText(ai + KINDS[e.kind].name, x0 + 5, R.y + R.h / 2 + 0.5);
        ctx.restore();
      }
      if (e === sel || e === hov) {
        ctx.beginPath();
        ctx.roundRect(x0 - 1, R.y - 1, w + 2, R.h + 2, r + 1);
        ctx.strokeStyle = e === sel ? this.c.sel : this.c.ink;
        ctx.lineWidth = e === sel ? 2 : 1;
        ctx.stroke();
      }
    }
  }

  _insideCut(e) {
    const g = this.m.groupAt(e.start);
    return !!g && g.b >= e.end;
  }

  _drawRange(L) {
    const { ctx, H } = this;
    const { i, o } = this.app.marks;
    const line = (t, color) => {
      const x = this.x(t);
      ctx.fillStyle = color;
      ctx.fillRect(x - 0.5, L.ruler.y, 1.5, H);
    };
    if (i != null && o != null) {
      const x0 = this.x(i), x1 = this.x(o);
      ctx.fillStyle = "rgba(123,147,255,.20)";
      ctx.fillRect(x0, L.ai.y, x1 - x0, H - L.ai.y);
      ctx.fillStyle = "rgba(123,147,255,.55)";
      ctx.fillRect(x0, L.ruler.y, x1 - x0, L.ruler.h);
    }
    if (i != null) line(i, this.c.accent);
    if (o != null) line(o, this.c.accent);
  }

  /** Amber brackets around the selected cut, the way an NLE shows what a trim will move. */
  _drawSelectedEdges(L) {
    const e = this.app.selEdit;
    if (!e) return;
    const { ctx } = this;
    const top = L.text.y, bottom = L.audio.y + L.audio.h;
    const bracket = (x, dir) => {
      ctx.fillStyle = this.c.sel;
      ctx.fillRect(x - (dir > 0 ? 0 : 2), top, 2, bottom - top);
      ctx.fillRect(x - (dir > 0 ? 0 : 6), top, 6, 2);
      ctx.fillRect(x - (dir > 0 ? 0 : 6), bottom - 2, 6, 2);
    };
    if (this.mode === "source") {
      bracket(this.x(e.start), 1);
      bracket(this.x(e.end), -1);
    } else if (e.on) {
      const x = this.x(e.start);
      bracket(x + 1, 1);
      bracket(x - 1, -1);
    } else {
      bracket(this.x(e.start), 1);
      bracket(this.x(e.end), -1);
    }
  }

  _drawRuler(L) {
    const { ctx, W, pps } = this;
    ctx.fillStyle = this.c.panel;
    ctx.fillRect(0, L.y, W, L.h);
    ctx.fillStyle = this.c.line;
    ctx.fillRect(0, L.y + L.h - 1, W, 1);
    const steps = [0.1, 0.2, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600];
    const major = steps.find(s => s * pps >= 90) ?? 3600;
    const minor = major / (major >= 60 ? (major === 60 ? 6 : 4) : major >= 1 ? 5 : 5);
    ctx.font = `10.5px ${this.font}`;
    ctx.textBaseline = "alphabetic";
    const startD = this.scroll, endD = this.scroll + W / pps;
    ctx.fillStyle = this.c["line-2"];
    for (let d = Math.floor(startD / minor) * minor; d <= endD; d += minor) {
      const x = (d - this.scroll) * pps;
      ctx.fillRect(x, L.y + L.h - 5, 1, 4);
    }
    for (let d = Math.floor(startD / major) * major; d <= endD; d += major) {
      const x = (d - this.scroll) * pps;
      ctx.fillStyle = this.c.muted;
      ctx.fillRect(x, L.y + L.h - 9, 1, 8);
      // the same labels in both views would lie: "result" counts the finished video
      const label = major < 1 ? `${fmtClock(Math.floor(d))}.${Math.round((d % 1) * 10)}` : fmtClock(d);
      ctx.fillStyle = this.c.muted;
      ctx.fillText(label, x + 4, L.y + 13);
    }
  }

  _drawSnap() {
    const s = this.snapMark;
    if (!s) return;
    const { ctx, H } = this;
    const x = this.x(s.t);
    ctx.fillStyle = this.c.sel;
    ctx.fillRect(x - 0.5, 0, 1, H);
    if (s.label) {
      ctx.font = `600 10px ${this.font}`;
      ctx.textBaseline = "middle";
      const w = ctx.measureText(s.label).width + 10;
      const bx = clamp(x - w / 2, 2, this.W - w - 2);
      ctx.fillStyle = this.c.sel;
      ctx.beginPath();
      ctx.roundRect(bx, 3, w, 16, 4);
      ctx.fill();
      ctx.fillStyle = "#14151a";
      ctx.fillText(s.label, bx + 5, 11.5);
    }
  }

  // ---------------------------------------------------------------- overview: the whole lesson at a glance
  drawOverview() {
    const { ovCtx: ctx, dpr } = this;
    const W = this.ov.clientWidth, H = this.ov.clientHeight;
    if (!W) return;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.fillStyle = this.c.panel;
    ctx.fillRect(0, 0, W, H);
    const dur = Math.max(1, this.duration);
    const k = W / dur;
    const X = t => this.srcToD(t) * k;
    if (this.m.peaks) {
      const X = t => this.srcToD(t) * k;
      const save = this.W;
      this.W = W;  // _wave clips to the width of the canvas it draws on
      this._wave(ctx, this.m.keep, X, 1 / k, H / 2, H / 2 - 2, [this.c["wave-dim"], this.c.wave]);
      if (this.mode === "source") this._wave(ctx, this.m.groups, X, 1 / k, H / 2, H / 2 - 2, [this.c["wave-cut"], this.c["wave-cut-2"]]);
      this.W = save;
    }
    if (this.mode === "source") {
      for (const g of this.m.groups) {
        ctx.fillStyle = this._groupColor(g);
        ctx.globalAlpha = 0.85;
        ctx.fillRect(X(g.a), 0, Math.max(1, X(g.b) - X(g.a)), 3);
        ctx.globalAlpha = 1;
      }
    }
    // the part shown below
    if (!this.fit) {
      const vx = this.scroll * k, vw = Math.max(6, this.W / this.pps * k);
      ctx.fillStyle = "rgba(0,0,0,.42)";
      ctx.fillRect(0, 0, vx, H);
      ctx.fillRect(vx + vw, 0, W - vx - vw, H);
      ctx.strokeStyle = this.c.ink;
      ctx.lineWidth = 1;
      ctx.strokeRect(vx + 0.5, 0.5, vw - 1, H - 1);
    }
  }

  _bindOverview() {
    const ov = this.ov;
    let drag = null;
    const at = ev => {
      const r = ov.getBoundingClientRect();
      return (ev.clientX - r.left) / r.width * this.duration;
    };
    ov.addEventListener("pointerdown", ev => {
      ov.setPointerCapture(ev.pointerId);
      const d = at(ev);
      const span = this.W / this.pps;
      if (!this.fit && d >= this.scroll && d <= this.scroll + span) {
        drag = { type: "pan", off: d - this.scroll };
      } else {
        drag = { type: "seek" };
        this.app.scrubStart();
        this.app.seek(this.dToSrc(d), { coalesce: true });
        if (!this.fit) this.reveal(this.dToSrc(d));
      }
    });
    ov.addEventListener("pointermove", ev => {
      if (!drag) {
        const d = at(ev), span = this.W / this.pps;
        ov.style.cursor = !this.fit && d >= this.scroll && d <= this.scroll + span ? "grab" : "pointer";
        return;
      }
      const d = at(ev);
      if (drag.type === "pan") {
        this.scroll = d - drag.off;
        this._clampScroll();
        this.invalidate();
        this.setPlayhead(this.playheadT);
      } else {
        this.app.seek(this.dToSrc(clamp(d, 0, this.duration)), { coalesce: true });
        if (!this.fit) this.reveal(this.playheadT);
      }
    });
    const up = () => {
      if (drag?.type === "seek") this.app.scrubEnd();
      drag = null;
    };
    ov.addEventListener("pointerup", up);
    ov.addEventListener("pointercancel", up);
    ov.addEventListener("mouseenter", () => this.app.hint("Весь урок. Клик — перейти, рамку можно двигать"));
    ov.addEventListener("mouseleave", () => this.app.hint(null));
  }

  // ---------------------------------------------------------------- what is under the pointer
  hit(x, y) {
    const L = this.lanes;
    const { i, o } = this.app.marks;
    if (y < L.ruler.y + L.ruler.h) return { type: "ruler" };
    if (i != null && Math.abs(x - this.x(i)) <= EDGE_PX) return { type: "mark", side: "i" };
    if (o != null && Math.abs(x - this.x(o)) <= EDGE_PX) return { type: "mark", side: "o" };
    if (y < L.ai.y + L.ai.h) {
      for (let k = this.chips.length - 1; k >= 0; k--) {
        const c = this.chips[k];
        if (x < c.x0 - 2 || x > c.x1 + 2 || y < c.y0 - 1 || y > c.y1 + 1) continue;
        if (!c.collapsed && c.x1 - c.x0 >= 14) {
          if (x - c.x0 < EDGE_PX) return { type: "edge", edit: c.e, side: "start" };
          if (c.x1 - x < EDGE_PX) return { type: "edge", edit: c.e, side: "end" };
        }
        return { type: "chip", edit: c.e };
      }
      return { type: "none" };
    }
    // the edges of cuts are grabbable in every lane below
    let best = null, bd = EDGE_PX + 1;
    for (const g of this.m.groups) {
      const xa = this.x(g.a), xb = this.x(g.b);
      if (xb < -EDGE_PX || xa > this.W + EDGE_PX) continue;
      if (this.mode === "result") {
        // a seam has two sides: left of it the previous clip ends, right of it the next begins
        if (Math.abs(x - xa) <= EDGE_PX) {
          return x < xa ? { type: "edge", edit: g.edgeA, side: "start", group: g }
            : { type: "edge", edit: g.edgeB, side: "end", group: g };
        }
      } else {
        const da = Math.abs(x - xa), db = Math.abs(x - xb);
        if (da < bd) { bd = da; best = { type: "edge", edit: g.edgeA, side: "start", group: g }; }
        if (db < bd) { bd = db; best = { type: "edge", edit: g.edgeB, side: "end", group: g }; }
      }
    }
    if (best) return best;
    const t = this.srcAt(x);
    if (t > this.m.T1) return { type: "none" };
    const g = this.mode === "source" ? this.m.groupAt(t) : null;
    if (g) return { type: "cut", group: g, t };
    return { type: "clip", clip: this.m.clipAt(t), t };
  }

  _cursor(h) {
    switch (h.type) {
      case "edge": case "mark": return "ew-resize";
      case "ruler": return "col-resize";
      case "chip": case "cut": return "pointer";
      default: return "default";
    }
  }

  _hintFor(h) {
    switch (h.type) {
      case "ruler": return "Тяните, чтобы перемотать";
      case "edge": return "Тяните, чтобы сдвинуть границу. Липнет к паузам между словами, с <kbd>⌥</kbd> — свободно";
      case "mark": return "Тяните край выделения";
      case "chip": {
        const e = h.edit;
        return `<b>${KINDS[e.kind].name}</b>: клик — открыть, <kbd>X</kbd> — ${e.on ? "оставить" : "вырезать"}, двойной клик — послушать`;
      }
      case "cut": return "Вырезанный кусок. Клик — выбрать, <kbd>X</kbd> — вернуть, двойной клик — послушать";
      case "clip": return "Клик — поставить курсор. Тяните — выделить кусок и вырезать его <kbd>⌫</kbd>";
      default: return null;
    }
  }

  // ---------------------------------------------------------------- gestures
  _bindPointer() {
    const st = this.stage;
    const pos = ev => {
      const r = st.getBoundingClientRect();
      return { x: ev.clientX - r.left, y: ev.clientY - r.top };
    };
    st.addEventListener("pointerdown", ev => {
      if (ev.button !== 0) return;
      const p = pos(ev);
      const h = this.hit(p.x, p.y);
      st.setPointerCapture(ev.pointerId);
      this.lastPointer = p;
      this.app.blurInputs();
      switch (h.type) {
        case "ruler":
          this.drag = { type: "scrub" };
          this.app.scrubStart();
          this.app.seek(this.srcAt(p.x), { coalesce: true });
          break;
        case "edge":
          this.m.beginGesture();
          this.drag = {
            type: "trim", edit: h.edit, side: h.side, x0: p.x, scroll0: this.scroll,
            t0: h.side === "start" ? h.edit.start : h.edit.end,
            other: h.side === "start" ? h.edit.end : h.edit.start,
          };
          this.app.select({ edit: h.edit }, { scroll: true });
          break;
        case "mark":
          this.drag = { type: "mark", side: h.side };
          break;
        default:
          this.drag = { type: "press", x0: p.x, y0: p.y, hit: h, shift: ev.shiftKey };
      }
      this._startAutoScroll();
    });

    st.addEventListener("pointermove", ev => {
      const p = pos(ev);
      this.lastPointer = p;
      this.lastAlt = ev.altKey;
      if (this.drag) { this._dragMove(p, ev.altKey); return; }
      const h = this.hit(p.x, p.y);
      st.style.cursor = this._cursor(h);
      if (this.hover?.edit !== h.edit) this.dirty = true;
      this.hover = h;
      this.app.hint(this._hintFor(h));
      this._showHoverLine(p);
    });

    st.addEventListener("pointerleave", () => {
      if (this.drag) return;
      this.hover = null;
      this.dirty = true;
      this.hoverEl.style.display = "none";
      this.hoverThumb.style.display = "none";
      this.app.hint(null);
    });

    const finish = ev => {
      const d = this.drag;
      this.drag = null;
      this._stopAutoScroll();
      this.snapMark = null;
      this.dirty = true;
      if (!d) return;
      if (d.type === "scrub") this.app.scrubEnd();
      else if (d.type === "trim") {
        const cur = d.side === "start" ? d.edit.start : d.edit.end;
        if (Math.abs(cur - d.t0) < 1e-6) this.m.cancelGesture();
        else this.m.commit("граница правки", () => { d.edit.reviewed = true; });
        this.app.afterTrim(d.edit, d.side);
      } else if (d.type === "press") {
        if (!ev || ev.type === "pointercancel") return;
        const p = pos(ev);
        this.app.timelineClick(d.hit, this.srcAt(p.x), p);
      } else if (d.type === "range") {
        this.app.rangeDone();
      }
    };
    st.addEventListener("pointerup", finish);
    st.addEventListener("pointercancel", finish);

    st.addEventListener("dblclick", ev => {
      const p = pos(ev);
      const h = this.hit(p.x, p.y);
      if (h.type === "chip") this.app.previewSource(h.edit);
      else if (h.type === "cut") this.app.previewSource(h.group.main);
    });

    st.addEventListener("wheel", ev => {
      ev.preventDefault();
      if (ev.ctrlKey || ev.metaKey || ev.altKey) {
        const p = pos(ev);
        this.zoomBy(Math.exp(-ev.deltaY * (ev.ctrlKey ? 0.012 : 0.003)), p.x);
        return;
      }
      if (this.fit) return;
      const d = Math.abs(ev.deltaX) > Math.abs(ev.deltaY) ? ev.deltaX : ev.deltaY;
      this.scroll += d / this.pps;
      this._clampScroll();
      this.invalidate();
      this.setPlayhead(this.playheadT);
    }, { passive: false });

    // Safari and the Mac app window report a trackpad pinch as gesture events, not as ctrl+wheel
    let gestureZoom = 1;
    st.addEventListener("gesturestart", ev => { ev.preventDefault(); gestureZoom = this.pps; });
    st.addEventListener("gesturechange", ev => {
      ev.preventDefault();
      const r = st.getBoundingClientRect();
      this.setZoom(gestureZoom * ev.scale, ev.clientX - r.left);
    });
    st.addEventListener("gestureend", ev => ev.preventDefault());
  }

  _showHoverLine(p) {
    const t = this.srcAt(p.x);
    if (t > this.m.T1 || p.x < 0 || p.x > this.W) {
      this.hoverEl.style.display = "none";
      this.hoverThumb.style.display = "none";
      return;
    }
    this.hoverEl.style.display = "";
    this.hoverEl.style.transform = `translateX(${p.x}px)`;
    this.hoverEl.firstElementChild.textContent = fmtClock(this.mode === "source" ? t - this.m.T0 : this.m.srcToOut(t));
    const L = this.lanes;
    const overPicture = p.y < L.ruler.y + L.ruler.h || (p.y >= L.video.y && p.y < L.video.y + L.video.h);
    const th = this.thumbs;
    if (th && overPicture) {
      const i = clamp(Math.floor(t / th.interval), 0, th.count - 1);
      const w = 200, k = w / th.w, h = th.h * k;
      const el = this.hoverThumb;
      el.style.display = "";
      el.style.width = w + "px";
      el.style.height = h + "px";
      el.style.backgroundImage = `url("${this.thumbUrl}")`;
      el.style.backgroundSize = `${th.cols * w}px ${th.rows * h}px`;
      el.style.backgroundPosition = `-${(i % th.cols) * w}px -${Math.floor(i / th.cols) * h}px`;
      el.style.transform = `translate(${clamp(p.x - w / 2, 2, this.W - w - 2)}px, ${-h - 8}px)`;
    } else {
      this.hoverThumb.style.display = "none";
    }
  }

  _dragMove(p, alt) {
    const d = this.drag;
    if (!d) return;
    switch (d.type) {
      case "scrub":
        this.app.seek(this.srcAt(clamp(p.x, 0, this.W)), { coalesce: true });
        break;
      case "trim": {
        const raw = d.t0 + ((p.x + this.scroll * this.pps) - (d.x0 + d.scroll0 * this.pps)) / this.pps;
        const t = this._snapped(raw, alt, { skip: [d.t0, d.other], side: d.side });
        this.m.setEdge(d.edit, d.side, t);
        this.m.live();
        this.app.seek(d.side === "start" ? d.edit.start : d.edit.end, { coalesce: true });
        break;
      }
      case "press":
        if (Math.abs(p.x - d.x0) > 3) {
          const anchor = this.srcAt(d.x0);
          this.drag = { type: "range", anchor };
          this.app.setMarks(anchor, anchor);
          this._dragMove(p, alt);
        }
        break;
      case "range": {
        const t0 = this.srcAt(clamp(p.x, 0, this.W));
        const side = t0 < d.anchor ? "start" : "end";
        const t = this._snapped(t0, alt, { side });
        this.app.setMarks(Math.min(d.anchor, t), Math.max(d.anchor, t));
        break;
      }
      case "mark": {
        const { i, o } = this.app.marks;
        const t = this._snapped(this.srcAt(clamp(p.x, 0, this.W)), alt, {
          skip: [d.side === "i" ? i : o], side: d.side === "i" ? "start" : "end",
        });
        if (d.side === "i") this.app.setMarks(o != null ? Math.min(t, o - 0.05) : t, o);
        else this.app.setMarks(i, i != null ? Math.max(t, i + 0.05) : t);
        break;
      }
    }
    this.dirty = true;
  }

  _snapped(t, alt, opts) {
    this.snapMark = null;
    if (alt || !this.app.snapOn) return t;
    const radius = Math.min(1.5, SNAP_PX / this.pps);
    // a trim drags the playhead along to show the frame at the cut, so it must not stick to it
    const extra = this.drag?.type === "trim" ? [] : [{ t: this.playheadT, label: "курсор" }];
    const s = this.m.snap(t, radius, { ...opts, extra });
    if (!s) return t;
    this.snapMark = s;
    return s.t;
  }

  // dragging against the side of the timeline keeps moving it
  _startAutoScroll() {
    clearInterval(this._autoScroll);
    this._autoScroll = setInterval(() => {
      if (!this.drag || this.drag.type === "press" || !this.lastPointer || this.fit) return;
      const { x } = this.lastPointer;
      const edge = 36;
      let v = 0;
      if (x < edge) v = -(edge - x);
      else if (x > this.W - edge) v = x - (this.W - edge);
      if (!v) return;
      this.scroll += v * 0.5 / this.pps;
      this._clampScroll();
      this.invalidate();
      this.setPlayhead(this.playheadT);
      this._dragMove(this.lastPointer, this.lastAlt);
    }, 40);
  }

  _stopAutoScroll() { clearInterval(this._autoScroll); }
}

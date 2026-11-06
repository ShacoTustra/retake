// The editor page: connects the model, the player, the timeline and the panels,
// and owns what is selected, the keyboard, saving and background jobs.

import { Assist, Transcript } from "./panels.js";
import { fmtClock, fmtDur, fmtTC, isMinor, isPause, KINDS, Model } from "./model.js";
import { Player } from "./player.js";
import { Timeline } from "./timeline.js";

const R = window.RETAKE;
const $ = s => document.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

async function api(path, opts = {}) {
  const r = await fetch(path, {
    ...opts, headers: { "X-Retake-Token": R.token, "Content-Type": "application/json" },
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || r.statusText);
  return j;
}

const store = {  // conveniences only: everything works if the browser keeps nothing
  get(k, d) { try { return JSON.parse(localStorage.getItem("retake:" + k)) ?? d; } catch { return d; } },
  set(k, v) { try { localStorage.setItem("retake:" + k, JSON.stringify(v)); } catch { /* private mode */ } },
};

const DEFAULT_HINT = "<kbd>Пробел</kbd> пуск · <kbd>Tab</kbd> следующая подсказка · <kbd>X</kbd> вырезать/оставить · <kbd>?</kbd> все клавиши";
const JOB = { llm: "AI-проверка", render: "Сборка видео", detect: "Поиск правок", process: "Обработка" };

class App {
  constructor(data, env) {
    this.data = data;
    this.model = new Model(data, env);
    this.selEdit = null;
    this.selClip = null;
    this.marks = { i: null, o: null };
    this.snapOn = store.get("snap", true);
    this.skipOn = store.get("skip", true);
    this.listenOnSelect = store.get("autolisten", true);
    this.player = new Player($("#vA"), $("#vB"), this.model);
    this.player.onJump = g => this._flashCut(g);
    this.player.onPlayError = () => this.hint("Браузер не дал включить видео. Нажмите пробел или кнопку пуска ещё раз");
    this.timeline = new Timeline(this);
    this.transcript = new Transcript(this);
    this.assist = new Assist(this);
    this.player.skip = this.skipOn;
    this.player.setBaseRate(store.get("rate", 1));
    this.player.setVolume(store.get("volume", 1));
    this.model.subscribe(kind => this._onModel(kind));
    this._bindControls();
    this._bindKeys();
    this._bindSplitters();
    this._bindVideoErrors();
    this.player.load(data.video);
    this.hint(null);
    this._renderHeader();
    this._renderSelectionBar();
    this.assist.render();
    this._renderAi();
    this.setSkip(this.skipOn);
    this._restoreView();
    $("#rateBtn").textContent = fmtRate(this.player.baseRate);
    $("#volume").value = this.player.volume;
    $("#btnSnap").classList.toggle("active", this.snapOn);
    this._lastPaused = true;
    this._tick = this._tick.bind(this);
    requestAnimationFrame(this._tick);
    this._maybeShowWelcome();
    this._loadThumbs();
    this.pollJobs(false);
    addEventListener("pagehide", () => this.flush(true));
  }

  // ---------------------------------------------------------------- the main loop
  _tick() {
    const p = this.player;
    p.tick();
    const t = p.time;
    const paused = p.paused;
    if (t !== this._lastT) {
      this._lastT = t;
      this.timeline.setPlayhead(t, { follow: !paused });
      this.transcript.setTime(t, !paused);
      this._renderClock(t);
      this._saveViewSoon(t);
    }
    if (paused !== this._lastPaused) {
      this._lastPaused = paused;
      $("#btnPlay use").setAttribute("href", paused ? "#i-play" : "#i-pause");
      $("#btnPlay").title = paused ? "Пуск (пробел)" : "Пауза (пробел)";
    }
    requestAnimationFrame(this._tick);
  }

  _renderClock(t) {
    const m = this.model;
    const big = this.timeline.mode === "result" ? m.srcToOut(t) : t - m.T0;
    $("#tcMain").textContent = fmtTC(big);
    const stats = m.stats();
    $("#tcSub").textContent = this.timeline.mode === "result"
      ? `из ${fmtClock(stats.out)}`
      : `из ${fmtClock(stats.total)} · в итоге ${fmtClock(m.srcToOut(t))}`;
    const g = m.groupAt(t);
    const badge = $("#vBadge");
    if (g) {
      badge.style.display = "";
      badge.style.setProperty("--c", `var(${KINDS[g.main.kind].color})`);
      badge.textContent = `✂ Будет вырезано · ${KINDS[g.main.kind].name} · ${fmtDur(g.b - g.a)}`;
    } else {
      badge.style.display = "none";
    }
  }

  _flashCut(g) {
    const el = $("#vFlash");
    el.textContent = `✂ −${fmtDur(g.b - g.a)}`;
    el.classList.remove("show");
    void el.offsetWidth;  // restart the animation
    el.classList.add("show");
  }

  // ---------------------------------------------------------------- model changes
  _onModel(kind) {
    this.timeline.invalidate();
    this._renderHeader();
    if (kind !== "commit") return;
    if (this.selEdit && !this.model.edits.includes(this.selEdit)) this.selEdit = null;
    if (this.selClip && !this.model.clipAt((this.selClip.a + this.selClip.b) / 2)) this.selClip = null;
    this.transcript.update();
    const top = this.assist.scroller.scrollTop;
    this.assist.render();
    this.assist.scroller.scrollTop = top;
    this._renderSelectionBar();
    this._saveSoon();
    this._renderUndo();
    this._renderClock(this.player.time);
  }

  _renderHeader() {
    const s = this.model.stats();
    const pct = s.total ? Math.round(s.cut / s.total * 100) : 0;
    $("#stats").innerHTML = `<span class="from">${fmtClock(s.total)}</span><svg class="i"><use href="#i-arrow"/></svg><b>${fmtClock(s.out)}</b><span class="pct">−${pct}%</span>`;
    $("#stats").title = `Исходник ${fmtDur(s.total)}, вырезается ${fmtDur(s.cut)}, ${s.splices} мест`;
  }

  _renderUndo() {
    const u = this.model.undoStack, r = this.model.redoStack;
    $("#btnUndo").disabled = !u.length;
    $("#btnRedo").disabled = !r.length;
    $("#btnUndo").title = u.length ? `Отменить: ${u[u.length - 1].label} (⌘Z)` : "Отменить (⌘Z)";
    $("#btnRedo").title = r.length ? `Повторить: ${r[r.length - 1].label} (⇧⌘Z)` : "Повторить (⇧⌘Z)";
  }

  // ---------------------------------------------------------------- saving
  _saveSoon() {
    clearTimeout(this._saveTimer);
    $("#saved").textContent = "";
    this._saveTimer = setTimeout(() => this.flush(), 400);
    this._dirtyDecisions = true;
  }

  async flush(leaving = false) {
    clearTimeout(this._saveTimer);
    if (!this._dirtyDecisions) return;
    this._dirtyDecisions = false;
    const url = `/api/projects/${R.project}/decisions`;
    const body = JSON.stringify(this.model.decisions());
    try {
      const r = await fetch(url, {
        method: "PUT", keepalive: leaving, body,
        headers: { "X-Retake-Token": R.token, "Content-Type": "application/json" },
      });
      if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || r.statusText);
      $("#saved").textContent = "сохранено";
    } catch (e) {
      this._dirtyDecisions = true;
      this.toast(`Не удалось сохранить решения: ${e.message}`, { kind: "err", sticky: true });
    }
  }

  _saveViewSoon(t) {
    clearTimeout(this._viewTimer);
    this._viewTimer = setTimeout(() => store.set(`view:${R.project}`, { t }), 800);
  }

  _restoreView() {
    const v = store.get(`view:${R.project}`, null);
    const t = v && v.t > this.model.T0 ? v.t : this.model.T0;
    const video = $("#vA");
    const apply = () => {
      this.player.seek(t);
      this.timeline.setPlayhead(t);
    };
    if (video.readyState >= 1) apply();
    else video.addEventListener("loadedmetadata", apply, { once: true });
  }

  // ---------------------------------------------------------------- playhead and selection
  seek(t, opts) {
    this.player.seek(t, opts);
    this.timeline.setPlayhead(this.player.time);
  }

  scrubStart() {
    this._resume = !this.player.paused;
    if (this._resume) this.player.pause();
  }

  scrubEnd() {
    if (this._resume) this.player.play();
    this._resume = false;
  }

  blurInputs() {
    if (document.activeElement && document.activeElement !== document.body) document.activeElement.blur();
  }

  /** sel: {edit} | {clip} | null */
  select(sel, { seek = false, listen = false, scroll = true } = {}) {
    this.selEdit = sel?.edit ?? null;
    this.selClip = sel?.clip ?? null;
    this.timeline.dirty = true;
    this.transcript.update();
    const top = this.assist.scroller.scrollTop;
    this.assist.render();
    this.assist.scroller.scrollTop = top;
    if (this.selEdit && scroll) {
      this._showInAssist(this.selEdit);
      this.assist.scrollToSelected();
      this.timeline.reveal(this.selEdit.start, this.selEdit.end);
      this.transcript.reveal(this.selEdit.start);
    }
    if (this.selEdit && seek) this.seek(Math.max(this.model.T0, this.selEdit.start));
    if (this.selEdit && listen && this.listenOnSelect) this.previewSource(this.selEdit);
    this._renderSelectionBar();
  }

  /** The selected thing must be visible in the panel, whatever filter is on. */
  _showInAssist(e) {
    if (this.assist.items().includes(e)) return;
    this.assist.filter = isPause(e) ? "pause" : isMinor(e) ? "minor" : "main";
    this.assist.aiOnly = false;
    this.assist.render();
  }

  timelineClick(h, t) {
    this.clearMarks();
    switch (h.type) {
      case "chip":
        this.select({ edit: h.edit }, { seek: true });
        break;
      case "cut":
        this.seek(t);
        this.select({ edit: h.group.main });
        break;
      case "clip":
        this.seek(t);
        this.select(h.clip ? { clip: h.clip } : null);
        break;
      default:
        this.select(null);
    }
  }

  clickWord(word) {
    const mid = (word.s + word.e) / 2;
    this.seek(word.s);
    const g = this.model.groupAt(mid);
    if (g) { this.select({ edit: g.main }, { scroll: false }); return; }
    const e = this.model.edits.find(x => x.kind !== "pause" && x.start <= mid && mid <= x.end);
    this.select(e ? { edit: e } : null, { scroll: false });
    if (e) this.assist.scrollToSelected();
  }

  selectWords(i0, i1) {
    const m = this.model;
    const a = m.gapPoint(i0 - 1, "start"), b = m.gapPoint(i1, "end");
    this.select(null);
    this.setMarks(a, b);
    this.seek(a);
    this.timeline.reveal(a, b);
  }

  // ---------------------------------------------------------------- in/out marks: the range
  get range() {
    const { i, o } = this.marks;
    return i != null && o != null && o > i ? { a: i, b: o } : null;
  }

  setMarks(i, o) {
    this.marks = { i, o };
    this.selClip = null;
    this.timeline.dirty = true;
    this._renderSelectionBar();
    this.transcript?.invalidate();
  }

  clearMarks() {
    if (this.marks.i == null && this.marks.o == null) return;
    this.setMarks(null, null);
  }

  rangeDone() {
    const r = this.range;
    if (!r || r.b - r.a < 0.05) { this.clearMarks(); return; }
    this.select(null, { scroll: false });
    this.hint("Выделено. <kbd>⌫</kbd> — вырезать, <kbd>⇧⌫</kbd> — вернуть, <kbd>/</kbd> — послушать, <kbd>Esc</kbd> — снять");
  }

  markIn() {
    const t = this.player.time;
    const o = this.marks.o != null && this.marks.o > t ? this.marks.o : null;
    this.setMarks(t, o);
  }

  markOut() {
    const t = this.player.time;
    const i = this.marks.i != null && this.marks.i < t ? this.marks.i : null;
    this.setMarks(i, t);
  }

  _renderSelectionBar() {
    const bar = $("#selBar");
    const r = this.range;
    const c = this.selClip;
    if (r) {
      bar.innerHTML = `<span class="what">Выделено <b>${fmtDur(r.b - r.a)}</b></span>
        <button data-sel="cut" class="danger" title="Вырезать выделенное (⌫)">Вырезать</button>
        <button data-sel="restore" title="Вернуть вырезанное в этом месте (⇧⌫)">Вернуть</button>
        <button data-sel="hear" title="Послушать (/)">▶</button>
        <button data-sel="clear" class="x" title="Снять выделение (Esc)">✕</button>`;
    } else if (c) {
      bar.innerHTML = `<span class="what">Фрагмент <b>${fmtDur(c.b - c.a)}</b></span>
        <button data-sel="cutclip" class="danger" title="Вырезать фрагмент (⌫)">Вырезать</button>
        <button data-sel="hearclip" title="Послушать (/)">▶</button>`;
    } else {
      bar.innerHTML = "";
    }
  }

  // ---------------------------------------------------------------- editing commands
  cutSelection() {
    const m = this.model;
    const r = this.range;
    if (r) {
      const e = m.addCut(r.a, r.b);
      this.clearMarks();
      if (e) { this.select({ edit: e }, { scroll: false }); this.toast(`Вырезано ${fmtDur(e.end - e.start)}`, { undo: true }); }
      else this.hint("Это место уже вырезано");
      return;
    }
    if (this.selClip) {
      const e = m.addCut(this.selClip.a, this.selClip.b);
      this.selClip = null;
      if (e) { this.select({ edit: e }, { scroll: false }); this.toast(`Вырезано ${fmtDur(e.end - e.start)}`, { undo: true }); }
      return;
    }
    if (this.selEdit && !this.selEdit.on) { this.toggleSelected(); return; }
    this.hint("Сначала выделите, что вырезать: потяните по дорожке, выделите текст или поставьте <kbd>I</kbd> и <kbd>O</kbd>");
  }

  restoreSelection() {
    const r = this.range;
    if (!r) return;
    this.model.restoreRange(r.a, r.b);
    this.clearMarks();
    this.toast("Возвращено", { undo: true });
  }

  toggleSelected() {
    if (this.range) { this.cutSelection(); return; }
    const e = this.selEdit;
    if (!e) {
      if (this.selClip) this.cutSelection();
      else {
        // X over a cut without selecting it first
        const g = this.model.groupAt(this.player.time);
        if (g) { this.select({ edit: g.main }); this.toggleSelected(); }
      }
      return;
    }
    const wasManual = e.kind === "manual";
    this.model.setOn(e, !e.on);
    if (wasManual) this.toast("Вырезка убрана", { undo: true });
  }

  /** Decide and go on: the suggestion stays as it is, the next one opens. */
  confirmAndNext() {
    const e = this.selEdit;
    if (e) this.model.markReviewed(e);
    this.nextSuggestion(1);
  }

  nextSuggestion(dir) {
    const list = this.assist.queue();
    if (!list.length) return;
    let e;
    const cur = this.selEdit;
    const t = cur ? cur.start : this.player.time;
    if (dir > 0) {
      const later = list.filter(x => x !== cur && x.start >= t);
      e = later.find(x => !x.reviewed) ?? list.find(x => !x.reviewed && x !== cur);
      if (!e) {
        if (this.assist.filter === "main") this.toast("Все подсказки проверены. Можно собирать видео.", { kind: "ok" });
        e = later[0];
      }
    } else {
      e = [...list].reverse().find(x => x !== cur && x.start <= t);
    }
    if (e) this.select({ edit: e }, { seek: true, listen: true });
  }

  blade() {
    const t = this.player.time;
    const r = this.model.blade(t);
    this.timeline.dirty = true;
    this.hint(r === "cut" ? "Правка разрезана на две: одну часть можно вернуть, <kbd>X</kbd>"
      : r === "removed" ? "Разрез убран"
        : "Разрез поставлен. Кликните по фрагменту и нажмите <kbd>⌫</kbd>, чтобы его вырезать");
  }

  setEdgeToPlayhead(side) {
    const e = this.selEdit;
    if (!e) { this.hint("Сначала выберите правку"); return; }
    this.model.moveEdge(e, side, this.player.time);
  }

  afterTrim(e, side) {
    const t = side === "start" ? e.start : e.end;
    this.seek(t);
    this.transcript.update();
    this.assist.render();
    this.assist.scrollToSelected();
  }

  // ---------------------------------------------------------------- listening
  previewSource(e) {
    const p = this.player;
    this.seek(Math.max(this.model.T0, e.start - 1));
    p.play({ exempt: { a: e.start, b: e.end }, stopAt: e.end + 1 });
  }

  previewResult(e) {
    const m = this.model;
    const g = m.groupAt((e.start + e.end) / 2);
    const a = g ? g.a : e.start, b = g ? g.b : e.end;
    this.seek(Math.max(m.T0, a - 2));
    this.player.play({ forceSkip: true, stopAt: b + 2 });
  }

  /** What is around the playhead: the selected edit, the range, the clip, or the current cut. */
  listen(result) {
    const r = this.range;
    if (r) {
      this.seek(r.a);
      this.player.play({ exempt: r, stopAt: r.b });
      return;
    }
    if (this.selClip) {
      this.seek(this.selClip.a);
      this.player.play({ stopAt: this.selClip.b });
      return;
    }
    let e = this.selEdit;
    if (!e) {
      const g = this.model.groupFrom(this.player.time);
      e = g?.main;
    }
    if (!e) return;
    if (result) this.previewResult(e); else this.previewSource(e);
  }

  setAutoListen(v) {
    this.listenOnSelect = v;
    store.set("autolisten", v);
  }

  _effectiveSkip() { return this.timeline.mode === "result" || this.skipOn; }

  setSkip(v) {
    this.skipOn = v;
    store.set("skip", v);
    this.player.skip = this._effectiveSkip();
    $("#btnSkip").classList.toggle("active", this.player.skip);
    $("#btnSkip").disabled = this.timeline.mode === "result";
  }

  setMode(mode) {
    this.timeline.setMode(mode);
    for (const b of document.querySelectorAll("#viewSeg button")) b.classList.toggle("active", b.dataset.mode === mode);
    this.setSkip(this.skipOn);
    document.body.dataset.view = mode;
    store.set(`mode:${R.project}`, mode);
    this._renderClock(this.player.time);
    this.hint(mode === "result" ? "Так выглядит готовое видео: вырезанное убрано, стыки отмечены линиями" : null);
  }

  // ---------------------------------------------------------------- hints and toasts
  hint(html) { $("#tlHint").innerHTML = html ?? DEFAULT_HINT; }

  toast(text, { kind = "", sticky = false, undo = false, action = null } = {}) {
    const box = $("#toasts");
    const t = document.createElement("div");
    t.className = "toast " + kind;
    t.innerHTML = `<span>${esc(text)}</span>`;
    const addBtn = (label, fn) => {
      const b = document.createElement("button");
      b.textContent = label;
      b.onclick = () => { fn(); t.remove(); };
      t.appendChild(b);
    };
    if (undo) addBtn("Отменить", () => this.undo());
    if (action) addBtn(action.label, action.fn);
    if (sticky) addBtn("✕", () => {});
    box.appendChild(t);
    while (box.children.length > 3) box.firstChild.remove();
    if (!sticky) setTimeout(() => t.remove(), 4500);
  }

  undo() {
    const label = this.model.undo();
    if (label) this.hint(`Отменено: ${esc(label)}`);
  }

  redo() {
    const label = this.model.redo();
    if (label) this.hint(`Повторено: ${esc(label)}`);
  }

  _maybeShowWelcome() {
    const box = $("#welcome");
    if (store.get("welcomed", false)) { box.style.display = "none"; return; }
    box.style.display = "";
    box.querySelector("[data-a=ok]").onclick = () => { store.set("welcomed", true); box.style.display = "none"; };
    box.querySelector("[data-a=keys]").onclick = () => this.showHelp();
  }

  showHelp() { $("#helpDlg").showModal(); }

  // ---------------------------------------------------------------- controls
  _bindControls() {
    const tl = this.timeline;
    const p = this.player;
    $("#btnPlay").onclick = () => p.toggle();
    $("#btnPrevEdit").onclick = () => this.seek(this.model.editPoint(p.time, -1));
    $("#btnNextEdit").onclick = () => this.seek(this.model.editPoint(p.time, 1));
    $("#btnStepB").onclick = () => p.step(-1);
    $("#btnStepF").onclick = () => p.step(1);
    $("#btnSkip").onclick = () => this.setSkip(!this.skipOn);
    $("#rateBtn").onclick = () => {
      const all = [1, 1.25, 1.5, 2];
      const r = all[(all.indexOf(p.baseRate) + 1) % all.length];
      p.setBaseRate(r);
      store.set("rate", r);
      $("#rateBtn").textContent = fmtRate(r);
    };
    $("#volume").oninput = ev => { p.setVolume(+ev.target.value); store.set("volume", +ev.target.value); };
    $("#btnUndo").onclick = () => this.undo();
    $("#btnRedo").onclick = () => this.redo();
    $("#btnBlade").onclick = () => this.blade();
    $("#btnIn").onclick = () => this.markIn();
    $("#btnOut").onclick = () => this.markOut();
    $("#btnSnap").onclick = () => {
      this.snapOn = !this.snapOn;
      store.set("snap", this.snapOn);
      $("#btnSnap").classList.toggle("active", this.snapOn);
      this.hint(this.snapOn ? "Прилипание включено: границы встают на паузы между словами" : "Прилипание выключено");
    };
    for (const b of document.querySelectorAll("#viewSeg button")) b.onclick = () => this.setMode(b.dataset.mode);
    $("#btnZoomIn").onclick = () => tl.zoomBy(1.5);
    $("#btnZoomOut").onclick = () => tl.zoomBy(1 / 1.5);
    $("#btnFit").onclick = () => tl.zoomFit();
    $("#zoom").oninput = ev => { tl.zoomPos = +ev.target.value / 1000; };
    $("#selBar").onclick = ev => {
      const a = ev.target.closest("[data-sel]")?.dataset.sel;
      if (a === "cut" || a === "cutclip") this.cutSelection();
      else if (a === "restore") this.restoreSelection();
      else if (a === "hear" || a === "hearclip") this.listen(false);
      else if (a === "clear") this.clearMarks();
    };
    $("#btnHelp").onclick = () => this.showHelp();
    $("#helpClose").onclick = () => $("#helpDlg").close();
    $("#vWrap").onclick = () => p.toggle();
    $("#btnAi").onclick = () => this.runAi();
    $("#btnExport").onclick = ev => { ev.stopPropagation(); this._toggleExport(); };
    $("#btnMp4").onclick = () => { this._toggleExport(false); this.exportMp4(); };
    $("#btnXml").onclick = () => { this._toggleExport(false); this.exportXml(); };
    $("#btnReset").onclick = () => {
      this._toggleExport(false);
      if (confirm("Вернуть исходные предложения и удалить ручные вырезки?")) {
        this.model.resetAll();
        this.toast("Всё как было", { undo: true });
      }
    };
    $("#btnReport").onclick = () => { this._toggleExport(false); this.report(); };
    document.addEventListener("click", ev => {
      this._toggleExport(false);
      // a clicked button must not keep the focus: the next Space is for the player, not for it
      ev.target.closest?.("button")?.blur();
    });
    $("#exportMenu").onclick = ev => ev.stopPropagation();
    // the page must not turn into the dropped file
    document.addEventListener("dragover", e => e.preventDefault());
    document.addEventListener("drop", e => e.preventDefault());
  }

  _toggleExport(show) {
    const m = $("#exportMenu");
    const on = show ?? m.hidden;
    m.hidden = !on;
    if (on) {
      const s = this.model.stats();
      $("#exportInfo").innerHTML = `<b>${fmtClock(s.out)}</b> вместо ${fmtClock(s.total)} · ${s.splices} ${plural(s.splices, "стык", "стыка", "стыков")}`;
    }
  }

  _bindVideoErrors() {
    const v = $("#vA");
    const fail = () => {
      const box = $("#vError");
      box.style.display = "";
      box.textContent = this.data.project.exists === false
        ? "Видео не найдено по старому пути. Укажите новое расположение на странице уроков."
        : "Не удалось открыть видео в этом окне: возможно, формат не поддерживается. Монтаж и экспорт всё равно работают.";
    };
    v.addEventListener("error", fail);
    if (this.data.project.exists === false) fail();
  }

  onZoom() {
    if (this.timeline) $("#zoom").value = Math.round(this.timeline.zoomPos * 1000);
  }

  _bindSplitters() {
    const root = document.documentElement;
    const saved = store.get("layout", {});
    for (const [k, v] of Object.entries(saved)) root.style.setProperty(k, v + "px");
    for (const el of document.querySelectorAll(".split")) {
      const kind = el.dataset.split;
      el.addEventListener("pointerdown", ev => {
        el.setPointerCapture(ev.pointerId);
        document.body.classList.add("resizing");
        const start = kind === "t" ? ev.clientY : ev.clientX;
        const name = { l: "--left-w", r: "--right-w", t: "--tl-h" }[kind];
        const box = { l: "#panelText", r: "#panelAssist", t: "#timeline" }[kind];
        const size = document.querySelector(box).getBoundingClientRect();
        const initial = kind === "t" ? size.height : size.width;
        const sign = kind === "l" ? 1 : -1;
        const limits = { l: [220, 620], r: [280, 640], t: [190, innerHeight - 260] }[kind];
        const move = e => {
          const d = ((kind === "t" ? e.clientY : e.clientX) - start) * sign;
          const v = Math.min(limits[1], Math.max(limits[0], initial + d));
          root.style.setProperty(name, v + "px");
          saved[name] = v;
        };
        const up = () => {
          document.body.classList.remove("resizing");
          el.removeEventListener("pointermove", move);
          el.removeEventListener("pointerup", up);
          store.set("layout", saved);
        };
        el.addEventListener("pointermove", move);
        el.addEventListener("pointerup", up);
      });
    }
    // a narrow window shows one side panel at a time
    const tabs = document.querySelectorAll("#sideTabs button");
    for (const b of tabs) {
      b.onclick = () => {
        document.body.dataset.side = b.dataset.side;
        for (const x of tabs) x.classList.toggle("active", x === b);
      };
    }
    document.body.dataset.side = "assist";
  }

  // ---------------------------------------------------------------- keyboard
  _bindKeys() {
    document.addEventListener("keydown", ev => {
      const tag = ev.target.tagName;
      if ((tag === "INPUT" && ev.target.type !== "range") || tag === "TEXTAREA" || tag === "SELECT") return;
      if ($("#helpDlg").open) return;
      if (ev.key === "?") { ev.preventDefault(); this.showHelp(); return; }  // on a Russian layout it is ⇧7
      const mod = ev.metaKey || ev.ctrlKey;
      const p = this.player;
      const tl = this.timeline;
      const done = () => ev.preventDefault();
      // physical keys (ev.code): the shortcuts stay where they are with a Russian layout
      if (mod) {
        switch (ev.code) {
          case "KeyZ": done(); if (ev.shiftKey) this.redo(); else this.undo(); return;
          case "KeyB": done(); this.blade(); return;
          case "Equal": case "NumpadAdd": done(); tl.zoomBy(1.5); return;
          case "Minus": case "NumpadSubtract": done(); tl.zoomBy(1 / 1.5); return;
          case "Digit0": done(); tl.zoomFit(); return;
          case "KeyE": done(); this._toggleExport(); return;
          case "KeyF": done(); document.body.dataset.side = "text"; $("#textSearch").focus(); return;
          case "KeyS": done(); this.flush(); return;
        }
        return;
      }
      if (ev.altKey && ev.code !== "ArrowLeft" && ev.code !== "ArrowRight") return;
      switch (ev.code) {
        case "Space": done(); p.toggle(); break;
        case "KeyK": p.pause(); break;
        case "KeyJ": p.shuttle(-1); break;
        case "KeyL": p.shuttle(1); break;
        case "ArrowLeft":
          done();
          if (ev.altKey) this._stepWord(-1);
          else if (ev.shiftKey) this.seek(p.time - 1, { coalesce: true });
          else p.step(-1);
          break;
        case "ArrowRight":
          done();
          if (ev.altKey) this._stepWord(1);
          else if (ev.shiftKey) this.seek(p.time + 1, { coalesce: true });
          else p.step(1);
          break;
        case "ArrowUp": done(); this.seek(this.model.editPoint(p.time, -1)); tl.reveal(this.player.time); break;
        case "ArrowDown": done(); this.seek(this.model.editPoint(p.time, 1)); tl.reveal(this.player.time); break;
        case "Home": done(); this.seek(this.model.T0); tl.reveal(this.model.T0); break;
        case "End": done(); this.seek(this.model.T1); tl.reveal(this.model.T1); break;
        case "Tab": done(); this.nextSuggestion(ev.shiftKey ? -1 : 1); break;
        case "Enter": done(); this.confirmAndNext(); break;
        case "KeyX": this.toggleSelected(); break;
        case "Backspace": case "Delete":
          done();
          if (ev.shiftKey) this.restoreSelection(); else this.cutSelection();
          break;
        case "KeyS": this.blade(); break;
        case "KeyI": this.markIn(); break;
        case "KeyO": this.markOut(); break;
        case "KeyP": this.listen(!ev.shiftKey); break;
        case "Slash":
          done();
          this.listen(false);
          break;
        case "KeyN": $("#btnSnap").click(); break;
        case "KeyR": this.setMode(tl.mode === "source" ? "result" : "source"); break;
        case "KeyZ":
          if (ev.shiftKey) tl.zoomFit();
          else if (this.range) tl.zoomToSpan(this.range.a, this.range.b);
          else if (this.selEdit) tl.zoomToSpan(this.selEdit.start - 1, this.selEdit.end + 1);
          else if (this.selClip) tl.zoomToSpan(this.selClip.a, this.selClip.b);
          else tl.zoomBy(2);
          break;
        case "BracketLeft": this.setEdgeToPlayhead("start"); break;
        case "BracketRight": this.setEdgeToPlayhead("end"); break;
        case "Escape":
          if (this.range || this.marks.i != null || this.marks.o != null) this.clearMarks();
          else this.select(null, { scroll: false });
          break;
        default: return;
      }
    });
  }

  _stepWord(dir) {
    const m = this.model;
    const t = this.player.time;
    const i = m.wordIndexAt(t + (dir > 0 ? 0.001 : -0.05));
    const w = m.words[Math.max(0, Math.min(m.words.length - 1, dir > 0 ? i + 1 : i))];
    if (w) this.seek(w.s);
  }

  // ---------------------------------------------------------------- AI, export, reports
  _renderAi() {
    const d = this.data;
    const L = d.llm;
    const b = $("#btnAi");
    const info = $("#aiInfo");
    const err = d.project.llm_error;
    if (!d.llm_ready) {
      b.disabled = false;
      b.textContent = "✦ Подключить AI-проверку";
      b.onclick = () => { location.href = "/?settings=1"; };
      info.textContent = "Модель прочитает текст урока, проверит подсказки и поищет пропущенное. Видео никуда не уходит.";
    } else {
      b.textContent = L ? "✦ Проверить ещё раз" : "✦ Проверить с AI";
      b.onclick = () => this.runAi();
      b.title = `Модель: ${d.llm_model}. Ваши решения сохранятся.`;
      info.innerHTML = L
        ? `Проверено: ${esc((L.model || "").split("/").pop())}, ${esc(L.at || "")}. Мнений: ${L.verdicts}, новых находок: ${L.new}.`
        : "Модель прочитает текст урока, проверит подсказки и поищет пропущенное. Видео никуда не уходит.";
    }
    if (err) info.innerHTML += ` <span class="err">Последний запуск не удался: ${esc(err)}</span>`;
  }

  async runAi() {
    await this.flush();
    try {
      await api(`/api/projects/${R.project}/llm`, { method: "POST", body: {} });
      this.pollJobs(true);
    } catch (e) { this.toast(e.message, { kind: "err", sticky: true }); }
  }

  async exportMp4() {
    await this.flush();
    try {
      await api(`/api/projects/${R.project}/render`, { method: "POST", body: { format: "mp4" } });
      this.pollJobs(true);
    } catch (e) { this.toast(e.message, { kind: "err", sticky: true }); }
  }

  async exportXml() {
    await this.flush();
    try {
      const r = await api(`/api/projects/${R.project}/render`, { method: "POST", body: { format: "fcpxml" } });
      this.toast(`Таймлайн сохранён: ${r.output.split(/[\\/]/).pop()}`, {
        kind: "ok", sticky: true,
        action: { label: "Показать в папке", fn: () => api(`/api/projects/${R.project}/reveal`, { method: "POST", body: { path: r.output } }) },
      });
    } catch (e) { this.toast(e.message, { kind: "err", sticky: true }); }
  }

  async report() {
    if (!confirm("Будет создан zip-файл для разработчика: журнал программы, сведения о системе, найденные правки по этому уроку (с текстом расшифровки) и ваши решения. Видео в него не попадёт.")) return;
    await this.flush();
    try {
      const r = await api("/api/report", { method: "POST", body: { project: R.project } });
      this.toast(`Отчёт сохранён: ${r.path.split(/[\\/]/).pop()}`, { kind: "ok" });
    } catch (e) { this.toast(e.message, { kind: "err", sticky: true }); }
  }

  /** seen: a job was running while we watched; a job that ended earlier is not news. */
  async pollJobs(seen) {
    clearTimeout(this._pollTimer);
    let p;
    try { p = await api(`/api/projects/${R.project}`); } catch {
      this._pollTimer = setTimeout(() => this.pollJobs(seen), 2000);
      return;
    }
    const st = p.status || {};
    const pill = $("#jobPill");
    const busy = ["queued", "running"].includes(st.state);
    $("#btnMp4").disabled = busy;
    $("#btnXml").disabled = busy;
    $("#btnAi").disabled = busy && st.job === "llm";
    if (busy) {
      pill.hidden = false;
      const pct = st.progress == null ? null : Math.round(st.progress * 100);
      $("#jobText").textContent = `${JOB[st.job] || "Обработка"}${pct != null ? ` ${pct}%` : ""}`;
      pill.title = st.message || "";
      const bar = $("#jobBar");
      bar.classList.toggle("indet", pct == null);
      bar.firstElementChild.style.width = (pct ?? 30) + "%";
      $("#jobCancel").onclick = () => api(`/api/projects/${R.project}/cancel`, { method: "POST", body: {} }).catch(() => {});
      this._pollTimer = setTimeout(() => this.pollJobs(true), 1000);
      return;
    }
    pill.hidden = true;
    if (seen) this._afterJob(st, p);
  }

  async _afterJob(st, p) {
    const name = JOB[st.job] || "Задача";
    if (st.state === "error") this.toast(`${name}: ошибка. ${st.error}`, { kind: "err", sticky: true });
    else if (st.state === "cancelled") this.toast(`${name}: отменено`);
    else if (st.job === "render" && st.state === "done") {
      this.toast(`Готово: ${(p.output || "").split(/[\\/]/).pop()}`, {
        kind: "ok", sticky: true,
        action: { label: "Показать в папке", fn: () => api(`/api/projects/${R.project}/reveal`, { method: "POST", body: {} }) },
      });
    }
    if (st.job === "llm" || st.job === "detect") {
      // new suggestions came in: start over with fresh data, the decisions are on the server
      location.reload();
    }
  }

  async _loadThumbs() {
    try {
      const st = await api(`/api/projects/${R.project}/thumbs`);
      if (st.state === "ready") this.timeline.setThumbs(st, this.data.thumbs_url);
      else if (st.state === "working") setTimeout(() => this._loadThumbs(), 1500);
    } catch { /* the video lane stays plain */ }
  }
}

const fmtRate = r => `${String(r).replace(".", ",")}×`;
const plural = (n, one, few, many) => {
  const a = n % 100, b = n % 10;
  if (a > 10 && a < 20) return many;
  if (b === 1) return one;
  return b >= 2 && b <= 4 ? few : many;
};

async function boot() {
  let data;
  try {
    data = await api(`/api/projects/${R.project}/review`);
  } catch (e) {
    $("#loading").textContent = `Не удалось открыть урок: ${e.message}`;
    return;
  }
  document.title = `${data.project.name} — Retake`;
  $("#title").textContent = data.project.name;
  $("#loading").style.display = "none";
  document.body.classList.add("ready");
  const app = new App(data, null);
  window.retake = app;  // for the browser console and end-to-end tests
  const mode = store.get(`mode:${R.project}`, "source");
  if (mode === "result") app.setMode("result");
  // the waveform comes separately: the page is usable before it arrives
  try {
    const r = await fetch(`/api/projects/${R.project}/peaks`, { headers: { "X-Retake-Token": R.token } });
    if (r.ok) {
      app.model.setEnv(new Uint8Array(await r.arrayBuffer()));
      app.timeline.invalidate();
    }
  } catch { /* no waveform, the rest still works */ }
}

boot();

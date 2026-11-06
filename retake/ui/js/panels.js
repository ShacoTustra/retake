// The two side panels: the transcript (edit by selecting text) and the suggestions (the review queue).

import { KINDS, fmtClock, fmtDur, fmtTC, isAiFocus, isMain, isMinor, isPause } from "./model.js";

const CONF = { high: "уверена", medium: "скорее да", low: "сомневается" };
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

// ---------------------------------------------------------------- transcript

export class Transcript {
  constructor(app) {
    this.app = app;
    this.m = app.model;
    this.box = document.querySelector("#textBody");
    this.search = document.querySelector("#textSearch");
    this.count = document.querySelector("#textCount");
    this.wordEls = [];
    this.gapEls = new Map();
    this.now = -1;
    this.matches = [];
    this.matchPos = -1;
    this.autoScrollPause = 0;
    this._build();
    this.update();
    this.box.addEventListener("click", ev => this._click(ev));
    this.box.addEventListener("mouseup", () => this._selectionDone());
    // reading ahead: do not pull the view back to the playing word for a while
    this.box.addEventListener("wheel", () => { this.autoScrollPause = performance.now() + 4000; }, { passive: true });
    this.search.addEventListener("input", () => this._find());
    this.search.addEventListener("keydown", ev => {
      if (ev.key === "Enter") { ev.preventDefault(); this._nextMatch(ev.shiftKey ? -1 : 1); }
      if (ev.key === "Escape") { this.search.value = ""; this._find(); this.search.blur(); }
      ev.stopPropagation();
    });
  }

  _build() {
    const { m, box } = this;
    box.textContent = "";
    this.wordEls = [];
    this.gapEls.clear();
    const pauses = m.edits.filter(isPause).sort((a, b) => a.start - b.start);
    let pi = 0, p = null, prevEnd = -1e9;
    const addGap = (e, parent) => {
      const g = document.createElement("button");
      g.className = "gap";
      g.dataset.id = e.id;
      g.title = "Пауза. Клик — вырезать или оставить";
      g.textContent = (e.end - e.start).toFixed(1);
      this.gapEls.set(e.id, g);
      parent.appendChild(g);
    };
    const frag = document.createDocumentFragment();
    for (const ph of m.phrases) {
      if (!p || ph.start - prevEnd > 2.0) {
        p = document.createElement("p");
        const t = document.createElement("button");
        t.className = "t";
        t.textContent = fmtTC(ph.start).replace(/\.\d$/, "");
        t.dataset.t = ph.start;
        p.appendChild(t);
        frag.appendChild(p);
      }
      for (let i = ph.w0; i < ph.w1; i++) {
        const w = m.words[i];
        while (pi < pauses.length && pauses[pi].end <= w.s + 0.01) addGap(pauses[pi++], p);
        const s = document.createElement("span");
        s.className = "w";
        s.textContent = w.t;
        s.dataset.i = i;
        this.wordEls[i] = s;
        p.appendChild(s);
      }
      prevEnd = ph.end;
    }
    while (p && pi < pauses.length) addGap(pauses[pi++], p);
    box.appendChild(frag);
  }

  /** Update on the next frame: selecting a range changes this on every mouse move. */
  invalidate() {
    if (this._pending) return;
    this._pending = true;
    requestAnimationFrame(() => { this._pending = false; this.update(); });
  }

  /** Paint the words: what is cut, what is only suggested, what belongs to the selected edit. */
  update() {
    const { m, wordEls } = this;
    const state = new Array(m.words.length);  // edit that governs each word
    const sugg = m.edits.filter(e => !isPause(e)).sort((a, b) => (b.end - b.start) - (a.end - a.start));
    for (const e of sugg) {
      const [i0, i1] = m.wordsBetween(e.start, e.end);
      for (let i = i0; i < i1; i++) state[i] = e;
    }
    const sel = this.app.selEdit;
    const { i: mi, o: mo } = this.app.marks;
    for (let i = 0; i < wordEls.length; i++) {
      const el = wordEls[i];
      if (!el) continue;
      const w = m.words[i], mid = (w.s + w.e) / 2;
      const g = m.groupAt(mid);
      const e = state[i];
      let cls = "w";
      if (g) cls += " cut";
      else if (e && !e.on) cls += " sugg";
      if (sel && mid >= sel.start && mid <= sel.end) cls += " focus";
      if (mi != null && mo != null && mid >= mi && mid <= mo) cls += " insel";
      if (i === this.now) cls += " now";
      if (this.matchSet?.has(i)) cls += " hit";
      if (el.className !== cls) el.className = cls;
      const color = g ? KINDS[g.main.kind].color : e ? KINDS[e.kind].color : null;
      if (el._color !== color) {
        el._color = color;
        if (color) el.style.setProperty("--c", `var(${color})`);
        else el.style.removeProperty("--c");
      }
    }
    for (const e of m.edits) {
      const g = this.gapEls.get(e.id);
      if (g) g.classList.toggle("on", e.on);
    }
  }

  setTime(t, playing) {
    const i = this.m.wordIndexAt(t);
    const idx = i >= 0 && t <= this.m.words[i].e + 0.3 ? i : -1;
    if (idx === this.now) return;
    this.wordEls[this.now]?.classList.remove("now");
    this.now = idx;
    const el = this.wordEls[idx];
    if (!el) return;
    el.classList.add("now");
    if (playing && performance.now() > this.autoScrollPause) this._keepInView(el);
  }

  _keepInView(el) {
    const b = this.box.getBoundingClientRect(), r = el.getBoundingClientRect();
    if (r.top < b.top + 40 || r.bottom > b.bottom - 60) {
      this.box.scrollTo({ top: this.box.scrollTop + r.top - b.top - b.height / 3, behavior: "smooth" });
    }
  }

  reveal(t) {
    const el = this.wordEls[Math.max(0, this.m.wordIndexAt(t + 0.01))];
    if (el) this._keepInView(el);
  }

  _click(ev) {
    const gap = ev.target.closest(".gap");
    if (gap) {
      const e = this.m.byId.get(+gap.dataset.id);
      if (e) { this.m.setOn(e, !e.on); this.app.select({ edit: e }, { seek: true }); }
      return;
    }
    const t = ev.target.closest(".t");
    if (t) { this.app.seek(+t.dataset.t); return; }
    const sel = window.getSelection();
    if (sel && !sel.isCollapsed) return;  // the end of a text selection, not a click
    const w = ev.target.closest(".w");
    if (!w) return;
    const word = this.m.words[+w.dataset.i];
    this.app.clickWord(word);
  }

  /** Selecting text selects that span of the video; every word boundary is a clean cut point. */
  _selectionDone() {
    const sel = window.getSelection();
    if (!sel || sel.isCollapsed || !this.box.contains(sel.anchorNode)) return;
    const r = sel.getRangeAt(0);
    const first = this._boundaryWord(r.startContainer, r.startOffset, 1);
    const last = this._boundaryWord(r.endContainer, r.endOffset, -1);
    if (first == null || last == null || last < first) return;
    const i0 = first, i1 = last;
    sel.removeAllRanges();
    this.app.selectWords(i0, i1);
  }

  /** Index of the first (dir 1) or last (dir -1) word really inside a selection boundary.
   * A boundary at the far edge of a word's text does not select anything of that word. */
  _boundaryWord(node, offset, dir) {
    if (node.nodeType === 3) {
      const w = node.parentElement.closest(".w");
      if (w) {
        const text = node.data;
        const touches = dir > 0 ? text.slice(offset).trim() !== "" : text.slice(0, offset).trim() !== "";
        return +w.dataset.i + (touches ? 0 : dir);
      }
    }
    // between words: a pause chip, a time label, the end of a paragraph
    const start = node.nodeType === 3 ? node.parentElement : (node.childNodes[offset] || node);
    if (start.classList?.contains("w")) return +start.dataset.i + (dir > 0 ? 0 : -1);
    const walker = document.createTreeWalker(this.box, NodeFilter.SHOW_ELEMENT);
    walker.currentNode = start;
    while (dir > 0 ? walker.nextNode() : walker.previousNode()) {
      if (walker.currentNode.classList.contains("w")) return +walker.currentNode.dataset.i;
    }
    return null;
  }

  // ---- search
  _find() {
    const q = this.search.value.trim().toLowerCase().replace(/ё/g, "е");
    this.matches = [];
    this.matchSet = null;
    if (q.length >= 2) {
      // words carry their leading space: build the text once, map positions back to words
      if (!this._text) {
        this._starts = [];
        let s = "";
        for (const w of this.m.words) {
          this._starts.push(s.length);
          s += w.t.toLowerCase().replace(/ё/g, "е");
        }
        this._text = s;
      }
      this.matchSet = new Set();
      let pos = this._text.indexOf(q);
      while (pos >= 0) {
        let lo = 0, hi = this._starts.length - 1;
        while (lo < hi) {
          const mid = (lo + hi + 1) >> 1;
          if (this._starts[mid] <= pos) lo = mid; else hi = mid - 1;
        }
        this.matches.push(lo);
        let k = lo;
        while (k < this._starts.length && this._starts[k] < pos + q.length) this.matchSet.add(k++);
        pos = this._text.indexOf(q, pos + q.length);
      }
    }
    this.matchPos = -1;
    this.count.textContent = q.length >= 2 ? (this.matches.length ? `${this.matches.length}` : "нет") : "";
    this.update();
    if (this.matches.length) this._nextMatch(1);
  }

  _nextMatch(dir) {
    if (!this.matches.length) return;
    this.matchPos = (this.matchPos + dir + this.matches.length) % this.matches.length;
    const i = this.matches[this.matchPos];
    this.count.textContent = `${this.matchPos + 1} из ${this.matches.length}`;
    const el = this.wordEls[i];
    if (el) {
      this.autoScrollPause = performance.now() + 4000;
      this._keepInView(el);
    }
    this.app.seek(this.m.words[i].s);
  }
}

// ---------------------------------------------------------------- suggestions

export class Assist {
  constructor(app) {
    this.app = app;
    this.m = app.model;
    this.list = document.querySelector("#assistList");
    this.scroller = document.querySelector("#assistScroll");
    this.tabs = document.querySelector("#assistTabs");
    this.bulk = document.querySelector("#assistBulk");
    this.progress = document.querySelector("#assistProgress");
    this.filter = "main";
    this.aiOnly = false;
    this.tabs.addEventListener("click", ev => {
      const b = ev.target.closest("[data-f]");
      if (!b) return;
      this.filter = b.dataset.f;
      this.aiOnly = false;
      this.render();
      this.scroller.scrollTop = 0;
    });
    this.list.addEventListener("click", ev => {
      if (ev.target.closest("[data-bulk=export]")) { ev.stopPropagation(); this.app._toggleExport(true); return; }
      this._click(ev);
    });
    this.bulk.addEventListener("change", ev => {
      if (ev.target.id === "autoListen") this.app.setAutoListen(ev.target.checked);
    });
    this.bulk.addEventListener("click", ev => {
      const b = ev.target.closest("[data-bulk]");
      if (!b) return;
      if (b.dataset.bulk === "ai") { this.aiOnly = !this.aiOnly; this.render(); return; }
      const on = b.dataset.bulk === "on";
      const list = this.items();
      this.m.setMany(list, on, on ? "вырезать все" : "оставить все");
    });
  }

  items() {
    const f = this.filter;
    const pick = f === "main" ? isMain : f === "minor" ? isMinor : f === "pause" ? isPause : () => true;
    return this.m.edits.filter(e => pick(e) && (!this.aiOnly || isAiFocus(e))).sort((a, b) => a.start - b.start);
  }

  /** The queue that Tab walks: things somebody has to decide on. */
  queue() {
    return this.items();
  }

  render() {
    const { m } = this;
    const count = pred => m.edits.filter(pred).length;
    const tabs = [["main", "Важное", count(isMain)], ["minor", "Запинки", count(isMinor)], ["pause", "Паузы", count(isPause)], ["all", "Всё", m.edits.length]];
    this.tabs.innerHTML = tabs.map(([f, name, n]) => `<button data-f="${f}" class="${this.filter === f ? "active" : ""}">${name}<i>${n}</i></button>`).join("");

    const main = m.edits.filter(e => isMain(e) && e.kind !== "manual");
    const done = main.filter(e => e.reviewed).length;
    const pct = main.length ? done / main.length * 100 : 100;
    this.progress.innerHTML = main.length
      ? `<div class="pbar"><i style="width:${pct}%"></i></div><span>${done === main.length ? "Всё проверено" : `Проверено ${done} из ${main.length}`}</span>`
      : "";

    const items = this.items();
    const nAi = m.edits.filter(isAiFocus).length;
    const onCount = items.filter(e => e.on).length;
    const cut = items.filter(e => e.on).reduce((s, e) => s + e.end - e.start, 0);
    let bulk = "";
    if (nAi && this.filter !== "pause" && this.filter !== "minor") {
      bulk += `<button data-bulk="ai" class="chip ai ${this.aiOnly ? "active" : ""}" title="Только места, где модель не согласна с правилами, и её собственные находки">✦ Спорные и новые <i>${nAi}</i></button>`;
    }
    if (this.filter !== "main" && items.length) {
      bulk += `<span class="sum">${onCount} из ${items.length}${cut ? ` · −${fmtDur(cut)}` : ""}</span>`;
      bulk += `<span class="sp"></span><button data-bulk="on" class="mini">Вырезать все</button><button data-bulk="off" class="mini">Оставить все</button>`;
    } else {
      bulk += `<span class="sp"></span><label class="auto" title="Открывая подсказку, сразу включать это место"><input type="checkbox" id="autoListen" ${this.app.listenOnSelect ? "checked" : ""}>Сразу слушать</label>`;
    }
    this.bulk.innerHTML = bulk;

    const allDone = this.filter === "main" && main.length > 0 && done === main.length;
    const banner = allDone
      ? `<div class="alldone"><b>Все подсказки проверены</b><span>Итог: ${fmtClock(m.outDuration)}. Посмотрите результат или соберите видео.</span><button data-bulk="export" class="primary">Экспорт</button></div>`
      : "";
    const sel = this.app.selEdit;
    this.list.innerHTML = banner + (items.length
      ? items.map(e => this._card(e, e === sel)).join("")
      : `<div class="empty">${this.aiOnly ? "Модель ни с чем не спорит." : this.filter === "main" ? "Важных подсказок нет. Вырежьте лишнее сами: выделите кусок на дорожке или текст слева." : "Здесь пусто."}</div>`);
  }

  _card(e, selected) {
    const { m } = this;
    const dur = e.end - e.start;
    const changed = e.start !== e.orig_start || e.end !== e.orig_end || e.on !== e.default_on;
    const text = isPause(e) ? "" : m.textBetween(e.start, e.end);
    let ai = "";
    if (e.llm) {
      const L = e.llm;
      const dis = e.disputed ? `<span class="tag warn" title="Правило предлагало ${e.rule_on ? "вырезать" : "оставить"}">спорно</span>` : "";
      const verdict = e.src === "llm" ? "нашла модель" : L.cut ? "вырезать" : "оставить";
      ai = `<div class="ai"><span class="tag ai">✦ ${verdict}</span>${dis}<span class="conf">${CONF[L.confidence] || ""}</span> ${esc(L.reason)}</div>`;
    } else if (e.why && e.src !== "llm" && !isPause(e)) {
      ai = `<div class="why">${esc(e.why)}</div>`;
    }
    const detail = selected ? `
      <div class="detail">
        <div class="joint"><span class="dim">${esc(m.textBefore(e.start))}</span> <b class="seam">✂</b> <span class="dim">${esc(m.textAfter(e.end))}</span></div>
        <div class="nudges">
          <div class="nudge"><span>Начало</span><button data-act="nudge" data-side="start" data-d="-0.1" title="На 0,1 с раньше">−</button><code>${fmtTC(e.start)}</code><button data-act="nudge" data-side="start" data-d="0.1" title="На 0,1 с позже">+</button></div>
          <div class="nudge"><span>Конец</span><button data-act="nudge" data-side="end" data-d="-0.1" title="На 0,1 с раньше">−</button><code>${fmtTC(e.end)}</code><button data-act="nudge" data-side="end" data-d="0.1" title="На 0,1 с позже">+</button></div>
        </div>
        <div class="acts">
          <button data-act="hear-result" title="Как прозвучит стык (P)">▶ Как будет</button>
          <button data-act="hear-source" title="Что вырезаем, с запасом (⇧P)">▶ Что вырезаем</button>
          ${changed && e.kind !== "manual" ? `<button data-act="reset" class="link">Вернуть как было</button>` : ""}
        </div>
        <div class="keys"><kbd>X</kbd> ${e.on ? "оставить" : "вырезать"}<kbd>Enter</kbd> согласиться и дальше<kbd>P</kbd> стык</div>
      </div>` : "";
    return `<article class="card ${e.on ? "on" : "off"} ${selected ? "selected" : ""} ${e.reviewed ? "done" : ""}" data-id="${e.id}" style="--c:var(${KINDS[e.kind].color})">
      <div class="head">
        <span class="kind">${e.src === "llm" ? "✦ " : ""}${KINDS[e.kind].name}</span>
        <span class="time">${fmtTC(e.start).replace(/\.\d$/, "")}</span>
        <span class="dur">${e.on ? "−" : ""}${fmtDur(dur)}</span>
        <span class="sp"></span>
        ${e.reviewed && e.kind !== "manual" ? `<span class="tick" title="Проверено">✓</span>` : ""}
        <div class="seg" role="group">
          <button data-act="cut" class="${e.on ? "active cut" : ""}" title="Вырезать">Вырезать</button><button data-act="keep" class="${e.on ? "" : "active keep"}" title="Оставить">Оставить</button>
        </div>
      </div>
      ${ai}
      ${text ? `<div class="text ${selected ? "" : "clamp"}">${esc(text)}</div>` : ""}
      ${detail}
    </article>`;
  }

  _click(ev) {
    const card = ev.target.closest(".card");
    if (!card) return;
    const e = this.m.byId.get(+card.dataset.id);
    if (!e) return;
    const act = ev.target.closest("[data-act]")?.dataset.act;
    switch (act) {
      case "cut": this.m.setOn(e, true); this.app.select({ edit: e }, { seek: false }); break;
      case "keep": this.m.setOn(e, false); if (e.kind !== "manual") this.app.select({ edit: e }, { seek: false }); break;
      case "nudge": {
        const b = ev.target.closest("[data-act]");
        this.m.moveEdge(e, b.dataset.side, e[b.dataset.side] + +b.dataset.d);
        this.app.seek(e[b.dataset.side]);
        break;
      }
      case "hear-result": this.app.previewResult(e); break;
      case "hear-source": this.app.previewSource(e); break;
      case "reset": this.m.resetEdit(e); break;
      default:
        this.app.select({ edit: e }, { seek: true, listen: true });
    }
  }

  scrollToSelected() {
    const el = this.list.querySelector(".card.selected");
    if (el) el.scrollIntoView({ block: "nearest" });
  }
}

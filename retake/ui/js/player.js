// Playback of the source as if the cuts were already made.
//
// A seek in a long-GOP screen recording takes 50-300 ms, which is exactly what a reviewer must not
// hear at a splice. So there are two <video> elements over each other: the one in the back waits,
// already decoded, at the end of the next cut, and takes over at the moment of the splice.

const FRAME_BUDGET = 0.02;  // jump this early: the check runs once per screen refresh
const PREPARE_AHEAD = 6;    // seconds before a cut when the waiting video gets positioned

export class Player {
  constructor(a, b, model) {
    this.m = model;
    this.cur = a;
    this.alt = b;
    this.dual = true;
    this.skip = true;       // the cuts are jumped over
    this.forceSkip = false; // ... also during this one preview, whatever the setting is
    this.baseRate = 1;      // the speed chosen for watching
    this.rate = 1;          // the current one: J and L change it for a while
    this.volume = 1;
    this.muted = false;
    this.stopAt = null;     // a preview ends here
    this.exempt = null;     // {a, b}: this span plays even though it is cut
    this.altTarget = null;
    this.pending = null;    // a seek that waits for the previous one (scrubbing)
    this.reverse = 0;
    this.onJump = null;     // (group) => void
    this.onPlayError = null;
    this.blessed = false;
    for (const v of [a, b]) {
      v.preload = "auto";
      v.playsInline = true;
      v.addEventListener("seeked", () => this._seeked(v));
    }
    b.muted = true;
    a.classList.add("front");
  }

  load(url) { this.cur.src = url; this.alt.src = url; }

  get time() { return this.pending ?? this.cur.currentTime; }
  get paused() { return this.cur.paused && !this.reverse; }

  _seeked(v) {
    if (v === this.cur && this.pending != null) {
      const t = this.pending;
      this.pending = null;
      v.currentTime = t;
    }
  }

  seek(t, { coalesce = false } = {}) {
    t = Math.min(this.m.T1, Math.max(this.m.T0, t));
    this.stopAt = null;
    this.forceSkip = false;
    // going into a cut on purpose means "let me hear this", not "jump away from it"
    const g = this.m.groupAt(t);
    this.exempt = g ? { a: g.a, b: g.b } : null;
    if (coalesce && this.cur.seeking) { this.pending = t; return; }
    this.pending = null;
    this.cur.currentTime = t;
  }

  /** The browser only lets an element with sound start from a click if it was started by one before. */
  _bless() {
    if (this.blessed) return;
    this.blessed = true;
    const s = this.alt;
    s.muted = true;
    const p = s.play();
    if (p) p.then(() => s.pause()).catch(() => {});
  }

  play({ exempt, stopAt = null, forceSkip = false } = {}) {
    this._stopReverse();
    this._bless();
    this.stopAt = stopAt;
    this.forceSkip = forceSkip;
    if (forceSkip) this.exempt = null;
    else {
      const g = this.m.groupAt(this.time);
      this.exempt = exempt ?? (g ? { a: g.a, b: g.b } : null);
    }
    this.cur.muted = this.muted;
    this.cur.volume = this.volume;
    this.cur.playbackRate = this.rate;
    const r = this.cur.play();
    // a play() interrupted by a pause or a seek rejects with AbortError, which is normal
    return (r || Promise.resolve()).catch(e => { if (e.name !== "AbortError") this.onPlayError?.(e); });
  }

  pause() {
    this._stopReverse();
    this.cur.pause();
    this.stopAt = null;
    this.forceSkip = false;
    this.setRate(this.baseRate);
  }

  toggle() { if (this.paused) this.play(); else this.pause(); }

  setRate(r) {
    this.rate = r;
    this.cur.playbackRate = r;
  }

  setBaseRate(r) {
    this.baseRate = r;
    this.setRate(r);
  }

  setVolume(v) {
    this.volume = v;
    this.cur.volume = v;
  }

  setMuted(m) {
    this.muted = m;
    this.cur.muted = m;
  }

  step(dir) {
    this.pause();
    this.seek(this.time + dir / this.m.fps, { coalesce: true });
  }

  /** J and L: pressing again goes faster, the opposite key first slows down and stops. */
  shuttle(dir) {
    const speeds = [1, 1.5, 2, 3];
    if (dir > 0) {
      if (this.reverse) { this._stopReverse(); return; }
      if (this.paused) { this.play(); return; }
      this.setRate(speeds.find(s => s > this.rate) ?? this.rate);
    } else {
      if (!this.paused && !this.reverse) {
        const slower = speeds.filter(s => s < this.rate).pop();
        if (slower) this.setRate(slower);
        else this.pause();
        return;
      }
      this.reverse = Math.min(4, (this.reverse || 0.5) * 2);
      this._startReverse();
    }
  }

  // video cannot play backwards in most browsers: walk back in steps, as fast as seeks finish
  _startReverse() {
    this.cur.pause();
    clearInterval(this._revTimer);
    this._revTimer = setInterval(() => {
      if (this.cur.seeking) return;
      const t = this.cur.currentTime - 0.12 * this.reverse;
      if (t <= this.m.T0) { this._stopReverse(); this.cur.currentTime = this.m.T0; return; }
      this.cur.currentTime = t;
    }, 120);
  }

  _stopReverse() {
    this.reverse = 0;
    clearInterval(this._revTimer);
  }

  /** Called once per screen refresh. */
  tick() {
    const v = this.cur;
    if (v.paused) return;
    const t = v.currentTime;
    if (this.stopAt != null && t >= this.stopAt) { this.pause(); return; }
    if (t >= this.m.T1) { this.pause(); return; }
    if (this.exempt && t >= this.exempt.b) this.exempt = null;
    if (!this.skip && !this.forceSkip) return;
    const from = this.exempt ? Math.max(t, this.exempt.b) : t;
    const g = this.m.groupFrom(from + 0.001);
    if (!g) return;
    // a cut can reach past the exempt part (a retake merged with the pause after it): that rest still goes
    const lead = Math.max(g.a, from) - t;
    if (lead <= FRAME_BUDGET * this.rate) this._jump(g);
    else if (this.dual && lead < PREPARE_AHEAD * this.rate && this.altTarget !== g.b) {
      this.altTarget = g.b;
      this.alt.currentTime = g.b;
    }
  }

  _jump(g) {
    if (g.b >= this.m.T1 - 0.01) { this.pause(); return; }
    const s = this.alt;
    const ready = this.dual && !s.seeking && s.readyState >= 2 && Math.abs(s.currentTime - g.b) < 0.06;
    if (ready) this._swap(g.b);
    else this.cur.currentTime = g.b;
    this.onJump?.(g);
  }

  _swap(target) {
    const old = this.cur, nu = this.alt;
    nu.playbackRate = this.rate;
    nu.volume = this.volume;
    nu.muted = this.muted;
    const p = nu.play();
    this.cur = nu;
    this.alt = old;
    this.altTarget = null;
    nu.classList.add("front");
    old.classList.remove("front");
    old.muted = true;
    old.pause();
    if (p) {
      p.catch(() => {
        // the browser refused to start the second video: go on with one, and a small gap at splices
        this.dual = false;
        this.cur = old;
        this.alt = nu;
        nu.classList.remove("front");
        old.classList.add("front");
        nu.muted = true;
        old.muted = this.muted;
        old.currentTime = target;
        old.play().catch(() => {});
      });
    }
  }
}

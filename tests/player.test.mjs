// node --test tests/*.mjs
import assert from "node:assert/strict";
import { test } from "node:test";
import { Model } from "../retake/ui/js/model.js";
import { Player } from "../retake/ui/js/player.js";

class FakeVideo {
  currentTime = 0; paused = true; seeking = false; readyState = 4; muted = false; volume = 1; playbackRate = 1;
  classList = { add() {}, remove() {} };
  addEventListener() {}
  play() { this.paused = false; return Promise.resolve(); }
  pause() { this.paused = true; }
}

function setup(edits) {
  const m = new Model({ range: [0, 100], fps: 25, words: [], phrases: [], edits });
  const a = new FakeVideo(), b = new FakeVideo();
  const p = new Player(a, b, m);
  p.dual = false;  // one video: a jump is simply a new currentTime
  return { m, p };
}
const at = (p, t) => { p.cur.currentTime = t; p.tick(); return p.cur.currentTime; };
const retake = { id: 1, kind: "retake", start: 10, end: 20, on: true };
const pause = { id: 2, kind: "pause", start: 20, end: 25, on: true };

test("playback jumps over a cut", () => {
  const { p } = setup([retake]);
  p.seek(8);
  p.play();
  assert.equal(at(p, 9.5), 9.5);
  assert.equal(at(p, 9.99), 20);
});

test("hearing a retake merged with the pause after it plays the retake, then skips the rest", () => {
  const { m, p } = setup([retake, pause]);
  assert.deepEqual(m.groups.map(g => [g.a, g.b]), [[10, 25]]);
  p.seek(9);
  p.play({ exempt: { a: 10, b: 20 }, stopAt: 21 });
  assert.equal(at(p, 9.99), 9.99, "not at the start of the cut");
  assert.equal(at(p, 15), 15, "not in the middle");
  assert.equal(at(p, 20.01), 25, "the pause after it is still cut");
});

test("pressing play inside a cut plays that cut", () => {
  const { p } = setup([retake]);
  p.seek(15);
  p.play();
  assert.equal(at(p, 16), 16);
  assert.equal(at(p, 21), 21);
  assert.equal(p.exempt, null, "the permission ends with the cut");
});

test("clicking into a cut while playing plays it too", () => {
  const { p } = setup([retake]);
  p.seek(5);
  p.play();
  p.seek(12);
  assert.equal(at(p, 12), 12);
});

test("with skipping off nothing is jumped, a splice preview still jumps", () => {
  const { p } = setup([retake]);
  p.skip = false;
  p.seek(8);
  p.play();
  assert.equal(at(p, 10.5), 10.5);
  p.seek(8);
  p.play({ forceSkip: true, stopAt: 22 });
  assert.equal(at(p, 9.99), 20);
  assert.equal(at(p, 22.1), 22.1);
  assert.equal(p.paused, true, "the preview stops by itself");
  assert.equal(p.forceSkip, false, "and leaves the setting alone");
});

test("a cut to the very end stops playback instead of seeking past it", () => {
  const { p } = setup([{ id: 1, kind: "retake", start: 90, end: 100, on: true }]);
  p.seek(89);
  p.play();
  at(p, 89.99);
  assert.equal(p.paused, true);
});

test("the waiting video takes over at the splice", () => {
  const { p } = setup([retake]);
  p.dual = true;
  const first = p.cur, second = p.alt;
  p.seek(8);
  p.play();
  at(p, 8);
  assert.equal(second.currentTime, 20, "it waits at the end of the cut");
  first.currentTime = 9.99;
  p.tick();
  assert.equal(p.cur, second);
  assert.equal(second.paused, false);
  assert.equal(first.paused, true);
  assert.equal(first.muted, true, "only one is heard");
  assert.equal(second.muted, false);
});

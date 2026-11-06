// node --test tests/*.mjs
import assert from "node:assert/strict";
import { test } from "node:test";
import { fmtTC, Model, Peaks } from "../retake/ui/js/model.js";

const words = [];
for (let i = 0; i < 100; i++) words.push({ s: i + 0.1, e: i + 0.8, t: ` w${i}` });
const lesson = (edits = [], extra = {}) => new Model({ range: [0, 100], fps: 25, words, phrases: [], edits, ...extra });
const cut = (id, start, end, on = true, kind = "retake") => ({ id, kind, start, end, on });

test("overlapping and touching cuts merge, the rest is kept", () => {
  const m = lesson([cut(1, 10, 20), cut(2, 15, 25, true, "pause"), cut(3, 25.01, 30), cut(4, 50, 60, false)]);
  assert.deepEqual(m.groups.map(g => [g.a, g.b]), [[10, 30]]);
  assert.deepEqual(m.keep.map(k => [k.a, k.b]), [[0, 10], [30, 100]]);
  assert.equal(m.outDuration, 80);
  assert.equal(m.groups[0].main.id, 1, "a cut is named after its longest edit that is not a pause");
});

test("source time maps to result time and back", () => {
  const m = lesson([cut(1, 10, 20), cut(2, 40, 50)]);
  assert.equal(m.srcToOut(5), 5);
  assert.equal(m.srcToOut(15), 10, "inside a cut: the seam");
  assert.equal(m.srcToOut(30), 20);
  assert.equal(m.srcToOut(60), 40);
  assert.equal(m.outToSrc(10), 20, "a seam belongs to the following clip");
  assert.equal(m.outToSrc(35), 55);
  for (const t of [0, 3.3, 25, 39.9, 77]) assert.ok(Math.abs(m.outToSrc(m.srcToOut(t)) - t) < 1e-9);
});

test("the whole lesson cut away leaves an empty result, not an error", () => {
  const m = lesson([cut(1, 0, 100)]);
  assert.equal(m.outDuration, 0);
  assert.equal(m.outToSrc(5), 0);
  assert.equal(m.srcToOut(50), 0);
});

test("undo and redo restore everything, one step per change", () => {
  const m = lesson([cut(1, 10, 20)]);
  const e = m.byId.get(1);
  m.setOn(e, false);
  assert.equal(e.on, false);
  assert.equal(e.reviewed, true);
  const manual = m.addCut(30, 35);
  m.moveEdge(e, "start", 12);
  assert.equal(m.undoStack.length, 3);
  m.undo();
  assert.equal(e.start, 10);
  m.undo();
  assert.equal(m.edits.includes(manual), false);
  m.undo();
  assert.equal(e.on, true);
  assert.equal(e.reviewed, false);
  assert.equal(m.undo(), null);
  m.redo(); m.redo();
  assert.equal(e.on, false);
  assert.equal(m.edits.includes(manual), true);
  m.addCut(70, 75);
  assert.equal(m.redoStack.length, 0, "a new change forgets what was undone");
});

test("a drag is one undo step, and an abandoned drag changes nothing", () => {
  const m = lesson([cut(1, 10, 20)]);
  const e = m.byId.get(1);
  m.beginGesture();
  for (const t of [11, 12, 13]) { m.setEdge(e, "start", t); m.live(); }
  m.commit("граница");
  assert.equal(m.undoStack.length, 1);
  m.undo();
  assert.equal(e.start, 10);
  m.beginGesture();
  m.setEdge(e, "end", 40);
  m.cancelGesture();
  assert.equal(e.end, 20);
  assert.equal(m.undoStack.length, 0);
});

test("an edge cannot cross the other edge or leave the lesson", () => {
  const m = lesson([cut(1, 10, 20)]);
  const e = m.byId.get(1);
  m.setEdge(e, "start", 25);
  assert.ok(e.start < e.end);
  m.setEdge(e, "end", 500);
  assert.equal(e.end, 100);
  m.setEdge(e, "start", -5);
  assert.equal(e.start, 0);
});

test("cutting what is already cut adds nothing", () => {
  const m = lesson([cut(1, 10, 20)]);
  assert.equal(m.addCut(12, 18), null);
  assert.equal(m.undoStack.length, 0);
  assert.ok(m.addCut(15, 25), "reaching past the cut is a real change");
});

test("returning a range: whole cut, either side, the middle", () => {
  let m = lesson([cut(1, 10, 20)]);
  m.restoreRange(5, 25);
  assert.equal(m.byId.get(1).on, false);

  m = lesson([cut(1, 10, 20)]);
  m.restoreRange(5, 12);
  assert.deepEqual(m.groups.map(g => [g.a, g.b]), [[12, 20]]);

  m = lesson([cut(1, 10, 20)]);
  m.restoreRange(18, 30);
  assert.deepEqual(m.groups.map(g => [g.a, g.b]), [[10, 18]]);

  m = lesson([cut(1, 10, 20)]);
  m.restoreRange(14, 16);
  assert.deepEqual(m.groups.map(g => [g.a, g.b]), [[10, 14], [16, 20]]);
  m.undo();
  assert.deepEqual(m.groups.map(g => [g.a, g.b]), [[10, 20]]);

  m = lesson([cut(1, 10, 20, true, "manual")]);
  m.restoreRange(0, 50);
  assert.equal(m.edits.length, 0, "a manual cut that is returned disappears");
});

test("the blade marks kept material and splits cuts", () => {
  const m = lesson([cut(1, 10, 20)]);
  assert.equal(m.blade(30), "split");
  assert.equal(m.blade(40), "split");
  assert.deepEqual(m.clipAt(35), { a: 30, b: 40 });
  assert.deepEqual(m.clipAt(5), { a: 0, b: 10 });
  assert.equal(m.clipAt(15), null, "nothing is kept inside a cut");
  assert.equal(m.blade(30), "removed", "the blade on a mark removes it");
  assert.deepEqual(m.clipAt(35), { a: 20, b: 40 });
  assert.equal(m.blade(15), "cut");
  assert.equal(m.byId.get(1).end, 15);
  assert.equal(m.edits.length, 2);
  assert.deepEqual(m.groups.map(g => [g.a, g.b]), [[10, 20]], "no audible change yet");
});

test("cut points in a long pause stay close to the speech", () => {
  const W = [{ s: 1, e: 2, t: " a" }, { s: 10, e: 11, t: " b" }];
  const m = new Model({ range: [0, 20], fps: 25, words: W, phrases: [], edits: [] });
  assert.equal(m.gapPoint(0, "start"), 2.15);
  assert.equal(m.gapPoint(0, "end"), 9.85);
  assert.equal(m.gapPoint(0, ""), 6);
});

test("a cut point looks for the quietest moment nearby", () => {
  const env = new Uint8Array(2000).fill(200);
  env[505] = 3;  // 5.05 s
  const W = [{ s: 4, e: 5, t: " a" }, { s: 5.1, e: 6, t: " b" }];
  const m = new Model({ range: [0, 20], fps: 25, words: W, phrases: [], edits: [] }, env);
  assert.equal(m.gapPoint(0, "start"), 5.05);
});

test("snapping finds the nearest gap and respects the radius", () => {
  const m = lesson([cut(1, 50, 60)]);
  assert.equal(m.snap(10.9, 0.3).label, "между словами");
  assert.equal(m.snap(10.45, 0.02), null);
  assert.equal(m.snap(50.05, 0.2).t, 50);
  assert.equal(m.snap(50.05, 0.2, { skip: [50] })?.t === 50, false, "the edge being dragged does not attract itself");
});

test("edit points for the arrow keys", () => {
  const m = lesson([cut(1, 10, 20)]);
  assert.equal(m.editPoint(0, 1), 10);
  assert.equal(m.editPoint(10, 1), 20);
  assert.equal(m.editPoint(20, -1), 10);
  assert.equal(m.editPoint(5, -1), 0);
  assert.equal(m.editPoint(50, 1), 100);
});

test("only what changed goes to the server", () => {
  const m = lesson([cut(1, 10, 20), cut(2, 30, 40), cut(3, 50, 60)]);
  m.setOn(m.byId.get(1), false);
  m.moveEdge(m.byId.get(2), "end", 41);
  m.addCut(70, 75);
  m.blade(80);
  const d = m.decisions();
  assert.deepEqual(Object.keys(d.overrides), ["1", "2"]);
  assert.deepEqual(d.overrides[2], { on: true, start: 30, end: 41, orig_start: 30, orig_end: 40, reviewed: true });
  assert.equal(d.manual.length, 1);
  assert.deepEqual(d.splits, [80]);
});

test("a saved decision loads back as a change to the original", () => {
  const m = lesson([{ ...cut(2, 30, 41), orig_start: 30, orig_end: 40, default_on: true, reviewed: true }]);
  const e = m.byId.get(2);
  m.resetEdit(e);
  assert.deepEqual([e.start, e.end, e.reviewed], [30, 40, false]);
});

test("the waveform lookup agrees with a plain scan", () => {
  const v = Uint8Array.from({ length: 1000 }, (_, i) => (i * 7919) % 256);
  const p = new Peaks(v);
  for (const [a, b] of [[0, 1], [3, 9], [10, 300], [512, 513], [0, 1000], [990, 1000]]) {
    const [mx] = p.range(a, b);
    const real = Math.max(...v.slice(a, b));
    assert.ok(mx >= real, `max over ${a}..${b}`);  // whole cells of the pyramid: it may see a little more
  }
  assert.equal(p.range(0, 1000)[0], 255);
});

test("time labels do not roll over badly", () => {
  assert.equal(fmtTC(59.96), "1:00.0");
  assert.equal(fmtTC(3599.96), "1:00:00.0");
  assert.equal(fmtTC(0), "0:00.0");
});

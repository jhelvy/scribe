/**
 * The margin rail's layout solver.
 *
 * Run with: node tests/test_rail.mjs
 *
 * This is the piece of the viewer worth testing in isolation, because it is
 * pure geometry and because getting it wrong is exactly what made the previous
 * viewer's notes drift. rail.js is deliberately DOM-free so it can be required
 * straight into Node.
 */

import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const Rail = require("../scribe/viewer/rail.js");

let passed = 0;
let failed = 0;

function test(name, fn) {
  try {
    fn();
    passed++;
    console.log(`  ok   ${name}`);
  } catch (err) {
    failed++;
    console.log(`  FAIL ${name}\n       ${err.message}`);
  }
}

/** The behaviour the old single forward pass had. */
function naiveForward(notes, gap) {
  let previousBottom = -Infinity;
  return notes.map((note) => {
    const top = Math.max(note.anchor, previousBottom + gap);
    previousBottom = top + note.height;
    return top;
  });
}

const rms = (tops, notes) =>
  Math.sqrt(tops.reduce((sum, t, i) => sum + (t - notes[i].anchor) ** 2, 0) / tops.length);

console.log("rail layout");

test("notes far apart are never moved", () => {
  const notes = [
    { anchor: 0, height: 50 },
    { anchor: 300, height: 50 },
    { anchor: 700, height: 50 },
  ];
  assert.deepEqual(Rail.layout(notes, { gap: 10 }), [0, 300, 700]);
});

test("no overlap is ever produced", () => {
  const notes = [
    { anchor: 400, height: 60 },
    { anchor: 405, height: 90 },
    { anchor: 410, height: 40 },
    { anchor: 480, height: 70 },
  ];
  const tops = Rail.layout(notes, { gap: 12 });
  assert.ok(Rail.verify(notes, tops, 12), `overlapping: ${tops}`);
});

test("a cluster straddles its anchors instead of ratcheting down", () => {
  // This is the whole point. The naive forward pass can only push notes
  // *down*, so a cluster ends up entirely below where it belongs and every
  // note after it inherits the error.
  const notes = [
    { anchor: 500, height: 60 },
    { anchor: 505, height: 60 },
    { anchor: 510, height: 60 },
  ];
  const solved = Rail.layout(notes, { gap: 10 });
  const naive = naiveForward(notes, 10);

  const meanShift = solved.reduce((s, t, i) => s + (t - notes[i].anchor), 0) / 3;
  assert.ok(Math.abs(meanShift) < 1, `cluster should centre, mean shift ${meanShift}`);
  assert.ok(solved[0] < notes[0].anchor, "the first note should sit above its anchor");
  assert.ok(rms(solved, notes) < rms(naive, notes), "must beat the naive pass");
});

test("the solution minimises squared displacement", () => {
  // Compare against a brute-forced optimum on a small case.
  const notes = [
    { anchor: 200, height: 40 },
    { anchor: 210, height: 40 },
    { anchor: 215, height: 40 },
  ];
  const gap = 8;
  const tops = Rail.layout(notes, { gap, minTop: -Infinity });
  const cost = (start) => {
    let total = 0;
    let at = start;
    for (let i = 0; i < notes.length; i++) {
      total += (at - notes[i].anchor) ** 2;
      at += notes[i].height + gap;
    }
    return total;
  };
  // Everything collides here, so the optimum is a single packed block; sweep
  // its offset and confirm the solver found the best one.
  let best = Infinity;
  let bestStart = 0;
  for (let s = 100; s < 300; s += 0.25) {
    const c = cost(s);
    if (c < best) { best = c; bestStart = s; }
  }
  assert.ok(Math.abs(tops[0] - bestStart) < 0.5, `got ${tops[0]}, optimum ~${bestStart}`);
});

test("a focused note sits exactly on its anchor", () => {
  const notes = [
    { anchor: 400, height: 60 },
    { anchor: 405, height: 60 },
    { anchor: 410, height: 60 },
  ];
  for (let f = 0; f < notes.length; f++) {
    const tops = Rail.layoutWithFocus(notes, f, { gap: 10 });
    assert.equal(tops[f], notes[f].anchor, `focus ${f} landed at ${tops[f]}`);
    assert.ok(Rail.verify(notes, tops, 10), `focus ${f} overlaps`);
  }
});

test("focusing displaces neighbours rather than everything", () => {
  const notes = [
    { anchor: 400, height: 60 },
    { anchor: 405, height: 60 },
    { anchor: 900, height: 60 },
  ];
  const tops = Rail.layoutWithFocus(notes, 0, { gap: 10 });
  assert.equal(tops[0], 400);
  assert.equal(tops[2], 900, "a distant note should not be dragged along");
});

test("nothing renders above the container", () => {
  const notes = [
    { anchor: 10, height: 60 },
    { anchor: 15, height: 60 },
    { anchor: 20, height: 60 },
  ];
  const tops = Rail.layout(notes, { gap: 10, minTop: 0 });
  assert.ok(tops[0] >= 0, `first note at ${tops[0]}`);
  assert.ok(Rail.verify(notes, tops, 10));

  const focused = Rail.layoutWithFocus(notes, 2, { gap: 10, minTop: 0 });
  assert.ok(focused[0] >= 0, `clamped focus put the first note at ${focused[0]}`);
  assert.ok(Rail.verify(notes, focused, 10));
});

test("empty and single-note cases", () => {
  assert.deepEqual(Rail.layout([], { gap: 10 }), []);
  assert.deepEqual(Rail.layout([{ anchor: 42, height: 10 }], { gap: 10 }), [42]);
  assert.deepEqual(Rail.layoutWithFocus([{ anchor: 42, height: 10 }], 0, { gap: 10 }), [42]);
});

test("out-of-range focus index degrades to a plain layout", () => {
  const notes = [{ anchor: 100, height: 30 }, { anchor: 400, height: 30 }];
  assert.deepEqual(
    Rail.layoutWithFocus(notes, 9, { gap: 10 }),
    Rail.layout(notes, { gap: 10 })
  );
});

test("500 notes stay valid and stay fast", () => {
  const notes = [];
  let at = 0;
  for (let i = 0; i < 500; i++) {
    at += Math.random() * 400;
    notes.push({ anchor: at, height: 40 + Math.random() * 100 });
  }
  const started = process.hrtime.bigint();
  const tops = Rail.layout(notes, { gap: 12, minTop: 0 });
  const ms = Number(process.hrtime.bigint() - started) / 1e6;
  assert.ok(Rail.verify(notes, tops, 12), "overlap in the stress case");
  assert.ok(ms < 50, `took ${ms.toFixed(1)}ms`);
});

test("zero-height notes do not break ordering", () => {
  const notes = [
    { anchor: 100, height: 0 },
    { anchor: 100, height: 40 },
    { anchor: 100, height: 0 },
  ];
  assert.ok(Rail.verify(notes, Rail.layout(notes, { gap: 5 }), 5));
});

console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);

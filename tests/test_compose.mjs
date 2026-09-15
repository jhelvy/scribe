/**
 * The composer's pure decisions: what sends, which token the caret is in,
 * how candidates rank, how a completion is spliced in.
 *
 * Run with: node tests/test_compose.mjs
 *
 * compose.js is DOM-free for the same reason rail.js is: these are the parts
 * where a wrong answer is silent (a Chinese sentence posted at the IME's
 * Enter, a path completed as a command) and a browser is a poor place to
 * enumerate the cases.
 */

import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const Compose = require("../scribe/viewer/compose.js");

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

const key = (overrides) => Object.assign({ key: "Enter", keyCode: 13, isComposing: false, shiftKey: false, metaKey: false, ctrlKey: false, altKey: false }, overrides);

console.log("shouldSend");

test("Enter sends", () => assert.equal(Compose.shouldSend(key({})), true));
test("Cmd/Ctrl+Enter still sends", () => {
  assert.equal(Compose.shouldSend(key({ metaKey: true })), true);
  assert.equal(Compose.shouldSend(key({ ctrlKey: true })), true);
});
test("Shift+Enter is a newline", () => assert.equal(Compose.shouldSend(key({ shiftKey: true })), false));
test("an IME commit is not Enter", () => {
  assert.equal(Compose.shouldSend(key({ isComposing: true })), false);
  assert.equal(Compose.shouldSend(key({ keyCode: 229 })), false);
});
test("other keys never send", () => assert.equal(Compose.shouldSend(key({ key: "a", keyCode: 65 })), false));

console.log("slashToken");

test("a leading slash token with the caret inside", () => {
  assert.deepEqual(Compose.slashToken("/ph-ex", 6), { query: "ph-ex", start: 0, end: 6 });
  assert.deepEqual(Compose.slashToken("/ph-ex more", 3), { query: "ph-ex", start: 0, end: 6 });
});
test("caret past the first token is not a command", () => assert.equal(Compose.slashToken("/ph-ex more", 9), null));
test("a slash later in the text is a path", () => assert.equal(Compose.slashToken("look at src/x", 10), null));
test("a bare slash offers everything", () => assert.deepEqual(Compose.slashToken("/", 1), { query: "", start: 0, end: 1 }));
test("caret before the slash is outside", () => assert.equal(Compose.slashToken("/x", 0), null));
test("empty text", () => assert.equal(Compose.slashToken("", 0), null));

console.log("mentionToken");

test("an @ word at the caret", () => {
  assert.deepEqual(Compose.mentionToken("see @sc", 7), { query: "sc", start: 4, end: 7 });
  assert.deepEqual(Compose.mentionToken("@app.js first", 4), { query: "app", start: 0, end: 7 });
});
test("an email-like @ inside a word is not a mention", () => assert.equal(Compose.mentionToken("mail me@x", 9), null));
test("no @ word at the caret", () => assert.equal(Compose.mentionToken("plain words", 5), null));

console.log("score and rank");

test("prefix beats word start beats substring beats subsequence", () => {
  const p = Compose.score("ph", "ph-app");
  const w = Compose.score("app", "ph-app");
  const s = Compose.score("h-a", "ph-app");
  const q = Compose.score("pap", "ph-app");
  assert.ok(p > w && w > s && s > q && q > 0, `${p} ${w} ${s} ${q}`);
});
test("no match scores zero", () => assert.equal(Compose.score("zz", "ph-app"), 0));
test("empty query matches everything", () => assert.equal(Compose.score("", "anything"), 1));
test("case does not matter", () => assert.ok(Compose.score("PH", "ph-app") > 0));
test("rank filters, orders and is stable", () => {
  const items = [{ name: "b-ph" }, { name: "ph-b" }, { name: "zzz" }, { name: "ph-a" }];
  assert.deepEqual(Compose.rank("ph", items).map((i) => i.name), ["ph-b", "ph-a", "b-ph"]);
});
test("rank reads through a function", () => {
  const items = [{ path: "src/a.js" }, { path: "docs/b.md" }];
  assert.deepEqual(Compose.rank("doc", items, (i) => i.path).map((i) => i.path), ["docs/b.md"]);
});

console.log("complete and step");

test("completing a slash token leaves a space for arguments", () => {
  const out = Compose.complete("/ph-ex", { start: 0, end: 6 }, "/ph-explain");
  assert.deepEqual(out, { text: "/ph-explain ", caret: 12 });
});
test("completing before existing text keeps it", () => {
  const out = Compose.complete("/ph-ex the rest", { start: 0, end: 6 }, "/ph-explain");
  assert.equal(out.text, "/ph-explain the rest");
  assert.equal(out.caret, 11);
});
test("completing a mention mid-sentence", () => {
  const out = Compose.complete("see @sc now", { start: 4, end: 7 }, "@scribe/app.js");
  assert.equal(out.text, "see @scribe/app.js now");
});
test("step wraps both ways", () => {
  assert.equal(Compose.step(0, -1, 3), 2);
  assert.equal(Compose.step(2, 1, 3), 0);
  assert.equal(Compose.step(0, 1, 0), -1);
});

test("draft keys are per session", () => {
  assert.notEqual(Compose.draftKey("a"), Compose.draftKey("b"));
  assert.equal(Compose.draftKey(null), Compose.draftKey(""));
});

console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);

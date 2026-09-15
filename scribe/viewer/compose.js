/**
 * The composer's decisions, without the composer.
 *
 * Everything here is a pure function of a key event, a string and a caret
 * position, so `tests/test_compose.mjs` can drive it in Node the way
 * `test_rail.mjs` drives the rail. The DOM work — reading the textarea,
 * positioning the popover — stays in app.js.
 *
 * The one rule worth stating: Enter sends, and an IME commit is not Enter.
 * Chinese, Japanese and Korean input methods finish a composition with the
 * Enter key, and the browser reports that key with `isComposing` set (Safari
 * sometimes only with the legacy keyCode 229). Sending on it would post half a
 * sentence.
 */

(function (root, factory) {
  var api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.Compose = api;
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  /** Does this keydown mean "send"? Enter alone or with Cmd/Ctrl; never
   *  Shift+Enter (a newline) and never the Enter that commits an IME. */
  function shouldSend(ev) {
    if (ev.key !== "Enter") return false;
    if (ev.isComposing || ev.keyCode === 229) return false;
    if (ev.altKey) return false;
    if (ev.shiftKey) return false;
    return true;
  }

  /** The `/command` token the caret sits in, when the message starts with one.
   *  Returns {query, start, end} or null. Only the first token counts: a slash
   *  later in the text is a path, not a command. */
  function slashToken(text, caret) {
    if (!text || text[0] !== "/") return null;
    var end = text.search(/\s/);
    if (end < 0) end = text.length;
    if (caret == null) caret = end;
    if (caret < 1 || caret > end) return null;
    return { query: text.slice(1, end), start: 0, end: end };
  }

  /** The `@path` token the caret sits in, anywhere in the text, when the `@`
   *  begins a word. Returns {query, start, end} or null. */
  function mentionToken(text, caret) {
    if (!text) return null;
    if (caret == null) caret = text.length;
    var start = caret;
    while (start > 0 && !/\s/.test(text[start - 1])) start--;
    if (text[start] !== "@") return null;
    var end = caret;
    while (end < text.length && !/\s/.test(text[end])) end++;
    return { query: text.slice(start + 1, caret), start: start, end: end };
  }

  /** Score a candidate against a query: 0 when it does not match, higher is
   *  better. Prefix beats word-start beats a scattered subsequence; shorter
   *  candidates win ties. Case-insensitive. */
  function score(query, candidate) {
    var q = (query || "").toLowerCase();
    var c = (candidate || "").toLowerCase();
    if (!q) return 1;
    if (!c) return 0;
    if (c.indexOf(q) === 0) return 1000 - c.length;
    var at = c.indexOf(q);
    if (at > 0 && /[^a-z0-9]/.test(c[at - 1])) return 800 - c.length;
    if (at > 0) return 600 - c.length;
    var i = 0;
    var gaps = 0;
    var last = -1;
    for (var j = 0; j < c.length && i < q.length; j++) {
      if (c[j] === q[i]) {
        if (last >= 0 && j !== last + 1) gaps++;
        last = j;
        i++;
      }
    }
    if (i < q.length) return 0;
    return 400 - gaps * 20 - c.length;
  }

  /** Filter and rank `items` by `query`, reading the text through `key`
   *  (a property name or a function). Stable for equal scores. */
  function rank(query, items, key) {
    var read = typeof key === "function" ? key : function (it) { return it[key || "name"]; };
    var scored = [];
    (items || []).forEach(function (item, index) {
      var s = score(query, read(item));
      if (s > 0) scored.push({ item: item, s: s, index: index });
    });
    scored.sort(function (a, b) { return b.s - a.s || a.index - b.index; });
    return scored.map(function (x) { return x.item; });
  }

  /** Replace the token [start, end) in `text` with `insert`, returning the new
   *  text and where the caret should land. */
  function complete(text, token, insert) {
    var head = text.slice(0, token.start);
    var tail = text.slice(token.end);
    if (tail && !/^\s/.test(tail)) tail = " " + tail;
    if (!tail) insert = insert + " ";
    return { text: head + insert + tail, caret: head.length + insert.length + (tail ? 0 : 0) };
  }

  /** Move a highlighted index by `delta` inside a list of `n`, wrapping. */
  function step(index, delta, n) {
    if (!n) return -1;
    return ((index + delta) % n + n) % n;
  }

  function draftKey(sessionId) { return "scribe-draft:" + (sessionId || "new"); }

  return {
    shouldSend: shouldSend,
    slashToken: slashToken,
    mentionToken: mentionToken,
    score: score,
    rank: rank,
    complete: complete,
    step: step,
    draftKey: draftKey,
  };
});

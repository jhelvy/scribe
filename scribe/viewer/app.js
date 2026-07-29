/* scribe viewer.
 *
 * Two ideas shape this file.
 *
 * 1. **Nothing is ever re-rendered wholesale.** The previous generation of this
 *    viewer rebuilt `#log` with innerHTML on every poll, then tried to restore
 *    the scroll position, the focused message and the open disclosure triangles
 *    afterwards. It mostly worked, and the "mostly" was the bug. Here every
 *    round and every item inside it carries a server-assigned key; an update
 *    replaces only the nodes whose payload actually changed. Open `<details>`,
 *    text selection, and scroll position survive because they are never touched.
 *
 * 2. **The rail is a solver, not a stack.** See rail.js.
 */

(function () {
  "use strict";

  var $ = function (id) { return document.getElementById(id); };
  var reduceMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;

  var state = {
    sessionId: null,
    head: {},
    rounds: [],           // payloads, index-aligned
    roundEls: new Map(),  // index -> element
    itemEls: new Map(),   // key -> {el, payload}
    notes: [],            // {key, el, anchorEl, kind}
    pending: new Map(),   // call_id -> payload
    focusKey: null,
    following: true,
    sessions: [],
    openProjects: new Set(),
    retry: null,
    filter: "",
    results: null,      // global search results, or null for the project tree
    focusRound: 0,      // round to jump to after the next load
    searchSeq: 0,
    find: "",
    hits: [],
    hitIndex: 0,
    folded: false,
    es: null,
    reconnect: 0,
  };

  /* ----------------------------------------------------------------- utils */

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  }

  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }

  function toast(message) {
    var node = el("div", "toast", message);
    document.body.appendChild(node);
    setTimeout(function () { node.remove(); }, 2200);
  }

  function timeOf(ts) {
    if (!ts) return "";
    var d = new Date(ts);
    return isNaN(d) ? "" : d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }

  function dateOf(ts) {
    if (!ts) return "";
    var d = new Date(ts);
    return isNaN(d) ? "" : d.toLocaleDateString([], { month: "short", day: "numeric" });
  }

  /* ------------------------------------------------------------ sanitizing */

  // Transcript content is not trusted input. A file this agent merely *read*
  // could contain `<script>` or an `onerror=` attribute, and marked passes raw
  // HTML straight through. Since this page shares an origin with the control
  // API — which can approve tool calls — a payload getting to run here would be
  // a genuine problem, not a cosmetic one. The daemon also sends a CSP that
  // forbids inline script; this is the second lock.
  var BANNED = /^(script|style|iframe|object|embed|link|meta|base|form|input|button|textarea|select|svg|math)$/i;

  function sanitize(root) {
    var walker = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT);
    var doomed = [];
    var node;
    while ((node = walker.nextNode())) {
      if (BANNED.test(node.tagName)) { doomed.push(node); continue; }
      for (var i = node.attributes.length - 1; i >= 0; i--) {
        var attr = node.attributes[i];
        var name = attr.name.toLowerCase();
        var value = (attr.value || "").replace(/[\s\u0000-\u001f]/g, "").toLowerCase();
        if (name.slice(0, 2) === "on") node.removeAttribute(attr.name);
        else if ((name === "href" || name === "src" || name === "xlink:href") &&
                 (value.indexOf("javascript:") === 0 || value.indexOf("data:text/html") === 0)) {
          node.removeAttribute(attr.name);
        }
      }
      if (node.tagName === "A") {
        node.setAttribute("rel", "noopener noreferrer");
        node.setAttribute("target", "_blank");
      }
    }
    doomed.forEach(function (n) { n.remove(); });
    return root;
  }

  var mdOptions = { gfm: true, breaks: false, headerIds: false, mangle: false };

  function escapeHtml(text) {
    return String(text == null ? "" : text)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }

  // Raw HTML in a transcript is *content*, not markup. Someone writing "maybe
  // the <aside> option" in a prompt means those eight characters; marked treats
  // it as the start of an HTML block and silently swallows the rest of the
  // paragraph. Overriding the html renderer to escape means every angle bracket
  // anyone typed is shown exactly as typed — which is both more faithful and
  // one fewer way for hostile markup to reach the DOM. Fenced code is a
  // different token type and is unaffected.
  function installRenderer() {
    if (!window.marked || !marked.use) return;
    marked.use({
      gfm: true,
      breaks: false,
      renderer: {
        html: function (token) {
          var raw = typeof token === "string" ? token : token.raw || token.text || "";
          return escapeHtml(raw);
        },
      },
    });
  }

  function markdown(text) {
    var wrap = el("div", "md");
    try {
      wrap.innerHTML = window.marked ? marked.parse(text || "", mdOptions) : "";
    } catch (e) {
      wrap.textContent = text || "";
    }
    if (!window.marked) wrap.textContent = text || "";
    return sanitize(wrap);
  }

  /* ---------------------------------------------------------------- render */

  function renderRound(payload) {
    var node = el("article", "round");
    node.dataset.index = payload.index;
    node.dataset.source = payload.source;
    node.id = "round-" + payload.index;

    var head = el("div", "round-head");
    head.appendChild(el("span", "round-n", String(payload.index)));
    head.appendChild(el("span", "round-who", payload.source === "web" ? "you · web" : payload.source === "system" ? "session" : "you"));
    var meta = el("span", "round-meta");
    [timeOf(payload.ts), payload.duration_label, payload.tool_count ? payload.tool_count + " tools" : "", payload.usage_label && payload.usage.total ? payload.usage_label + " tok" : ""]
      .filter(Boolean)
      .forEach(function (bit) { meta.appendChild(el("span", null, bit)); });
    head.appendChild(meta);
    node.appendChild(head);

    if (payload.prompt) {
      var prompt = el("div", "prompt");
      prompt.appendChild(markdown(payload.prompt));
      node.appendChild(prompt);
    }
    if (payload.images) {
      node.appendChild(el("div", "notice", "+ " + payload.images + " pasted image" + (payload.images > 1 ? "s" : "")));
    }

    var body = el("div", "reply");
    body.dataset.body = "1";
    node.appendChild(body);
    syncItems(body, payload);
    return node;
  }

  function syncItems(body, payload) {
    var wanted = payload.items || [];
    var existing = Array.from(body.children);
    var byKey = new Map();
    existing.forEach(function (child) { if (child.dataset.key) byKey.set(child.dataset.key, child); });

    var frag = document.createDocumentFragment();
    wanted.forEach(function (item) {
      var previous = state.itemEls.get(item.key);
      var node;
      if (previous && previous.el.isConnected && sameItem(previous.payload, item)) {
        node = previous.el;
      } else {
        node = renderItem(item, previous && previous.el);
        state.itemEls.set(item.key, { el: node, payload: item });
      }
      frag.appendChild(node); // moving a live node preserves its state
    });
    clear(body);
    body.appendChild(frag);
  }

  function sameItem(a, b) {
    // The server stamps a content hash on every item, so this is a string
    // compare rather than stringifying a round that may hold a hundred tool
    // calls with tens of kilobytes of output each, several times a second.
    if (a && b && a.h && b.h) return a.h === b.h;
    return JSON.stringify(a) === JSON.stringify(b);
  }

  function renderItem(item, previousEl) {
    switch (item.kind) {
      case "text": return renderText(item);
      case "thinking": return renderThinking(item, previousEl);
      case "notice": return renderNotice(item);
      case "tool": return renderTool(item, previousEl);
      default: return el("div", "notice", item.kind);
    }
  }

  function renderText(item) {
    var node = markdown(item.md);
    node.dataset.key = item.key;
    if (item.truncated) node.appendChild(el("div", "truncated", "… truncated"));
    return node;
  }

  function renderThinking(item, previousEl) {
    var node = el("details", "thinking");
    node.dataset.key = item.key;
    // Carry the open state across a re-render of the same item.
    if (previousEl && previousEl.tagName === "DETAILS") node.open = previousEl.open;
    var summary = el("summary", null, item.label ? "thought for " + item.label : "thinking");
    node.appendChild(summary);
    var body = el("div", "body");
    body.appendChild(markdown(item.md));
    node.appendChild(body);
    return node;
  }

  function renderNotice(item) {
    var node = el("div", "notice");
    node.dataset.key = item.key;
    node.dataset.variant = item.variant;
    node.appendChild(el("span", null, item.variant === "command" ? "»" : item.variant === "compact" ? "⧉" : "·"));
    node.appendChild(el("span", null, item.text));
    return node;
  }

  var STATUS_CHIP = { error: "error", "no-result": "not run", pending: "running", interrupted: "interrupted" };
  // Kinds worth spending a model call on. A Read or a todo update explains
  // itself from its own arguments; a shell pipeline does not.
  var EXPLAINABLE = /^(bash|other|mcp|web|task)$/;

  function renderTool(item, previousEl) {
    var node = el("details", "tool-card");
    node.dataset.key = item.key;
    node.dataset.status = item.status;
    node.dataset.callId = item.id || "";
    if (previousEl && previousEl.tagName === "DETAILS") node.open = previousEl.open;

    var summary = el("summary");
    summary.appendChild(el("span", "tool-name", item.name));
    summary.appendChild(el("span", "tool-subject", item.subject || ""));

    var flags = el("span", "tool-flags");
    if (item.tool_kind === "edit" && (item.added || item.removed)) {
      if (item.added) flags.appendChild(el("span", "chip add", "+" + item.added));
      if (item.removed) flags.appendChild(el("span", "chip del", "−" + item.removed));
    }
    if (STATUS_CHIP[item.status]) {
      flags.appendChild(el("span", "chip" + (item.status === "error" ? " err" : ""), STATUS_CHIP[item.status]));
    }
    if (item.duration_label && item.duration_ms > 1500) {
      flags.appendChild(el("span", "chip", item.duration_label));
    }
    if (item.explanation) {
      var chip = el("span", "chip note", "note");
      chip.title = "Show the explanation in the margin";
      chip.addEventListener("click", function (ev) {
        ev.preventDefault();
        ev.stopPropagation();
        focusNote(item.key);
      });
      flags.appendChild(chip);
    } else if (EXPLAINABLE.test(item.tool_kind)) {
      // Any call can be explained on demand, in any session, including ones
      // recorded months ago. The answer is cached by content, so asking once
      // annotates that command everywhere it ever appears.
      var ask = el("span", "chip ask", "explain");
      ask.title = "Explain this call in plain English";
      ask.addEventListener("click", function (ev) {
        ev.preventDefault();
        ev.stopPropagation();
        ask.textContent = "…";
        api("/api/explain", { session_id: state.sessionId, call_id: item.id }).then(function (r) {
          if (r.error) { ask.textContent = "explain"; toast(r.error); }
        });
      });
      flags.appendChild(ask);
    }
    summary.appendChild(flags);
    node.appendChild(summary);

    node.appendChild(renderToolBody(item));
    node.addEventListener("toggle", function () { scheduleRail(true); });
    return node;
  }

  function renderToolBody(item) {
    var body = el("div", "tool-body");

    function block(label, node) {
      var wrap = el("div", "block");
      if (label) wrap.appendChild(el("div", "label", label));
      wrap.appendChild(node);
      body.appendChild(wrap);
      return wrap;
    }

    if (item.tool_kind === "bash") {
      if (item.command) block(null, pre(item.command, "code"));
      if (item.stdout) block("output", pre(item.stdout, "code out"));
      if (item.stderr) block("stderr", pre(item.stderr, "code err"));
      if (!item.stdout && !item.stderr && item.result_text) block("output", pre(item.result_text, "code out"));
      if (!item.stdout && !item.stderr && !item.result_text && item.status === "ok") {
        block(null, el("div", "truncated", "no output"));
      }
    } else if (item.tool_kind === "edit") {
      if (item.patch && item.patch.length) block(item.file_path || "diff", renderDiff(item.patch));
      else if (item.result_text) block("result", pre(item.result_text, "code out"));
      if (item.status === "error" && item.result_text) block("error", pre(item.result_text, "code err"));
    } else if (item.tool_kind === "write") {
      if (item.content) block(item.file_path || "content", pre(item.content, "code"));
    } else if (item.tool_kind === "read") {
      var lines = (item.result_text || "").split("\n").length - 1;
      block(null, el("div", "truncated", item.status === "error" ? "" : "read " + lines + " lines"));
      if (item.status === "error") block("error", pre(item.result_text, "code err"));
    } else if (item.tool_kind === "task" && item.subagent) {
      var sub = el("div", "subagent");
      item.subagent.forEach(function (round) { sub.appendChild(renderRound(round)); });
      block(item.agent_name || "subagent", sub);
    } else {
      if (item.input && Object.keys(item.input).length) {
        block("arguments", pre(JSON.stringify(item.input, null, 2), "code"));
      }
      if (item.result_text) block("result", pre(item.result_text, "code out"));
    }

    if (item.truncated) body.appendChild(el("div", "truncated", "… output truncated; the full text is in the markdown log"));
    return body;
  }

  function pre(text, cls) {
    var node = el("pre", cls);
    node.textContent = text || "";
    return node;
  }

  function renderDiff(hunks) {
    var node = el("pre", "code diff");
    hunks.forEach(function (hunk) {
      var header = el("span", "l h", "@@ -" + hunk.oldStart + "," + hunk.oldLines + " +" + hunk.newStart + "," + hunk.newLines + " @@");
      node.appendChild(header);
      (hunk.lines || []).forEach(function (line) {
        var cls = line[0] === "+" ? "l a" : line[0] === "-" ? "l d" : "l";
        node.appendChild(el("span", cls, line));
      });
    });
    return node;
  }

  /* ------------------------------------------------------------ reconcile */

  function applyRounds(rounds, removed) {
    var column = $("column");
    var appended = false;

    (removed || []).forEach(function (index) {
      var node = state.roundEls.get(index);
      if (node) { node.remove(); state.roundEls.delete(index); }
      state.rounds[index] = undefined;
    });

    rounds.forEach(function (payload) {
      var i = payload.index - 1;
      var existing = state.roundEls.get(i);
      state.rounds[i] = payload;
      if (existing) {
        patchRound(existing, payload);
      } else {
        var node = renderRound(payload);
        if (!reduceMotion) node.classList.add("enter");
        insertRound(column, node, i);
        state.roundEls.set(i, node);
        appended = true;
      }
    });

    rebuildNotes();
    if (state.find) runFind(state.find, false);
    if (appended && state.following) scrollToBottom();
    scheduleRail(true);
  }

  function patchRound(node, payload) {
    var head = node.querySelector(".round-meta");
    if (head) {
      clear(head);
      [timeOf(payload.ts), payload.duration_label, payload.tool_count ? payload.tool_count + " tools" : "", payload.usage.total ? payload.usage_label + " tok" : ""]
        .filter(Boolean)
        .forEach(function (bit) { head.appendChild(el("span", null, bit)); });
    }
    syncItems(node.querySelector('[data-body="1"]'), payload);
  }

  function insertRound(column, node, index) {
    var after = null;
    for (var i = index + 1; i < state.rounds.length; i++) {
      if (state.roundEls.has(i)) { after = state.roundEls.get(i); break; }
    }
    if (after) column.insertBefore(node, after);
    else column.appendChild(node);
  }

  /* ----------------------------------------------------------------- rail */

  function rebuildNotes() {
    var inner = $("rail-inner");
    var wanted = [];

    state.rounds.forEach(function (payload) {
      if (!payload) return;
      (payload.items || []).forEach(function (item) {
        if (item.kind !== "tool") return;
        if (!item.explanation && !state.pending.has(item.id)) return;
        wanted.push(item);
      });
    });

    var seen = new Set();
    var notes = [];
    wanted.forEach(function (item) {
      seen.add(item.key);
      var record = state.notes.find(function (n) { return n.key === item.key; });
      var pending = state.pending.get(item.id);
      if (!record) {
        record = { key: item.key, el: renderNote(item, pending) };
        if (!reduceMotion) record.el.classList.add("appear");
        inner.appendChild(record.el);
      } else {
        updateNote(record.el, item, pending);
      }
      record.anchorEl = state.itemEls.has(item.key) ? state.itemEls.get(item.key).el : null;
      record.item = item;
      notes.push(record);
    });

    state.notes.forEach(function (record) {
      if (!seen.has(record.key)) record.el.remove();
    });
    state.notes = notes;
  }

  function renderNote(item, pending) {
    var node = el("aside", "rail-note");
    node.dataset.key = item.key;
    node.addEventListener("click", function (ev) {
      if (ev.target.closest("button")) return;
      focusNote(item.key);
    });
    updateNote(node, item, pending);
    return node;
  }

  function updateNote(node, item, pending) {
    clear(node);
    var awaiting = pending && !pending.decided;
    node.classList.toggle("approval", !!awaiting);

    var head = el("div", "rail-note-head");
    head.appendChild(el("span", "rail-note-tool", item.name));
    if (awaiting) head.appendChild(el("span", "chip", "needs approval"));
    var tier = (pending && pending.explanation_tier) || item.explanation_tier;
    head.appendChild(el("span", "rail-note-tier", tier ? "explained" : ""));
    node.appendChild(head);

    var text = (pending && pending.explanation) || item.explanation || "";
    var body = el("div", "rail-note-body");
    if (text) body.appendChild(markdown(text));
    else body.appendChild(el("div", "truncated", "explaining…"));
    node.appendChild(body);

    if (item.subject) node.appendChild(el("div", "rail-note-sub", item.subject));

    if (awaiting && pending.seconds_left > 0) {
      var actions = el("div", "approval-actions");
      actions.appendChild(actionButton("allow", "approve", pending.call_id));
      actions.appendChild(actionButton("deny", "deny", pending.call_id));
      actions.appendChild(actionButton("", "→ terminal", pending.call_id, "pass"));
      node.appendChild(actions);
      var bar = el("div", "countdown");
      var fill = el("i");
      bar.appendChild(fill);
      node.appendChild(bar);
      startCountdown(fill, pending);
    }
  }

  function actionButton(cls, label, callId, behavior) {
    var button = el("button", "btn " + cls, label);
    button.type = "button";
    button.addEventListener("click", function (ev) {
      ev.stopPropagation();
      decide(callId, behavior || (cls === "allow" ? "allow" : cls === "deny" ? "deny" : "pass"));
    });
    return button;
  }

  function startCountdown(fill, pending) {
    var total = pending.seconds_left;
    var started = Date.now();
    (function step() {
      if (!fill.isConnected) return;
      var left = total - (Date.now() - started) / 1000;
      fill.style.width = Math.max(0, (left / total) * 100) + "%";
      if (left > 0) requestAnimationFrame(step);
    })();
  }

  var railQueued = false;

  function scheduleRail(immediate) {
    if (immediate) return layoutRail();
    if (railQueued) return;
    railQueued = true;
    requestAnimationFrame(function () { railQueued = false; layoutRail(); });
  }

  function layoutRail() {
    var rail = $("rail");
    if (!rail.offsetParent) return; // hidden at narrow widths
    var origin = rail.getBoundingClientRect().top + window.scrollY;

    var measurable = state.notes.filter(function (n) { return n.anchorEl && n.anchorEl.isConnected; });
    if (!measurable.length) { drawTethers([]); return; }

    var notes = measurable.map(function (record) {
      var box = record.anchorEl.getBoundingClientRect();
      return {
        anchor: box.top + window.scrollY - origin,
        height: record.el.offsetHeight,
      };
    });

    var focusIndex = measurable.findIndex(function (n) { return n.key === state.focusKey; });
    var tops = focusIndex >= 0
      ? Rail.layoutWithFocus(notes, focusIndex, { gap: 10, minTop: 0 })
      : Rail.layout(notes, { gap: 10, minTop: 0 });

    measurable.forEach(function (record, i) {
      record.top = tops[i];
      record.anchorTop = notes[i].anchor;
      record.el.style.transform = "translateY(" + Math.round(tops[i]) + "px)";
      record.el.classList.toggle("focused", record.key === state.focusKey);
      if (record.anchorEl) record.anchorEl.classList.toggle("linked", record.key === state.focusKey);
    });

    $("rail-inner").style.height = Math.max(0, tops[tops.length - 1] + notes[notes.length - 1].height) + "px";
    drawTethers(measurable);
  }

  function drawTethers(records) {
    var svg = $("tethers");
    var rail = $("rail");
    var column = $("column");
    clear(svg);
    if (!records.length) return;

    var railBox = rail.getBoundingClientRect();
    var columnRight = column.getBoundingClientRect().right - railBox.left;
    svg.setAttribute("width", railBox.width);
    svg.style.height = $("rail-inner").style.height;

    records.forEach(function (record) {
      // A short elbow from the tool card's right edge across the gutter to the
      // note. Cheap to draw, and it makes the pairing legible when a cluster
      // has pushed a note well away from its anchor.
      var y0 = record.anchorTop + 14;
      var y1 = record.top + 14;
      var path = document.createElementNS("http://www.w3.org/2000/svg", "path");
      var mid = (columnRight + 0) / 2;
      path.setAttribute(
        "d",
        "M " + columnRight + " " + y0 + " C " + mid + " " + y0 + ", " + mid + " " + y1 + ", " + 0 + " " + y1
      );
      if (record.key === state.focusKey) path.classList.add("active");
      svg.appendChild(path);
    });
  }

  function focusNote(key) {
    state.focusKey = state.focusKey === key ? null : key;
    layoutRail();
    if (!state.focusKey) return;
    var record = state.notes.find(function (n) { return n.key === key; });
    if (record && record.anchorEl) {
      if (record.anchorEl.tagName === "DETAILS") record.anchorEl.open = true;
      scrollToCenter(record.anchorEl);
      layoutRail();
    }
  }

  /* -------------------------------------------------------------- scrolling */

  function topbarHeight() {
    var bar = document.querySelector(".topbar");
    return bar ? bar.getBoundingClientRect().height : 0;
  }

  // Centre when the target fits comfortably; top-align when it does not.
  // Centring a message taller than the viewport would start it above the fold,
  // which is worse than useless — you would have to scroll *up* to read the
  // thing you just clicked.
  function scrollToCenter(node) {
    var box = node.getBoundingClientRect();
    var chrome = topbarHeight();
    var usable = window.innerHeight - chrome;
    var top = box.top + window.scrollY - chrome;
    var offset = box.height < usable * 0.6 ? (usable - box.height) / 2 : usable * 0.14;
    window.scrollTo({ top: Math.max(0, top - offset), behavior: reduceMotion ? "auto" : "smooth" });
  }

  // "Latest" means the last thing anyone wrote, not the bottom of the
  // document. The stage carries a tall trailing pad so the final message can
  // sit at a comfortable reading height; scrolling to `scrollHeight` would land
  // in that empty space and look like the log had stopped rendering.
  function contentBottom() {
    var column = $("column");
    var last = column.lastElementChild;
    if (!last) return 0;
    var box = last.getBoundingClientRect();
    return box.bottom + window.scrollY;
  }

  function bottomTarget() {
    var target = contentBottom() - window.innerHeight + 24;
    return Math.max(0, Math.min(target, document.body.scrollHeight - window.innerHeight));
  }

  function scrollToBottom() {
    window.scrollTo({ top: bottomTarget(), behavior: reduceMotion ? "auto" : "smooth" });
  }

  function nearBottom() {
    return window.scrollY >= bottomTarget() - 120;
  }

  function onScroll() {
    var was = state.following;
    state.following = nearBottom();
    if (was !== state.following) updateFollowPill();
    var max = document.body.scrollHeight - window.innerHeight;
    $("progress").style.width = max > 0 ? (window.scrollY / max) * 100 + "%" : "0";
    scheduleRail();
  }

  function updateFollowPill() {
    var pill = $("follow-pill");
    var live = state.head && state.head.live;
    pill.dataset.show = String(!state.following && state.rounds.length > 0);
    $("follow-label").textContent = live ? "following live" : "jump to latest";
  }

  /* ---------------------------------------------------------------- search */

  function runFind(term, jump) {
    state.find = term;
    clearHits();
    if (!term || term.length < 2) {
      $("find-count").textContent = "";
      state.roundEls.forEach(function (node) { node.classList.remove("dim"); });
      return;
    }
    var needle = term.toLowerCase();
    var hits = [];
    state.roundEls.forEach(function (node) {
      var found = markMatches(node, needle, hits);
      node.classList.toggle("dim", !found);
    });
    state.hits = hits;
    state.hitIndex = 0;
    $("find-count").textContent = hits.length ? "1/" + hits.length : "0";
    if (jump && hits.length) gotoHit(0);
  }

  function markMatches(root, needle, hits) {
    var walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
      acceptNode: function (node) {
        if (!node.nodeValue || node.nodeValue.toLowerCase().indexOf(needle) < 0) return NodeFilter.FILTER_REJECT;
        if (node.parentElement && node.parentElement.closest("mark.hit")) return NodeFilter.FILTER_REJECT;
        return NodeFilter.FILTER_ACCEPT;
      },
    });
    var targets = [];
    var node;
    while ((node = walker.nextNode())) targets.push(node);
    targets.forEach(function (text) {
      var value = text.nodeValue;
      var lower = value.toLowerCase();
      var frag = document.createDocumentFragment();
      var at = 0;
      var found;
      while ((found = lower.indexOf(needle, at)) >= 0) {
        if (found > at) frag.appendChild(document.createTextNode(value.slice(at, found)));
        var mark = el("mark", "hit", value.slice(found, found + needle.length));
        frag.appendChild(mark);
        hits.push(mark);
        at = found + needle.length;
      }
      if (at < value.length) frag.appendChild(document.createTextNode(value.slice(at)));
      text.parentNode.replaceChild(frag, text);
    });
    return targets.length > 0;
  }

  function clearHits() {
    document.querySelectorAll("mark.hit").forEach(function (mark) {
      var parent = mark.parentNode;
      if (!parent) return;
      parent.replaceChild(document.createTextNode(mark.textContent), mark);
      parent.normalize();
    });
    state.hits = [];
  }

  function gotoHit(index) {
    if (!state.hits.length) return;
    state.hits.forEach(function (m) { m.classList.remove("current"); });
    state.hitIndex = (index + state.hits.length) % state.hits.length;
    var mark = state.hits[state.hitIndex];
    mark.classList.add("current");
    var card = mark.closest("details");
    if (card) card.open = true;
    scrollToCenter(mark);
    $("find-count").textContent = state.hitIndex + 1 + "/" + state.hits.length;
  }

  /* -------------------------------------------------------------- sessions */

  // Projects are collapsed by default. With a hundred sessions across a dozen
  // repos, a flat list is a wall of text you have to scroll past to reach
  // anything; the project you are working in is almost always the only one you
  // want open. The active session's project opens itself, and typing a filter
  // opens whatever matches.
  function loadOpenProjects() {
    try {
      return new Set(JSON.parse(localStorage.getItem("scribe-open-projects") || "[]"));
    } catch (e) {
      return new Set();
    }
  }

  function saveOpenProjects() {
    try {
      localStorage.setItem("scribe-open-projects", JSON.stringify([...state.openProjects]));
    } catch (e) {}
  }

  // The sidebar is either the project tree or a set of search results. One
  // input drives both: the tree filters on titles as you type, and the
  // full-text query replaces it the moment results come back.
  function renderSidebar() {
    if (state.results && state.filter.trim().length >= 2) return renderSearchResults();
    renderSessions();
  }

  function runGlobalSearch() {
    var query = state.filter.trim();
    if (query.length < 2) {
      state.results = null;
      return renderSidebar();
    }
    var seq = ++state.searchSeq;
    api("/api/search?q=" + encodeURIComponent(query) + "&limit=400").then(function (data) {
      if (seq !== state.searchSeq) return; // a later keystroke already won
      if (!data || data.offline || data.error) return;
      state.results = data;
      renderSidebar();
    });
  }

  var HL_OPEN = String.fromCharCode(2);
  var HL_CLOSE = String.fromCharCode(3);

  function snippetNode(text) {
    // FTS5 marks hits with control characters rather than markup, so the
    // snippet can never inject HTML no matter what was in the transcript.
    var node = el("div", "snip");
    String(text || "").split(HL_OPEN).forEach(function (chunk, i) {
      if (i === 0) return node.appendChild(document.createTextNode(chunk));
      var parts = chunk.split(HL_CLOSE);
      node.appendChild(el("mark", null, parts[0]));
      if (parts.length > 1) node.appendChild(document.createTextNode(parts.slice(1).join("")));
    });
    return node;
  }

  function renderSearchResults() {
    var list = $("session-list");
    clear(list);
    var data = state.results;

    var head = el("div", "search-head");
    head.appendChild(el("span", null,
      data.total + " match" + (data.total === 1 ? "" : "es") + " in " +
      data.sessions.length + " conversation" + (data.sessions.length === 1 ? "" : "s")));
    if (data.indexing) head.appendChild(el("span", "indexing", "indexing…"));
    list.appendChild(head);

    if (!data.sessions.length) {
      list.appendChild(el("div", "empty", "no matches"));
      return;
    }

    data.sessions.forEach(function (entry) {
      var hit = el("button", "search-hit");
      hit.setAttribute("aria-current", String(entry.id === state.sessionId));

      var top = el("div", "hit-head");
      top.appendChild(el("span", "hit-title", entry.title));
      top.appendChild(el("span", "hit-count", String(entry.hits)));
      hit.appendChild(top);

      var meta = el("div", "hit-meta");
      meta.appendChild(el("span", null, entry.project));
      meta.appendChild(el("span", null, dateOf(entry.updated)));
      if (entry.archived) meta.appendChild(el("span", "kept", "kept"));
      hit.appendChild(meta);

      (entry.matches || []).forEach(function (match) {
        var row = snippetNode(match.snippet);
        row.dataset.round = match.round;
        hit.appendChild(row);
      });

      var firstRound = (entry.matches && entry.matches[0] && entry.matches[0].round) || 0;
      hit.addEventListener("click", function () {
        openAt(entry.id, firstRound);
        $("sidebar").dataset.open = "false";
      });
      list.appendChild(hit);
    });
  }

  // Open a conversation and land on the round that matched, rather than at the
  // bottom — the point of a search result is the specific moment it found.
  function openAt(id, round) {
    state.focusRound = round || 0;
    if (id === state.sessionId) return jumpToFocus();
    selectSession(id);
  }

  function jumpToFocus() {
    var round = state.focusRound;
    state.focusRound = 0;
    if (!round) return false;
    var node = state.roundEls.get(round - 1) || document.getElementById("round-" + round);
    if (!node) return false;
    state.following = false;
    scrollToCenter(node);
    node.classList.remove("flash");
    void node.offsetWidth;
    node.classList.add("flash");
    updateFollowPill();
    return true;
  }

  function renderSessions() {
    var list = $("session-list");
    clear(list);
    var filter = state.filter.trim().toLowerCase();
    var groups = new Map();

    state.sessions.forEach(function (session) {
      if (filter && (session.title + " " + session.project).toLowerCase().indexOf(filter) < 0) return;
      if (!groups.has(session.project)) groups.set(session.project, []);
      groups.get(session.project).push(session);
    });

    if (!groups.size) {
      list.appendChild(el("div", "empty", filter ? "no matches" : "no sessions"));
      return;
    }

    var activeProject = null;
    state.sessions.forEach(function (s) {
      if (s.id === state.sessionId) activeProject = s.project;
    });

    groups.forEach(function (sessions, project) {
      var open = !!filter || project === activeProject || state.openProjects.has(project);

      var group = el("div", "project-group");
      group.dataset.open = String(open);

      var header = el("button", "project-name");
      header.setAttribute("aria-expanded", String(open));
      header.appendChild(el("span", "caret", "›"));
      header.appendChild(el("span", "pname", project));
      if (sessions.some(function (s) { return s.live; })) {
        header.appendChild(el("span", "live-dot"));
      }
      header.appendChild(el("span", "pcount", String(sessions.length)));
      header.addEventListener("click", function () {
        var next = group.dataset.open !== "true";
        group.dataset.open = String(next);
        header.setAttribute("aria-expanded", String(next));
        if (next) state.openProjects.add(project);
        else state.openProjects.delete(project);
        saveOpenProjects();
      });
      group.appendChild(header);

      var body = el("div", "project-sessions");
      sessions.forEach(function (session) {
        var button = el("button", "session-item");
        button.setAttribute("aria-current", String(session.id === state.sessionId));
        button.appendChild(el("span", "t", session.title || session.id.slice(0, 8)));
        var meta = el("span", "m");
        if (session.live) meta.appendChild(el("span", "live-dot"));
        meta.appendChild(el("span", null, dateOf(session.updated) + " " + timeOf(session.updated)));
        if (session.archived) {
          // Claude Code has deleted the original; this exists only because we
          // archived it. Worth saying plainly rather than silently.
          var kept = el("span", "kept", "kept");
          kept.title = "Claude Code deleted the original — preserved by scribe";
          meta.appendChild(kept);
        }
        button.appendChild(meta);
        button.addEventListener("click", function () {
          state.openProjects.add(project);
          saveOpenProjects();
          selectSession(session.id);
          $("sidebar").dataset.open = "false";
        });
        body.appendChild(button);
      });
      group.appendChild(body);
      list.appendChild(group);
    });
  }

  function selectSession(id) {
    if (!id || id === state.sessionId) return;
    state.sessionId = id;
    location.hash = "#/s/" + id;
    resetView();
    load(id);
    renderSidebar();
  }

  function resetView() {
    clear($("column"));
    clear($("rail-inner"));
    clear($("tethers"));
    state.rounds = [];
    state.roundEls.clear();
    state.itemEls.clear();
    state.notes = [];
    state.pending.clear();
    state.focusKey = null;
    state.following = true;
  }

  /* ------------------------------------------------------------------ data */

  function api(path, body) {
    var options = { headers: { "Content-Type": "application/json" } };
    if (body) { options.method = "POST"; options.body = JSON.stringify(body); }
    return fetch(path, options)
      .then(function (r) {
        setConnection(r.ok ? "live" : "lost");
        return r.json().catch(function () { return {}; });
      })
      .catch(function () {
        // The daemon being gone is the single most likely reason anything here
        // fails, and a rejected promise nobody handles just leaves a blank page
        // with no explanation. Say so instead.
        setConnection("lost");
        return { error: "the scribe daemon is not responding", offline: true };
      });
  }

  function setConnection(stateName) {
    var dot = $("conn-dot");
    if (dot.dataset.state === stateName) return;
    dot.dataset.state = stateName;
    dot.title = stateName === "live" ? "connected" : "daemon not responding";
  }

  function load(id) {
    return api("/api/session?id=" + encodeURIComponent(id)).then(function (data) {
      if (data && data.offline) return showOffline(id);
      if (!data || data.error) return showProblem(data && data.error);
      state.head = data.head || {};
      applyHead();
      (data.pending || []).forEach(function (p) { state.pending.set(p.call_id, p); });
      applyRounds(data.rounds || [], []);
      requestAnimationFrame(function () {
        if (!jumpToFocus()) scrollToBottom();
        scheduleRail(true);
      });
      openStream(id);
    });
  }

  function applyHead() {
    var head = state.head;
    document.title = (head.title || "scribe") + " · scribe";
    $("session-title").textContent = head.title || "—";
    var facts = $("session-facts");
    clear(facts);
    [head.project, head.git_branch, head.round_count + " rounds", head.tool_count + " tools", head.usage_label + " tok"]
      .filter(Boolean)
      .forEach(function (bit) { facts.appendChild(el("span", null, bit)); });

    $("arm-toggle").setAttribute("aria-pressed", String(!!head.armed));
    $("arm-toggle").hidden = !head.remote_approval;
    $("dock").hidden = !head.reply_queue;
    renderQueue(head.queued || []);
    updateFollowPill();
  }

  function renderQueue(items) {
    var node = $("queue");
    clear(node);
    (items || []).forEach(function (text, index) {
      var row = el("div", "queued");
      row.appendChild(el("span", null, text));
      var drop = el("button", "btn", "×");
      drop.type = "button";
      drop.addEventListener("click", function () {
        api("/api/unqueue", { session_id: state.sessionId, index: index });
      });
      row.appendChild(drop);
      node.appendChild(row);
    });
    $("dock-note").textContent = (items && items.length)
      ? items.length + " message" + (items.length > 1 ? "s" : "") + " queued — delivered when this turn ends"
      : "";
  }

  function decide(callId, behavior) {
    api("/api/decision", { call_id: callId, behavior: behavior }).then(function () {
      var pending = state.pending.get(callId);
      if (pending) { pending.decided = true; pending.behavior = behavior; }
      rebuildNotes();
      scheduleRail(true);
      toast(behavior === "pass" ? "handed back to the terminal" : behavior + "ed");
    });
  }

  /* ------------------------------------------------------------------- SSE */

  function openStream(id) {
    if (state.es) { state.es.close(); state.es = null; }
    var es = new EventSource("/api/stream?id=" + encodeURIComponent(id));
    state.es = es;

    es.addEventListener("open", function () {
      state.reconnect = 0;
      $("conn-dot").dataset.state = "live";
    });
    es.addEventListener("error", function () {
      $("conn-dot").dataset.state = "lost";
    });
    es.addEventListener("rounds", function (ev) {
      var data = JSON.parse(ev.data);
      if (data.head) { state.head = data.head; applyHead(); }
      applyRounds(data.rounds || [], data.removed || []);
    });
    es.addEventListener("head", function (ev) {
      state.head = Object.assign({}, state.head, JSON.parse(ev.data));
      applyHead();
    });
    es.addEventListener("pending", function (ev) {
      var data = JSON.parse(ev.data);
      if (data.decided) state.pending.delete(data.call_id);
      else state.pending.set(data.call_id, data);
      rebuildNotes();
      scheduleRail(true);
      if (!data.decided && data.holding) notifyApproval(data);
    });
    es.addEventListener("queue", function (ev) {
      renderQueue(JSON.parse(ev.data).queued || []);
    });
    es.addEventListener("sessions", function (ev) {
      state.sessions = JSON.parse(ev.data);
      renderSidebar();
    });
    es.addEventListener("notify", function (ev) {
      var data = JSON.parse(ev.data);
      if (data.message) toast(data.message);
    });
  }

  function notifyApproval(pending) {
    toast(pending.tool_name + " needs approval");
  }

  /* --------------------------------------------------------------- controls */

  function bind() {
    window.addEventListener("scroll", onScroll, { passive: true });
    window.addEventListener("resize", function () { scheduleRail(); });

    new ResizeObserver(function () { scheduleRail(); }).observe($("column"));

    $("follow-pill").addEventListener("click", function () {
      state.following = true;
      scrollToBottom();
      updateFollowPill();
    });

    $("theme-toggle").addEventListener("click", toggleTheme);

    $("collapse-toggle").addEventListener("click", function () {
      state.folded = !state.folded;
      document.querySelectorAll(".tool-card, .thinking").forEach(function (node) {
        node.open = !state.folded;
      });
      scheduleRail(true);
    });

    $("log-link").addEventListener("click", function () {
      var path = state.head.log_path || "";
      if (!path) return;
      navigator.clipboard.writeText(path).then(function () { toast("copied " + path); },
        function () { toast(path); });
    });

    $("arm-toggle").addEventListener("click", function () {
      var next = $("arm-toggle").getAttribute("aria-pressed") !== "true";
      api("/api/arm", { session_id: state.sessionId, on: next }).then(function (r) {
        if (r.error) return toast(r.error);
        $("arm-toggle").setAttribute("aria-pressed", String(!!r.armed));
        toast(r.armed ? "approvals come here now" : "approvals go to the terminal");
      });
    });

    var filterTimer;
    $("session-filter").addEventListener("input", function (ev) {
      state.filter = ev.target.value;
      clearTimeout(filterTimer);
      // The project tree redraws instantly on the text we already have; the
      // full-text query is debounced because it crosses the wire.
      renderSidebar();
      filterTimer = setTimeout(runGlobalSearch, 220);
    });
    $("session-filter").addEventListener("keydown", function (ev) {
      if (ev.key === "Escape") {
        ev.target.value = "";
        state.filter = "";
        state.results = null;
        renderSidebar();
        ev.target.blur();
      }
    });

    var findTimer;
    $("find-input").addEventListener("input", function (ev) {
      clearTimeout(findTimer);
      var value = ev.target.value;
      findTimer = setTimeout(function () { runFind(value, true); }, 180);
    });
    $("find-input").addEventListener("keydown", function (ev) {
      if (ev.key === "Enter") {
        ev.preventDefault();
        gotoHit(state.hitIndex + (ev.shiftKey ? -1 : 1));
      } else if (ev.key === "Escape") {
        ev.target.value = "";
        runFind("", false);
        ev.target.blur();
      }
    });

    $("menu-toggle").addEventListener("click", function () {
      var bar = $("sidebar");
      bar.dataset.open = bar.dataset.open === "true" ? "false" : "true";
    });

    $("compose").addEventListener("submit", function (ev) {
      ev.preventDefault();
      sendReply();
    });
    $("compose-input").addEventListener("keydown", function (ev) {
      if (ev.key === "Enter" && (ev.metaKey || ev.ctrlKey)) { ev.preventDefault(); sendReply(); }
    });
    $("compose-input").addEventListener("input", function (ev) {
      ev.target.style.height = "auto";
      ev.target.style.height = Math.min(144, ev.target.scrollHeight) + "px";
    });

    // Clicking anywhere in a round focuses it and centres it — the behaviour
    // asked for explicitly, extended so a tool call also lights up its note.
    $("column").addEventListener("click", function (ev) {
      var card = ev.target.closest(".tool-card");
      if (card && !ev.target.closest("summary")) return;
      if (card && card.dataset.key) {
        var note = state.notes.find(function (n) { return n.key === card.dataset.key; });
        if (note) { state.focusKey = card.dataset.key; scheduleRail(true); }
      }
    });

    document.addEventListener("keydown", onKey);
    window.addEventListener("hashchange", fromHash);
  }

  function sendReply() {
    var input = $("compose-input");
    var text = input.value.trim();
    if (!text) return;
    api("/api/message", { session_id: state.sessionId, text: text }).then(function (r) {
      if (r.error) return toast(r.error);
      input.value = "";
      input.style.height = "auto";
    });
  }

  function toggleTheme() {
    var current = document.documentElement.dataset.theme;
    var systemDark = matchMedia("(prefers-color-scheme: dark)").matches;
    var next = current ? (current === "dark" ? "light" : "dark") : systemDark ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem("scribe-theme", next); } catch (e) {}
  }

  function onKey(ev) {
    var typing = /^(INPUT|TEXTAREA)$/.test(document.activeElement.tagName);
    if (ev.key === "/" && !typing) { ev.preventDefault(); $("find-input").focus(); return; }
    if (typing) return;
    if (ev.metaKey || ev.ctrlKey || ev.altKey) return;

    if (ev.key === "j" || ev.key === "k") {
      ev.preventDefault();
      stepRound(ev.key === "j" ? 1 : -1);
    } else if (ev.key === "t") {
      toggleTheme();
    } else if (ev.key === ".") {
      state.following = true;
      scrollToBottom();
      updateFollowPill();
    } else if (ev.key === "Escape") {
      state.focusKey = null;
      scheduleRail(true);
    }
  }

  function stepRound(direction) {
    var nodes = Array.from($("column").children);
    if (!nodes.length) return;
    var chrome = topbarHeight() + 8;
    var current = nodes.findIndex(function (node) {
      return node.getBoundingClientRect().bottom > chrome + 4;
    });
    if (current < 0) current = nodes.length - 1;
    var next = Math.min(nodes.length - 1, Math.max(0, current + direction));
    scrollToCenter(nodes[next]);
    nodes[next].classList.remove("flash");
    void nodes[next].offsetWidth;
    nodes[next].classList.add("flash");
  }

  /* ------------------------------------------------------------------ boot */

  function fromHash() {
    var match = /^#\/s\/([\w-]+)$/.exec(location.hash || "");
    if (match && match[1] !== state.sessionId) selectSession(match[1]);
  }

  function notice(lines) {
    var column = $("column");
    clear(column);
    clear($("rail-inner"));
    clear($("tethers"));
    var node = el("div", "empty");
    lines.forEach(function (line) {
      if (line.b) node.appendChild(el("b", null, line.b));
      else if (line.code) node.appendChild(el("code", null, line.code));
      else if (line.br) node.appendChild(document.createElement("br"));
      else node.appendChild(document.createTextNode(line.t || ""));
    });
    column.appendChild(node);
    return node;
  }

  function showProblem(message) {
    notice([{ b: "Could not load that session." }, { br: 1 }, { t: message || "Unknown error." }]);
  }

  // Retry quietly until the daemon comes back, then load for real. Restarting
  // `scribe serve` should be enough to fix the page without touching it.
  function showOffline(id) {
    notice([
      { b: "The scribe daemon isn't running." },
      { br: 1 },
      { t: "Start it with " },
      { code: "scribe serve" },
      { t: " — this page reconnects on its own." },
    ]);
    clearTimeout(state.retry);
    state.retry = setTimeout(function () {
      if (state.sessionId === id) load(id);
    }, 3000);
  }

  function showEmpty() {
    var column = $("column");
    clear(column);
    var node = el("div", "empty");
    node.innerHTML = "";
    node.appendChild(el("b", null, "No sessions yet."));
    node.appendChild(document.createElement("br"));
    node.appendChild(document.createTextNode("scribe reads Claude Code's own transcripts, so anything you have already run shows up here. Nothing found under "));
    node.appendChild(el("code", null, "~/.claude/projects"));
    node.appendChild(document.createTextNode("."));
    column.appendChild(node);
  }

  // A single self-contained file, with the session inlined and no server to
  // talk to. Everything that reads is kept; everything that writes is hidden,
  // because there is nothing on the other end of it.
  function bootStatic(snapshot) {
    installRenderer();
    bind();
    state.sessionId = snapshot.head.id;
    state.head = snapshot.head;
    state.sessions = [Object.assign({ live: false }, snapshot.head)];
    renderSidebar();
    applyHead();
    applyRounds(snapshot.rounds || [], []);
    document.body.dataset.static = "true";
    ["arm-toggle", "dock"].forEach(function (id) { $(id).hidden = true; });
    $("conn-dot").title = "exported file — not live";
    requestAnimationFrame(function () { scheduleRail(true); });
  }

  function boot() {
    if (window.SCRIBE_SNAPSHOT) return bootStatic(window.SCRIBE_SNAPSHOT);
    installRenderer();
    state.openProjects = loadOpenProjects();
    bind();
    api("/api/sessions").then(function (data) {
      state.sessions = (data && data.sessions) || [];
      renderSidebar();
      var match = /^#\/s\/([\w-]+)$/.exec(location.hash || "");
      var wanted = match ? match[1] : state.sessions.length ? state.sessions[0].id : null;
      if (!wanted) return showEmpty();
      state.sessionId = wanted;
      location.hash = "#/s/" + wanted;
      renderSidebar();
      load(wanted);
    });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();

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
    view: "session",    // session | board
    doneOpen: false,    // the board's done column, expanded or a strip
    boardTick: null,
    cfg: {},
    attachments: [],    // {id, name, mime, image, url, pending}
    compose: { mode: "", model: "" },  // picks for a session that has no process yet
    popover: null,
    commands: null,     // the slash catalogue for the current session
    commandsKey: "",
    newCwd: "",         // the folder picked on the home view
    stamp: "",          // the daemon's viewer-files hash, from the SSE hello
    stats: null,
    statsRange: "all",
    statsTab: "overview",
    draft: false,       // the open session has no transcript yet
    fileSeq: 0,
    fileTimer: null,
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

  function fmtBytes(n) {
    n = Number(n) || 0;
    if (!n) return "";
    if (n >= 1048576) return (n / 1048576).toFixed(1) + " MB";
    if (n >= 1024) return Math.round(n / 1024) + " KB";
    return n + " B";
  }

  function attachmentUrl(a) {
    if (a.path) return "/api/file?path=" + encodeURIComponent(a.path);
    return "/api/blob?session_id=" + encodeURIComponent(state.sessionId || "") + "&uuid=" + encodeURIComponent(a.uuid || "") + "&i=" + (a.index || 0);
  }

  // Pictures show as pictures and files as cards; both open in a new tab.
  // Built as DOM, never through marked, so nothing here is markup.
  function renderAttached(payload) {
    var wrap = el("div", "attached");
    (payload.attachments || []).forEach(function (a) {
      var url = attachmentUrl(a);
      var link = document.createElement("a");
      link.href = url;
      link.target = "_blank";
      link.rel = "noopener";
      if (a.kind === "image") {
        link.className = "shot";
        var img = document.createElement("img");
        img.src = url;
        img.alt = a.name || "pasted image";
        img.loading = "lazy";
        link.appendChild(img);
        link.title = a.name || "open the image";
      } else {
        link.className = "filecard";
        var ext = ((a.name || "").split(".").pop() || "").toUpperCase();
        if (ext === (a.name || "").toUpperCase()) ext = "FILE";
        link.appendChild(el("span", "filecard-ext", ext.slice(0, 5)));
        var text = el("span", "filecard-text");
        text.appendChild(el("span", "filecard-name", a.name || "file"));
        var meta = [a.media_type || "", fmtBytes(a.size)].filter(Boolean).join(" · ");
        if (meta) text.appendChild(el("span", "filecard-meta", meta));
        link.appendChild(text);
        link.title = a.path || "";
      }
      wrap.appendChild(link);
    });
    return wrap;
  }

  function renderRound(payload) {
    var node = el("article", "round");
    node.dataset.index = payload.index;
    node.dataset.source = payload.source;
    node.id = "round-" + payload.index;

    var head = el("div", "round-head");
    head.appendChild(el("span", "round-n", String(payload.index)));
    var who = { web: "you · web", peer: "another session", system: "session" }[payload.source] || "you";
    head.appendChild(el("span", "round-who", who));
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
    if (payload.attachments && payload.attachments.length) {
      node.appendChild(renderAttached(payload));
    } else if (payload.images) {
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
      var here = sessions.find(function (s) { return s.cwd; });
      if (here) {
        var plus = el("span", "pnew", "+");
        plus.title = "new session in " + here.cwd;
        plus.setAttribute("role", "button");
        plus.addEventListener("click", function (ev) {
          ev.stopPropagation();
          showNew(here.cwd);
          $("sidebar").dataset.open = "false";
        });
        header.appendChild(plus);
      }
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
        button.setAttribute("aria-current", String(state.view !== "board" && session.id === state.sessionId));
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
    if (!id) return;
    if (id === state.sessionId && state.view === "session") return;
    leaveBoard();
    closePopover();
    clearAttachments();
    state.compose = { mode: "", model: "" };
    state.commands = null;
    state.draft = false;
    state.view = "session";
    document.body.dataset.view = "session";
    $("arm-toggle").hidden = false;
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

  /* ----------------------------------------------------------------- board */

  // Live sessions as cards, one column per thing a session can be waiting
  // on. The column comes from the server (`phase`), which reads it off the
  // transcript's tail; the board only draws. It is redrawn whole on every
  // change — unlike the conversation, a card holds no state worth keeping.
  var COLUMNS = [
    { key: "needs_you", label: "needs you", empty: "nothing is waiting on you" },
    { key: "planning", label: "planning", empty: "no session is in plan mode" },
    { key: "working", label: "working", empty: "nothing is running" },
    { key: "your_turn", label: "your turn", empty: "no replies waiting to be read" },
  ];
  var DONE_CAP = 60;

  function ago(ts) {
    var d = (Date.now() - Date.parse(ts)) / 1000;
    if (!(d >= 0)) return "";
    if (d < 60) return Math.floor(d) + "s";
    if (d < 3600) return Math.floor(d / 60) + "m";
    if (d < 86400) return Math.floor(d / 3600) + "h";
    return Math.floor(d / 86400) + "d";
  }

  function elapsed(ts) {
    var d = (Date.now() - Date.parse(ts)) / 1000;
    if (!(d >= 0)) return "";
    var m = Math.floor(d / 60), sec = Math.floor(d % 60);
    if (d < 3600) return m + "m " + (sec < 10 ? "0" : "") + sec + "s";
    return Math.floor(d / 3600) + "h " + (m % 60) + "m";
  }

  function clockText(node) {
    var mode = node.dataset.mode;
    if (mode === "countdown") {
      var left = Math.max(0, Math.round((Number(node.dataset.until) - Date.now()) / 1000));
      return left + "s";
    }
    if (mode === "elapsed") return elapsed(node.dataset.from);
    return ago(node.dataset.from);
  }

  function clock(mode, from, until) {
    var node = el("span", "clock");
    node.dataset.mode = mode;
    if (from) node.dataset.from = from;
    if (until) node.dataset.until = String(until);
    if (mode === "countdown") node.classList.add("warn");
    node.textContent = clockText(node);
    return node;
  }

  function tickClocks() {
    document.querySelectorAll("#board .clock").forEach(function (node) {
      var text = clockText(node);
      if (node.textContent !== text) node.textContent = text;
    });
  }

  function cardStatus(session) {
    var st = session.state || {};
    var status = el("div", "status");
    var pending = session.pending && session.pending[0];
    var kind, text, chip;

    if (session.phase === "needs_you") {
      if (pending) {
        kind = "approve"; chip = pending.tool_name; text = pending.subject || pending.tool_name;
      } else if (st.activity_kind === "terminal") {
        kind = "terminal"; chip = "terminal"; text = st.activity;
      } else {
        kind = st.activity_kind || "ask"; chip = kind === "plan" ? "plan" : "ask"; text = st.activity;
      }
    } else if (session.phase === "working" || session.phase === "planning") {
      status.appendChild(el("span", "pulse"));
      kind = st.activity_kind || "wait"; chip = st.tool || (session.phase === "planning" ? "plan" : "…");
      text = st.activity || "";
    } else {
      kind = st.activity_kind === "stop" ? "stop" : "reply";
      chip = st.activity_kind === "stop" ? "interrupted" : (st.reply ? "replied" : "idle");
      text = st.reply || st.activity || "";
    }

    var k = el("span", "k", chip);
    k.dataset.kind = kind;
    status.appendChild(k);
    status.appendChild(el("span", "text", text));

    if (pending && pending.seconds_left > 0) {
      status.appendChild(clock("countdown", null, Date.now() + pending.seconds_left * 1000));
    } else if (session.phase === "working" || session.phase === "planning") {
      status.appendChild(clock("elapsed", st.turn_started || st.since || session.updated));
    } else {
      status.appendChild(clock("ago", st.since || session.updated));
    }
    return status;
  }

  function cardActions(session) {
    var act = el("span", "act");
    var pending = session.pending && session.pending[0];
    function button(label, primary, onClick) {
      var b = el("button", "btn" + (primary ? " primary" : ""), label);
      b.type = "button";
      b.addEventListener("click", function (ev) { ev.stopPropagation(); onClick(); });
      act.appendChild(b);
    }
    if (pending) {
      button("deny", false, function () { decideFromBoard(pending.call_id, "deny"); });
      button("approve", true, function () { decideFromBoard(pending.call_id, "allow"); });
    } else if (session.phase === "needs_you") {
      // A terminal dialog is answered in the terminal; here you can only look.
      var terminal = session.state && session.state.activity_kind === "terminal";
      button(terminal ? "open" : "answer", !terminal, function () { selectSession(session.id); });
    } else if (session.phase === "your_turn") {
      if (session.reply_via) {
        button("reply", false, function () { replyFromBoard(session.id); });
      }
      button("read", true, function () { selectSession(session.id); });
    } else if (session.phase === "done") {
      if (session.reply_via === "spawn") {
        button("continue", false, function () { replyFromBoard(session.id); });
      }
    } else {
      button("open", false, function () { selectSession(session.id); });
    }
    return act;
  }

  function renderCard(session) {
    var card = el("article", "card");
    card.tabIndex = 0;
    card.setAttribute("role", "button");

    var head = el("div", "head");
    head.appendChild(el("span", "proj", session.project || ""));
    if (session.git_branch) head.appendChild(el("span", "branch", session.git_branch));
    head.appendChild(el("span", "id", (session.id || "").slice(0, 8)));
    card.appendChild(head);

    card.appendChild(el("div", "title", session.title || session.id.slice(0, 8)));

    if (session.phase !== "done") card.appendChild(cardStatus(session));

    var foot = el("div", "foot");
    if (session.phase === "done") {
      foot.appendChild(el("span", null, dateOf(session.updated) + " " + timeOf(session.updated)));
    } else {
      foot.appendChild(el("span", null, "since " + timeOf(session.started)));
    }
    if (session.queued) foot.appendChild(el("span", "queued", session.queued + " queued"));
    if (session.archived) {
      var kept = el("span", "kept", "kept");
      kept.title = "Claude Code deleted the original — preserved by scribe";
      foot.appendChild(kept);
    }
    foot.appendChild(cardActions(session));
    card.appendChild(foot);

    card.addEventListener("click", function () { selectSession(session.id); });
    card.addEventListener("keydown", function (ev) {
      if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); selectSession(session.id); }
    });
    return card;
  }

  function columnNode(key, label, count, items, emptyText) {
    var section = el("section", "col");
    section.dataset.phase = key;
    var head = el("div", "colhead");
    head.appendChild(el("span", "dot"));
    head.appendChild(el("span", null, label));
    head.appendChild(el("span", "count", String(count)));
    section.appendChild(head);
    var cards = el("div", "cards");
    if (!items.length) cards.appendChild(el("div", "col-empty", emptyText));
    items.forEach(function (session) { cards.appendChild(renderCard(session)); });
    section.appendChild(cards);
    return section;
  }

  function renderDone(done) {
    var kept = done.filter(function (s) { return s.archived; }).length;
    if (!state.doneOpen) {
      var strip = el("section", "col done-strip");
      strip.dataset.phase = "done";
      strip.title = "every session with no process behind it, newest first";
      strip.setAttribute("role", "button");
      strip.tabIndex = 0;
      strip.appendChild(el("span", "dot"));
      strip.appendChild(el("span", "n", String(done.length)));
      strip.appendChild(el("span", "vlabel", "done"));
      if (kept) strip.appendChild(el("span", "kept", kept + " kept"));
      var open = function () {
        state.doneOpen = true;
        renderBoard();
        var col = $("board").querySelector('.col[data-phase="done"]');
        if (col) col.scrollIntoView({ inline: "end", block: "nearest", behavior: reduceMotion ? "auto" : "smooth" });
      };
      strip.addEventListener("click", open);
      strip.addEventListener("keydown", function (ev) {
        if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); open(); }
      });
      return strip;
    }
    var shown = done.slice(0, DONE_CAP);
    var section = columnNode("done", "done", done.length, shown, "nothing finished yet");
    var fold = el("button", "fold", "⇥");
    fold.type = "button";
    fold.title = "collapse";
    fold.addEventListener("click", function () { state.doneOpen = false; renderBoard(); });
    section.querySelector(".colhead").appendChild(fold);
    if (done.length > shown.length) {
      section.appendChild(el("div", "col-more", (done.length - shown.length) + " more in the list"));
    }
    return section;
  }

  function renderBoard() {
    if (state.view !== "board") return;
    var board = $("board");
    clear(board);

    var groups = {};
    COLUMNS.forEach(function (c) { groups[c.key] = []; });
    var done = [];
    state.sessions.forEach(function (session) {
      if (groups[session.phase]) groups[session.phase].push(session);
      else done.push(session);
    });
    // Whoever has waited longest on you comes first; everything else newest first.
    var sinceOf = function (s) { return Date.parse((s.state && s.state.since) || s.updated || 0) || 0; };
    groups.needs_you.sort(function (a, b) { return sinceOf(a) - sinceOf(b); });
    ["planning", "working", "your_turn"].forEach(function (key) {
      groups[key].sort(function (a, b) { return sinceOf(b) - sinceOf(a); });
    });

    COLUMNS.forEach(function (col) {
      board.appendChild(columnNode(col.key, col.label, groups[col.key].length, groups[col.key], col.empty));
    });
    board.appendChild(renderDone(done));

    var live = state.sessions.filter(function (s) { return s.phase && s.phase !== "done"; }).length;
    var projects = new Set(state.sessions.filter(function (s) { return s.phase !== "done"; })
      .map(function (s) { return s.project; })).size;
    var facts = $("session-facts");
    clear(facts);
    [live + " live", groups.needs_you.length + " need you", projects + " project" + (projects === 1 ? "" : "s")]
      .forEach(function (bit) { facts.appendChild(el("span", null, bit)); });
    var needs = groups.needs_you.length;
    document.title = (needs ? "(" + needs + ") " : "") + "board · scribe";
  }

  function decideFromBoard(callId, behavior) {
    api("/api/decision", { call_id: callId, behavior: behavior }).then(function (r) {
      if (r && r.error) return toast(r.error);
      toast(behavior === "allow" ? "approved" : "denied");
    });
  }

  function replyFromBoard(id) {
    selectSession(id);
    requestAnimationFrame(function () {
      var input = $("compose-input");
      if (input && !$("dock").hidden) input.focus();
    });
  }

  function showBoard() {
    if (state.view === "board") return;
    state.view = "board";
    document.body.dataset.view = "board";
    $("board").hidden = false;
    $("session-title").textContent = "board";
    $("board-toggle").setAttribute("aria-pressed", "true");
    if (location.hash !== "#/board") location.hash = "#/board";
    // Subscribe with no session: the stream then carries only what every
    // page gets — the session list and card updates.
    openStream("");
    renderBoard();
    renderSidebar();
    clearInterval(state.boardTick);
    state.boardTick = setInterval(tickClocks, 1000);
  }

  function leaveBoard() {
    if (state.view !== "board") return;
    state.view = "session";
    document.body.dataset.view = "session";
    $("board").hidden = true;
    $("board-toggle").setAttribute("aria-pressed", "false");
    clearInterval(state.boardTick);
    state.boardTick = null;
  }

  function toggleBoard() {
    if (state.view !== "board") return showBoard();
    if (state.sessionId) selectSession(state.sessionId);
    else showHome();
  }

  function mergeCard(card) {
    var index = state.sessions.findIndex(function (s) { return s.id === card.id; });
    if (index < 0) state.sessions.unshift(card);
    else state.sessions[index] = card;
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
      restoreDraft();
      if (data.draft) {
        // Claude has not written the transcript yet. The `sessions` event
        // says when it has; `load` runs again then.
        state.draft = true;
        notice([{ b: "Starting Claude…" }, { br: 1 }, { t: "The conversation appears here as soon as the first turn is written." }]);
        openStream(id);
        return;
      }
      state.draft = false;
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
    applyDock(head);
    updateFollowPill();
  }

  // How a message typed here reaches the session. The daemon decides
  // (`reply_via`); the page only says what will happen when you press send.
  var DOCK_HINT = {
    driver: "message this session…",
    inbox: "message this session — Claude gets it now…",
    queue: "reply from here — delivered when Claude finishes this turn…",
    spawn: "continue this session — Claude starts again in its folder and picks up here…",
  };

  function applyDock(head) {
    var via = head.reply_via || "";
    var dock = $("dock");
    dock.hidden = !via;
    dock.dataset.via = via;
    var input = $("compose-input");
    input.placeholder = DOCK_HINT[via] || "";
    applyCaps(head);
    renderQueue(head.queued || []);
    var note = "";
    if (via === "inbox" && head.inbox_held) {
      note = "this session runs with permissions bypassed, so Claude Code asks in the terminal before delivering a message from here";
    } else if (via === "inbox") {
      note = "running in a terminal — messages go straight in; mode and model are set there";
    } else if (via === "spawn") {
      note = "no Claude process is behind this session — sending starts one in its folder, and it stays for follow-ups";
    } else if (via === "driver" && head.driver) {
      note = head.driver.state === "running"
        ? "Claude is working on your message…"
        : "a Claude process of scribe's own is behind this session";
    }
    if (via !== "queue" || !(head.queued || []).length) $("dock-note").textContent = note;
  }

  function onDelivery(d) {
    if (d.status === "starting") $("dock-note").textContent = "starting Claude…";
    else if (d.status === "delivered" && d.via === "inbox") toast(d.held ? "sent — approve it in the terminal to deliver" : "delivered");
    else if (d.status === "delivered") $("dock-note").textContent = "Claude is working on your message…";
    else if (d.status === "done") applyDock(state.head);
    else if (d.status === "failed") toast("could not continue: " + (d.error || "unknown error"));
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
    var note = (items && items.length)
      ? items.length + " message" + (items.length > 1 ? "s" : "") + " queued — delivered when this turn ends"
      : "";
    if (note || $("dock").dataset.via === "queue") $("dock-note").textContent = note;
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
    // The daemon stamps its viewer files. A different stamp after a
    // reconnect means it was restarted on new code: reload rather than run
    // this script against payloads it does not understand.
    es.addEventListener("hello", function (ev) {
      var stamp = (JSON.parse(ev.data) || {}).stamp || "";
      if (!stamp) return;
      if (state.stamp && state.stamp !== stamp) { location.reload(); return; }
      state.stamp = stamp;
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
    es.addEventListener("delivery", function (ev) {
      onDelivery(JSON.parse(ev.data));
    });
    es.addEventListener("sessions", function (ev) {
      state.sessions = JSON.parse(ev.data);
      renderSidebar();
      renderBoard();
      if (state.draft && state.sessionId) {
        var mine = state.sessions.find(function (s) { return s.id === state.sessionId; });
        if (mine && !mine.draft) load(state.sessionId);
      }
    });
    es.addEventListener("card", function (ev) {
      mergeCard(JSON.parse(ev.data));
      renderSidebar();
      renderBoard();
    });
    es.addEventListener("notify", function (ev) {
      var data = JSON.parse(ev.data);
      if (data.message) toast(data.message);
    });
  }

  function notifyApproval(pending) {
    toast(pending.tool_name + " needs approval");
  }


  /* -------------------------------------------------------------- composer */

  // One popover for every menu the composer opens: the mode and model
  // pickers, and (later) the slash-command and @-file lists. Anchored above
  // its button because the dock sits at the bottom of the page. Keyboard:
  // ↑↓ move, Enter/Tab pick, Esc close; clicking elsewhere closes.
  function openPopover(opts) {
    closePopover();
    var node = el("div", "popover");
    node.setAttribute("role", "listbox");
    if (opts.cls) node.classList.add(opts.cls);
    var handle = { node: node, items: [], index: -1, opts: opts };

    function render() {
      clear(node);
      if (!handle.items.length) {
        node.appendChild(el("div", "popover-empty", opts.empty || "nothing matches"));
        return;
      }
      handle.items.forEach(function (item, i) {
        var row = el("div", "popover-item" + (item.disabled ? " disabled" : "") + (i === handle.index ? " active" : ""));
        row.setAttribute("role", "option");
        row.dataset.index = String(i);
        var head = el("div", "popover-head");
        head.appendChild(el("span", "popover-label", item.label));
        if (item.hint) head.appendChild(el("span", "popover-hint", item.hint));
        if (item.tag) head.appendChild(el("span", "chip", item.tag));
        row.appendChild(head);
        if (item.detail) row.appendChild(el("div", "popover-detail", item.detail));
        row.addEventListener("mousedown", function (ev) { ev.preventDefault(); });
        row.addEventListener("click", function () { handle.pick(i); });
        node.appendChild(row);
      });
      var active = node.querySelector(".popover-item.active");
      if (active) active.scrollIntoView({ block: "nearest" });
    }

    handle.update = function (items) {
      handle.items = items || [];
      var firstEnabled = handle.items.findIndex(function (it) { return !it.disabled; });
      handle.index = firstEnabled;
      render();
    };
    handle.move = function (delta) {
      var n = handle.items.length;
      if (!n) return;
      var i = handle.index;
      for (var tries = 0; tries < n; tries++) {
        i = Compose.step(i, delta, n);
        if (!handle.items[i].disabled) break;
      }
      handle.index = i;
      render();
    };
    handle.pick = function (i) {
      var item = handle.items[i == null ? handle.index : i];
      if (!item || item.disabled) {
        if (item && item.disabled && item.why) toast(item.why);
        return;
      }
      closePopover();
      opts.onPick(item);
    };
    handle.close = function () {
      if (state.popover !== handle) return;
      state.popover = null;
      node.remove();
      document.removeEventListener("mousedown", onOutside, true);
      if (opts.onClose) opts.onClose();
    };
    function onOutside(ev) {
      if (!node.contains(ev.target) && !(opts.anchor && opts.anchor.contains(ev.target))) handle.close();
    }

    document.body.appendChild(node);
    var box = (opts.anchor || $("compose")).getBoundingClientRect();
    node.style.left = Math.max(8, Math.min(box.left, window.innerWidth - 8 - 360)) + "px";
    node.style.bottom = (window.innerHeight - box.top + 6) + "px";
    document.addEventListener("mousedown", onOutside, true);
    state.popover = handle;
    handle.update(opts.items || []);
    return handle;
  }

  function closePopover() {
    if (state.popover) state.popover.close();
  }

  var MODE_LABEL = {
    "default": "manual",
    acceptEdits: "accept edits",
    plan: "plan",
    auto: "auto",
    bypassPermissions: "bypass",
    dontAsk: "don't ask",
  };
  var MODE_DETAIL = {
    "default": "asks before each tool that needs permission",
    acceptEdits: "file edits go through; commands still ask",
    plan: "reads and plans; writes nothing until the plan is approved",
    auto: "Claude Code decides what is safe to run",
    bypassPermissions: "nothing asks; nothing is held",
  };
  var MODEL_DETAIL = {
    "default": "whatever this account uses by default",
    fable: "Fable 5.1 — the most capable",
    opus: "Opus 5",
    sonnet: "Sonnet 5",
    haiku: "Haiku 4.5 — fastest and cheapest",
  };

  function modeLabel(value) { return MODE_LABEL[value] || value || "mode"; }

  function modelLabel(value) {
    if (!value || value === "default") return "default";
    var family = /fable|opus|sonnet|haiku/.exec(value);
    if (!family) return value;
    var version = /-(\d+)-(\d+)/.exec(value);
    return family[0] + (version ? " " + version[1] + "." + version[2] : "");
  }

  // What the composer offers is `head.caps`, decided by the daemon per
  // channel. The page only draws it — and remembers a pick for a session
  // that has no process yet, to send along with the first message.
  function applyCaps(head) {
    var caps = head.caps || {};
    var via = head.reply_via || "";
    var mode = caps.mode || {};
    var model = caps.model || {};
    var modeValue = (via === "spawn" && state.compose.mode) || mode.value || "";
    var modelValue = (via === "spawn" && state.compose.model) || model.value || "";

    var modeBtn = $("mode-btn");
    $("mode-label").textContent = modeLabel(modeValue);
    modeBtn.disabled = !mode.settable;
    modeBtn.dataset.value = modeValue;
    modeBtn.title = mode.settable ? "Permission mode (Shift+Tab cycles)" : "Permission mode — change it in the terminal";

    var modelBtn = $("model-btn");
    $("model-label").textContent = modelLabel(modelValue);
    modelBtn.disabled = !model.settable;
    modelBtn.title = model.settable ? "Model" : "Model — change it in the terminal";

    $("attach-btn").hidden = !caps.attachments || caps.attachments === "none";
    $("attach-btn").title = caps.attachments === "blocks"
      ? "Attach files (or paste, or drop them here) — images are shown to Claude"
      : "Attach files (or paste, or drop them here) — Claude reads them by path";

    var running = !!(head.driver && head.driver.state === "running");
    $("stop-btn").hidden = !(caps.interrupt && running);

    var channel = $("channel");
    var text = { driver: "page session", inbox: "terminal session", queue: "via stop hook", spawn: "starts Claude here" }[via] || "";
    channel.textContent = text;
    channel.dataset.via = via;
    channel.title = { driver: "a Claude process of scribe's own is behind this session",
                      inbox: "a terminal session with an inbox: messages go straight in",
                      queue: "delivered through the Stop hook when this turn ends",
                      spawn: "no process yet — the first message starts one in the session's folder" }[via] || "";
  }

  function pickMode() {
    var caps = state.head.caps || {};
    var mode = caps.mode || {};
    if (!mode.settable) return;
    var current = $("mode-btn").dataset.value;
    openPopover({
      anchor: $("mode-btn"),
      cls: "menu",
      items: (mode.choices || []).map(function (value) {
        return { label: modeLabel(value), detail: MODE_DETAIL[value] || "", value: value, tag: value === current ? "current" : "" };
      }),
      onPick: function (item) { setMode(item.value); },
    });
  }

  function cycleMode() {
    var caps = state.head.caps || {};
    var mode = caps.mode || {};
    if (!mode.settable || !(mode.choices || []).length) return;
    var choices = mode.choices;
    var i = choices.indexOf($("mode-btn").dataset.value);
    setMode(choices[(i + 1) % choices.length]);
  }

  function setMode(value) {
    if (state.head.reply_via === "spawn") {
      state.compose.mode = value;
      applyCaps(state.head);
      return;
    }
    api("/api/session/mode", { session_id: state.sessionId, mode: value }).then(function (r) {
      if (r.error) return toast(r.error);
      $("mode-label").textContent = modeLabel(r.mode || value);
      $("mode-btn").dataset.value = r.mode || value;
      toast("mode: " + modeLabel(r.mode || value));
    });
  }

  function pickModel() {
    var caps = state.head.caps || {};
    var model = caps.model || {};
    if (!model.settable) return;
    var current = modelLabel($("model-label").textContent);
    openPopover({
      anchor: $("model-btn"),
      cls: "menu",
      items: (model.choices || []).map(function (value) {
        return { label: value, detail: MODEL_DETAIL[value] || "", value: value, tag: modelLabel(value) === current || value === current ? "current" : "" };
      }),
      onPick: function (item) { setModel(item.value); },
    });
  }

  function setModel(value) {
    if (state.head.reply_via === "spawn") {
      state.compose.model = value;
      applyCaps(state.head);
      return;
    }
    api("/api/session/model", { session_id: state.sessionId, model: value }).then(function (r) {
      if (r.error) return toast(r.error);
      $("model-label").textContent = modelLabel(value);
      toast("model: " + modelLabel(value));
    });
  }

  function stopTurn() {
    if ($("stop-btn").hidden) return;
    api("/api/interrupt", { session_id: state.sessionId }).then(function (r) {
      if (r.error) return toast(r.error);
      toast("stopped");
    });
  }

  // -- attachments --------------------------------------------------------

  function attachFiles(files) {
    Array.from(files || []).forEach(function (file) {
      var entry = { id: null, name: file.name || "pasted", mime: file.type || "", image: /^image\//.test(file.type || ""), url: null, pending: true };
      if (entry.image) { try { entry.url = URL.createObjectURL(file); } catch (e) {} }
      state.attachments.push(entry);
      renderAttachments();
      fetch("/api/upload?session_id=" + encodeURIComponent(state.sessionId || "new") + "&name=" + encodeURIComponent(entry.name), {
        method: "POST",
        headers: { "Content-Type": file.type || "application/octet-stream" },
        body: file,
      })
        .then(function (r) { return r.json(); })
        .then(function (r) {
          if (r.error) { dropAttachment(entry); return toast(r.error); }
          entry.id = r.id;
          entry.name = r.name;
          entry.image = !!r.image;
          entry.pending = false;
          renderAttachments();
        })
        .catch(function () { dropAttachment(entry); toast("upload failed"); });
    });
  }

  function dropAttachment(entry) {
    var i = state.attachments.indexOf(entry);
    if (i >= 0) state.attachments.splice(i, 1);
    if (entry.url) { try { URL.revokeObjectURL(entry.url); } catch (e) {} }
    renderAttachments();
  }

  function clearAttachments() {
    state.attachments.slice().forEach(dropAttachment);
  }

  function renderAttachments() {
    var strip = $("attachments");
    clear(strip);
    strip.hidden = !state.attachments.length;
    state.attachments.forEach(function (entry) {
      var item = el("div", "attachment" + (entry.pending ? " pending" : ""));
      if (entry.image && entry.url) {
        var img = document.createElement("img");
        img.src = entry.url;
        img.alt = entry.name;
        item.appendChild(img);
      } else {
        item.appendChild(el("span", "attachment-icon", "▤"));
      }
      item.appendChild(el("span", "attachment-name", entry.name));
      var remove = el("button", "attachment-remove", "×");
      remove.type = "button";
      remove.title = "remove";
      remove.addEventListener("click", function () { dropAttachment(entry); });
      item.appendChild(remove);
      strip.appendChild(item);
    });
  }


  // -- `/` commands and `@` files ---------------------------------------------

  // The catalogue is the daemon's (`/api/commands`): disk plus whatever a
  // driver reported. Cached per session and per "live or not", because a
  // driver starting mid-session changes what is on offer.
  function loadCommands() {
    var key = state.sessionId + ":" + ((state.head.caps || {}).commands || "");
    if (state.commands && state.commandsKey === key) return Promise.resolve(state.commands);
    return api("/api/commands?session_id=" + encodeURIComponent(state.sessionId || "")).then(function (r) {
      if (r.error) return [];
      state.commands = r.commands || [];
      state.commandsKey = key;
      return state.commands;
    });
  }

  function currentToken(kind) {
    var input = $("compose-input");
    return kind === "slash"
      ? Compose.slashToken(input.value, input.selectionStart)
      : Compose.mentionToken(input.value, input.selectionStart);
  }

  function suggest() {
    if (state.popover && state.popover.opts.cls === "menu") return;
    var slash = currentToken("slash");
    if (slash) return suggestCommands(slash);
    var mention = currentToken("mention");
    if (mention) return suggestFiles(mention);
    if (state.popover && state.popover.opts.cls === "complete") closePopover();
  }

  function showComplete(kind, items, empty) {
    var pop = state.popover;
    if (pop && pop.opts.cls === "complete" && pop.opts.kind === kind) {
      pop.update(items);
      return;
    }
    openPopover({
      anchor: $("compose"),
      cls: "complete",
      kind: kind,
      items: items,
      empty: empty,
      onPick: function (item) { completeWith(kind, item.insert); },
    });
  }

  function completeWith(kind, insert) {
    var input = $("compose-input");
    var token = currentToken(kind);
    if (!token) return;
    var out = Compose.complete(input.value, token, insert);
    input.value = out.text;
    input.setSelectionRange(out.caret, out.caret);
    autogrow(input);
    saveDraft(input.value);
    input.focus();
  }

  var SCOPE_TAG = { project: "project", user: "yours", plugin: "plugin" };

  function suggestCommands(token) {
    loadCommands().then(function (list) {
      if (!currentToken("slash")) return;
      var ranked = Compose.rank(token.query, list, "name").slice(0, 40);
      var items = ranked.map(function (c) {
        return {
          label: "/" + c.name,
          hint: c.argument_hint,
          detail: c.description,
          tag: c.scope === "claude" ? (c.kind === "builtin" ? "built-in" : "bundled") : SCOPE_TAG[c.scope] || c.scope,
          disabled: !c.available,
          why: c.why,
          insert: "/" + c.name,
        };
      });
      showComplete("slash", items, "no command matches");
    });
  }

  function suggestFiles(token) {
    clearTimeout(state.fileTimer);
    var seq = ++state.fileSeq;
    state.fileTimer = setTimeout(function () {
      api("/api/files?session_id=" + encodeURIComponent(state.sessionId || "") + "&q=" + encodeURIComponent(token.query)).then(function (r) {
        if (seq !== state.fileSeq || !currentToken("mention")) return;
        var items = (r.files || []).map(function (f) {
          return { label: "@" + f.path, insert: "@" + f.path };
        });
        showComplete("mention", items, "no file matches");
      });
    }, 120);
  }

  // A command the channel cannot carry is stopped here, with the reason,
  // rather than sent to a session that would read it as prose.
  function commandBlocked(text) {
    var token = Compose.slashToken(text, null);
    if (!token || !state.commands) return "";
    var entry = state.commands.find(function (c) { return c.name === token.query; });
    if (entry && !entry.available) return "/" + entry.name + ": " + entry.why;
    return "";
  }

  function bindComposer() {
    $("attach-btn").addEventListener("click", function () { $("file-input").click(); });
    $("file-input").addEventListener("change", function (ev) {
      attachFiles(ev.target.files);
      ev.target.value = "";
    });
    $("mode-btn").addEventListener("click", pickMode);
    $("model-btn").addEventListener("click", pickModel);
    $("stop-btn").addEventListener("click", stopTurn);

    var input = $("compose-input");
    input.addEventListener("paste", function (ev) {
      var files = ev.clipboardData && ev.clipboardData.files;
      if (files && files.length) { ev.preventDefault(); attachFiles(files); }
    });
    var dock = $("dock");
    ["dragenter", "dragover"].forEach(function (name) {
      dock.addEventListener(name, function (ev) {
        if (!ev.dataTransfer || !Array.from(ev.dataTransfer.types || []).includes("Files")) return;
        ev.preventDefault();
        dock.dataset.drop = "true";
      });
    });
    dock.addEventListener("dragleave", function (ev) {
      if (!dock.contains(ev.relatedTarget)) dock.dataset.drop = "false";
    });
    dock.addEventListener("drop", function (ev) {
      dock.dataset.drop = "false";
      if (!ev.dataTransfer || !ev.dataTransfer.files.length) return;
      ev.preventDefault();
      attachFiles(ev.dataTransfer.files);
    });
  }


  /* ------------------------------------------------------------- new session */

  // The "what's up next" view: pick a folder, write the first message, and a
  // driver starts a fresh session there. Until Claude writes the transcript
  // the session is a draft card; the daemon swaps in the real one.
  function showNew(cwd) {
    showHome(cwd, true);
  }

  // The home: what you have done, over every session on the machine, and
  // where to start the next one. `#/` lands here; `#/new` too, with the
  // composer focused.
  function showHome(cwd, focus) {
    var already = state.view === "home";
    leaveBoard();
    closePopover();
    clearAttachments();
    state.view = "home";
    document.body.dataset.view = "home";
    state.sessionId = null;
    state.draft = false;
    state.commands = null;
    state.compose = { mode: "", model: "" };
    state.newCwd = cwd || state.newCwd || "";
    if (location.hash !== "#/" && location.hash !== "#/new") location.hash = "#/";
    document.title = "scribe";
    $("session-title").textContent = "home";
    clear($("session-facts"));
    $("arm-toggle").hidden = true;
    openStream("");
    resetView();

    var column = $("column");
    clear(column);
    clear($("rail-inner"));
    clear($("tethers"));
    var home = el("div", "home");
    home.appendChild(el("h2", "home-title", "What's up next?"));
    var stats = el("section", "stats");
    stats.id = "stats";
    home.appendChild(stats);
    var needs = el("div", "needs");
    needs.id = "needs";
    home.appendChild(needs);
    column.appendChild(home);
    loadStats(state.statsRange || "all", state.statsTab || "overview");

    var panel = el("div", "newpanel");
    panel.appendChild(el("p", "lede", "Pick a folder, write the first message, and Claude starts there. The session lands in the list on the left like any other."));
    var field = el("label", "cwd-field");
    field.appendChild(el("span", "label", "start in"));
    var input = document.createElement("input");
    input.id = "new-cwd";
    input.type = "text";
    input.autocomplete = "off";
    input.spellcheck = false;
    input.placeholder = "~/Documents/GitHub/…";
    input.value = state.newCwd;
    input.setAttribute("list", "recent-cwds");
    field.appendChild(input);
    var status = el("span", "cwd-status");
    field.appendChild(status);
    panel.appendChild(field);
    var list = document.createElement("datalist");
    list.id = "recent-cwds";
    panel.appendChild(list);
    var recent = el("div", "recent");
    panel.appendChild(recent);
    home.appendChild(panel);

    function check() {
      var value = input.value.trim();
      state.newCwd = value;
      status.textContent = "";
      status.dataset.ok = "";
      if (!value) return;
      api("/api/fs?path=" + encodeURIComponent(value)).then(function (r) {
        if (input.value.trim() !== value) return;
        status.dataset.ok = r.ok ? "true" : "false";
        status.textContent = r.ok ? "✓" : "not a folder";
      });
    }
    input.addEventListener("input", check);
    input.addEventListener("keydown", function (ev) {
      if (ev.key === "Enter") { ev.preventDefault(); focusComposer(); }
    });

    api("/api/new").then(function (r) {
      if (r.error) return;
      state.head = { id: null, title: "New session", reply_via: "spawn", caps: r.caps || {}, queued: [] };
      applyDock(state.head);
      $("compose-input").placeholder = "describe a task or ask a question…";
      (r.recent || []).forEach(function (item) {
        var opt = document.createElement("option");
        opt.value = item.cwd;
        list.appendChild(opt);
        var chip = el("button", "recent-cwd", item.project);
        chip.type = "button";
        chip.title = item.cwd;
        chip.addEventListener("click", function () {
          input.value = item.cwd;
          check();
          focusComposer();
        });
        recent.appendChild(chip);
      });
      if (!input.value && r.recent && r.recent.length) {
        input.value = r.recent[0].cwd;
      }
      check();
      if (focus) { if (input.value) focusComposer(); else input.focus(); }
    });
    renderSidebar();
  }

  // -- the stats card -------------------------------------------------------

  function fmtCount(n) {
    n = Number(n) || 0;
    if (n >= 1e6) return (n / 1e6).toFixed(n >= 1e7 ? 0 : 1) + "M";
    if (n >= 1e3) return (n / 1e3).toFixed(n >= 1e4 ? 0 : 1) + "k";
    return String(n);
  }

  function hourLabel(h) {
    if (h == null) return "—";
    var ampm = h < 12 ? "AM" : "PM";
    var x = h % 12 || 12;
    return x + " " + ampm;
  }

  function loadStats(range, tab) {
    state.statsRange = range;
    state.statsTab = tab;
    var node = $("stats");
    if (!node) return;
    api("/api/stats?range=" + encodeURIComponent(range)).then(function (data) {
      if (!$("stats") || state.view !== "home") return;
      if (data.error) { clear($("stats")); $("stats").appendChild(el("div", "empty", data.error)); return; }
      state.stats = data;
      renderStats(data, tab);
      renderNeeds(data.needs_you || []);
    });
  }

  function renderStats(data, tab) {
    var node = $("stats");
    clear(node);

    var head = el("div", "stats-head");
    var tabs = el("div", "seg");
    [["overview", "Overview"], ["models", "Models"]].forEach(function (t) {
      var b = el("button", "seg-btn", t[1]);
      b.type = "button";
      b.setAttribute("aria-pressed", String(t[0] === tab));
      b.addEventListener("click", function () { renderStats(state.stats, t[0]); state.statsTab = t[0]; });
      tabs.appendChild(b);
    });
    head.appendChild(tabs);
    head.appendChild(el("span", "spacer"));
    var ranges = el("div", "seg");
    [["all", "All"], ["30d", "30d"], ["7d", "7d"]].forEach(function (r) {
      var b = el("button", "seg-btn", r[1]);
      b.type = "button";
      b.setAttribute("aria-pressed", String(r[0] === state.statsRange));
      b.addEventListener("click", function () { loadStats(r[0], state.statsTab); });
      ranges.appendChild(b);
    });
    head.appendChild(ranges);
    node.appendChild(head);

    if (tab === "models") {
      renderModels(node, data);
    } else {
      var tiles = el("div", "tiles");
      [
        ["Sessions", fmtCount(data.sessions)],
        ["Messages", fmtCount(data.messages)],
        ["Total tokens", fmtCount(data.tokens)],
        ["Active days", fmtCount(data.active_days)],
        ["Current streak", data.current_streak + "d"],
        ["Longest streak", data.longest_streak + "d"],
        ["Peak hour", hourLabel(data.peak_hour)],
        ["Favourite model", modelLabel(data.favourite_model) || "—"],
      ].forEach(function (t) {
        var tile = el("div", "tile");
        tile.appendChild(el("span", "tile-label", t[0]));
        tile.appendChild(el("b", "tile-value", t[1]));
        tiles.appendChild(tile);
      });
      node.appendChild(tiles);
      node.appendChild(heatmap(data.grid || []));
    }

    var books = data.tokens / 160000;
    var note = books >= 1
      ? "You've used ~" + (books >= 10 ? Math.round(books) : books.toFixed(1)) + "× more tokens than Pride and Prejudice."
      : data.indexing ? "Still indexing…" : "";
    if (note) node.appendChild(el("div", "stats-note", note));
  }

  function renderModels(node, data) {
    var models = data.models || [];
    if (!models.length) { node.appendChild(el("div", "empty", "no model usage in this range")); return; }
    var table = el("div", "models");
    var maxTokens = Math.max.apply(null, models.map(function (m) { return m.tokens; }));
    models.forEach(function (m) {
      var row = el("div", "model-row");
      row.appendChild(el("span", "model-name", modelLabel(m.model)));
      var bar = el("span", "model-bar");
      var fill = el("i");
      fill.style.width = Math.max(2, Math.round(100 * m.tokens / (maxTokens || 1))) + "%";
      bar.appendChild(fill);
      row.appendChild(bar);
      row.appendChild(el("span", "model-num", fmtCount(m.tokens) + " tok"));
      row.appendChild(el("span", "model-num", Math.round(m.share * 100) + "%"));
      row.appendChild(el("span", "model-num", m.sessions + " session" + (m.sessions === 1 ? "" : "s")));
      row.title = m.model;
      table.appendChild(row);
    });
    node.appendChild(table);
  }

  // A calendar heatmap: one hue, light to dark, in five steps by quantile
  // of the days that had anything at all. Inline SVG so the CSS tokens
  // colour it in both themes.
  function heatmap(grid) {
    var NS = "http://www.w3.org/2000/svg";
    var cell = 11, gap = 3, step = cell + gap, left = 30, top = 18;
    if (!grid.length) return el("div", "empty", "nothing yet");
    var active = grid.map(function (g) { return g.p; }).filter(function (p) { return p > 0; }).sort(function (a, b) { return a - b; });
    function q(f) { return active.length ? active[Math.min(active.length - 1, Math.floor(f * active.length))] : 1; }
    var q50 = q(0.5), q75 = q(0.75), q90 = q(0.9);
    function level(p) { return !p ? 0 : p >= q90 ? 4 : p >= q75 ? 3 : p >= q50 ? 2 : 1; }

    var first = new Date(grid[0].d + "T00:00:00");
    var firstCol = (first.getDay() + 6) % 7; // Monday-first rows
    var weeks = Math.ceil((grid.length + firstCol) / 7);
    var svg = document.createElementNS(NS, "svg");
    var width = left + weeks * step;
    var height = top + 7 * step;
    svg.setAttribute("viewBox", "0 0 " + width + " " + height);
    svg.setAttribute("class", "heatmap");
    svg.setAttribute("role", "img");
    svg.setAttribute("aria-label", "activity by day");

    ["Mon", "Wed", "Fri"].forEach(function (name, i) {
      var t = document.createElementNS(NS, "text");
      t.setAttribute("x", 0);
      t.setAttribute("y", top + (i * 2) * step + cell - 2);
      t.setAttribute("class", "hm-label");
      t.textContent = name;
      svg.appendChild(t);
    });
    var lastMonth = -1;
    grid.forEach(function (g, i) {
      var idx = i + firstCol;
      var col = Math.floor(idx / 7), row = idx % 7;
      var date = new Date(g.d + "T00:00:00");
      if (row === 0 && date.getMonth() !== lastMonth) {
        lastMonth = date.getMonth();
        var m = document.createElementNS(NS, "text");
        m.setAttribute("x", left + col * step);
        m.setAttribute("y", 11);
        m.setAttribute("class", "hm-label");
        m.textContent = date.toLocaleString(undefined, { month: "short" });
        svg.appendChild(m);
      }
      var r = document.createElementNS(NS, "rect");
      r.setAttribute("x", left + col * step);
      r.setAttribute("y", top + row * step);
      r.setAttribute("width", cell);
      r.setAttribute("height", cell);
      r.setAttribute("rx", 2);
      r.setAttribute("class", "hm hm" + level(g.p));
      var title = document.createElementNS(NS, "title");
      title.textContent = g.d + (g.p ? " · " + g.p + " prompt" + (g.p === 1 ? "" : "s") + " · " + fmtCount(g.t) + " tok" : " · quiet");
      r.appendChild(title);
      svg.appendChild(r);
    });
    var wrap = el("div", "heatmap-wrap");
    wrap.appendChild(svg);
    return wrap;
  }

  function renderNeeds(items) {
    var node = $("needs");
    if (!node) return;
    clear(node);
    if (!items.length) return;
    node.appendChild(el("span", "needs-label", "needs you"));
    items.forEach(function (item) {
      var b = el("button", "needs-item", item.title || item.id.slice(0, 8));
      b.type = "button";
      b.title = (item.state && item.state.activity) || "";
      b.addEventListener("click", function () { selectSession(item.id); });
      node.appendChild(b);
    });
  }

  function startNew(text, attachments) {
    var cwd = ($("new-cwd") ? $("new-cwd").value : state.newCwd).trim();
    if (!cwd) { toast("pick a folder first"); if ($("new-cwd")) $("new-cwd").focus(); return Promise.resolve({ error: null }); }
    var body = { cwd: cwd, text: text, attachments: attachments };
    if (state.compose.mode) body.mode = state.compose.mode;
    if (state.compose.model) body.model = state.compose.model;
    return api("/api/new", body).then(function (r) {
      if (r.error) return r;
      state.view = "session";
      document.body.dataset.view = "session";
      selectSession(r.id);
      return r;
    });
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
    document.querySelector(".brand b").addEventListener("click", function () { showHome(); });
    $("new-toggle").addEventListener("click", function () {
      showNew();
      $("sidebar").dataset.open = "false";
    });
    $("board-toggle").addEventListener("click", function () {
      toggleBoard();
      $("sidebar").dataset.open = "false";
    });

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
    bindComposer();
    $("compose-input").addEventListener("keydown", function (ev) {
      var pop = state.popover;
      if (pop) {
        if (ev.key === "ArrowDown") { ev.preventDefault(); pop.move(1); return; }
        if (ev.key === "ArrowUp") { ev.preventDefault(); pop.move(-1); return; }
        if (ev.key === "Enter" || ev.key === "Tab") { if (!ev.isComposing) { ev.preventDefault(); pop.pick(); } return; }
        if (ev.key === "Escape") { ev.preventDefault(); closePopover(); return; }
      }
      if (ev.key === "Tab" && ev.shiftKey) { ev.preventDefault(); cycleMode(); return; }
      if (ev.key === "Escape") {
        if (!$("stop-btn").hidden) stopTurn();
        else ev.target.blur();
        return;
      }
      if (Compose.shouldSend(ev)) { ev.preventDefault(); sendReply(); }
    });
    $("compose-input").addEventListener("input", function (ev) {
      autogrow(ev.target);
      saveDraft(ev.target.value);
      suggest();
    });
    $("compose-input").addEventListener("keyup", function (ev) {
      if (/^(ArrowLeft|ArrowRight|Home|End)$/.test(ev.key)) suggest();
    });
    $("compose-input").addEventListener("click", suggest);

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

  function autogrow(input) {
    input.style.height = "auto";
    input.style.height = Math.min(144, input.scrollHeight) + "px";
  }

  // A half-written message survives switching sessions and reloading the
  // page. Per session, because a draft belongs to the conversation it was
  // written in.
  function saveDraft(text) {
    try {
      var key = Compose.draftKey(state.sessionId);
      if (text) localStorage.setItem(key, text);
      else localStorage.removeItem(key);
    } catch (e) {}
  }

  function restoreDraft() {
    var input = $("compose-input");
    var text = "";
    try { text = localStorage.getItem(Compose.draftKey(state.sessionId)) || ""; } catch (e) {}
    input.value = text;
    autogrow(input);
  }

  function setSending(on) {
    $("compose-input").disabled = on;
    $("compose-send").disabled = on;
    $("compose").dataset.sending = on ? "true" : "false";
  }

  function sendReply() {
    var input = $("compose-input");
    var text = input.value.trim();
    var ready = state.attachments.filter(function (a) { return a.id; });
    if ((!text && !ready.length) || $("compose").dataset.sending === "true") return;
    if (state.attachments.some(function (a) { return a.pending; })) return toast("still uploading…");
    var blocked = commandBlocked(text);
    if (blocked) return toast(blocked);
    var body = { session_id: state.sessionId, text: text, attachments: ready.map(function (a) { return a.id; }) };
    if (state.head.reply_via === "spawn") {
      if (state.compose.mode) body.mode = state.compose.mode;
      if (state.compose.model) body.model = state.compose.model;
    }
    // The text stays in the box until the daemon has it, so a refused or
    // failed send costs nothing but a toast.
    setSending(true);
    var request = state.view === "home"
      ? startNew(text, body.attachments)
      : api("/api/message", body);
    request.then(function (r) {
      setSending(false);
      if (r.error) { input.focus(); return toast(r.error); }
      input.value = "";
      autogrow(input);
      saveDraft("");
      clearAttachments();
      input.focus();
      if (r.via === "inbox") onDelivery({ status: "delivered", via: "inbox", held: r.held });
      if (r.via === "driver" && r.queued) toast("queued — sent when this turn ends");
    });
  }

  function focusComposer() {
    var dock = $("dock");
    if (dock.hidden) return false;
    var input = $("compose-input");
    input.focus();
    input.setSelectionRange(input.value.length, input.value.length);
    return true;
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
    if ((ev.metaKey || ev.ctrlKey) && ev.key === "k") {
      if (focusComposer()) ev.preventDefault();
      return;
    }
    if (typing) return;
    if (ev.metaKey || ev.ctrlKey || ev.altKey) return;

    if (ev.key === "c") {
      if (focusComposer()) ev.preventDefault();
    } else if (ev.key === "j" || ev.key === "k") {
      ev.preventDefault();
      stepRound(ev.key === "j" ? 1 : -1);
    } else if (ev.key === "t") {
      toggleTheme();
    } else if (ev.key === "b") {
      toggleBoard();
    } else if (ev.key === "n") {
      showNew();
    } else if (ev.key === "h") {
      showHome();
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
    if (location.hash === "#/board") return showBoard();
    if (location.hash === "#/new") return state.view === "home" ? focusComposer() : showNew();
    if (location.hash === "#/" || location.hash === "") return state.view === "home" ? undefined : showHome();
    var match = /^#\/s\/([\w-]+)$/.exec(location.hash || "");
    if (match && (match[1] !== state.sessionId || state.view === "board")) selectSession(match[1]);
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
    ["arm-toggle", "dock", "board-toggle", "new-toggle"].forEach(function (id) { $(id).hidden = true; });
    $("conn-dot").title = "exported file — not live";
    requestAnimationFrame(function () { scheduleRail(true); });
  }

  function boot() {
    if (window.SCRIBE_SNAPSHOT) return bootStatic(window.SCRIBE_SNAPSHOT);
    installRenderer();
    state.openProjects = loadOpenProjects();
    bind();
    api("/api/config").then(function (cfg) { if (cfg && !cfg.error) state.cfg = cfg; });
    api("/api/sessions").then(function (data) {
      state.sessions = (data && data.sessions) || [];
      renderSidebar();
      if (location.hash === "#/board") return showBoard();
      if (location.hash === "#/new") return showNew();
      var match = /^#\/s\/([\w-]+)$/.exec(location.hash || "");
      if (!match) return showHome();
      var wanted = match[1];
      state.sessionId = wanted;
      location.hash = "#/s/" + wanted;
      renderSidebar();
      load(wanted);
    });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();

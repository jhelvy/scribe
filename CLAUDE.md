# CLAUDE.md

## What this is

**scribe** — a CLI that mirrors every Claude Code session into markdown and
serves a live web view of it. Python 3 standard library only; the viewer is
vanilla JS with no build step. Nothing here enters a session's context window,
and nothing ever writes to a transcript.

## The one idea

**Copy first, render second.** Claude Code deletes transcripts after
`cleanupPeriodDays` (default 30) with no warning. Every other tool in this space
reads `~/.claude/projects` and stops there, so they all inherit that expiry.
`archive.py` runs before anything else in `poll_session` for exactly this
reason: if rendering throws, the bytes are already safe.

Compaction is *not* a threat — it appends a boundary and keeps writing to the
same file, leaving earlier rows intact. Verified against a real session that
dropped 460k tokens from context and kept all 440 pre-boundary rows.

**The JSONL transcript is the source of truth.** Claude Code writes a complete
record of every session to `~/.claude/projects/<mangled-cwd>/<session>.jsonl`,
and every hook payload carries `transcript_path`. scribe parses that; hooks
only say *when* to look.

**A session is four files, not one.** Missing any of them makes "fully
reproducible" untrue::

    <project>/<session>.jsonl
    <project>/<session>/subagents/agent-*.jsonl
    <project>/<session>/subagents/agent-*.meta.json
    <project>/<session>/tool-results/<id>.txt

**The archive is a peer source, not a backup.** `index_sessions` unions live and
archived transcripts, so a session Claude Code has deleted stays listed,
searchable, renderable and exportable. It is flagged `archived` and shown as
`kept`.

Everything else follows:

- The model is a **pure function** of the transcript, so the markdown is
  **regenerable**. There is no append-only bookkeeping, no reserved call
  numbers, no supersede-by-appending-a-duplicate, no `flock` between writers.
  Writes are `tmp` + `os.replace`.
- Capture **does not depend on hooks**. If the daemon is down, nothing is lost.
- `scribe build --all` **backfills every session ever run**.

The predecessor at `jhelvy/scribe` reconstructed conversations from hook stdin
instead, and its README conceded the result was "not a full audit trail". Do not
reintroduce that. If you need something the model lacks, get it from the
transcript.

## Layout

```
bin/scribe            CLI entry
bin/scribe-hook       hook entry — tiny on purpose, runs on every tool call
scribe/
  sockpath.py           where the control socket lives (leaf module, no imports)
  paths.py  config.py   ~/.scribe layout, settings
  transcript.py         JSONL tail-by-offset + the cheap session index
  model.py              Session / Round / Text / Thinking / ToolCall / Notice
  build.py              rows -> model. THE builder.
  render_md.py          model -> CommonMark
  render_json.py        model -> viewer payload
  redact.py             secret scrubbing, applied to both renderers
  store.py              where a log lives and what goes in it
  daemon.py             HTTP + SSE + watcher + control socket
  control.py            approval holds and the reply queue (no transport)
  peer.py               messages into a session: the inbox socket
  driver.py             a headless Claude Code child the page drives (stream-json)
  catalog.py            what `/` completes to: skills and commands, disk + Claude
  search.py             FTS5 index over every session, incremental
  explain.py            Haiku explainer, content-addressed cache
  install.py            writing hooks into settings.json
  export.py  replay.py
  viewer/              index.html styles.css app.js rail.js theme-boot.js
                       compose.js (DOM-free composer rules) marked.min.js (vendored, MIT)
tests/   test_*.py  test_rail.mjs  test_compose.mjs  helpers.py  fake_claude.py
```

## Invariants

- **Standard library only**, in every module. The one vendored file is
  `marked.min.js` (MIT).
- **The builder is total.** Unknown row types are skipped, never raised on. New
  ones appear in Claude Code releases; a logger that crashes on one is worse
  than useless.
- **The hook never blocks and never fails.** It reads stdin, talks to the daemon
  over a Unix socket, prints the reply, exits 0. All logic lives in
  `daemon.dispatch`, so hook behaviour changes without reinstalling. The single
  exception is `PermissionRequest`, which is *designed* to be held.
- **One builder, two renderers.** If markdown and the viewer disagree, the bug
  is that something bypassed `build.py`.
- **Explanations are the only thing that spends tokens.** Cheap calls are
  answered from their own arguments; answers are cached by content hash; the
  model call happens on a thread, never in the hook's path.

## Things that bite

**Never feed the archive its own file as a source.** Once a session outlives its
original it re-enters the index pointing at the archived copy. Without the
guards in `mirror_file` / `archive_ref`, the rotate-then-copy path renames the
destination aside and then fails to read the source it just moved — losing the
canonical file and rotating again on every sweep. Two independent checks exist
because the failure is silent and permanent.

**Subagent rows are all `isSidechain: true`.** They are a sidechain *of the
parent*, but the whole conversation at their own level, so a nested `build()`
must be called with `nested=True` or it filters out every row and produces zero
rounds. Both the file-based and inline paths hit this.

**Subagents link back explicitly.** `agent-*.meta.json` carries `toolUseId`,
naming the exact `Task` call that spawned it. Use that, not contiguous-run
guessing — the latter cannot tell two parallel subagents apart.

**`AF_UNIX` paths are capped at ~104 bytes** regardless of filesystem limits.
`sockpath.py` exists because of this and because the daemon and the hook must
agree on the answer — a disagreement silently disables every hook.

**The explainer child is a real Claude Code session** and gets a transcript of
its own. It runs with `cwd` inside `~/.scribe/run/explain` precisely so
`transcript.index_sessions` can recognise and drop those. It also sets
`SCRIBE_DISABLE=1` and `--setting-sources ""` so it cannot fire our hooks.
Do **not** switch it to `--bare`: that reads auth only from `ANTHROPIC_API_KEY`,
never the keychain, which breaks subscription users.

**`ai-title` is not always a title.** When a session runs under a named agent,
Claude Code overwrites that field with the *agent's name*. `transcript.pick_title`
takes the newest title that is neither a known `agentName` nor slug-shaped.

**A rewritten transcript must replace, not append.** `--resume` and compaction
rewrite in place. `TranscriptTail.restarted` signals it and `LiveSession.poll`
drops its accumulated rows; without that the session doubles.

**Cache-read tokens are not a total.** Every assistant message re-reads the whole
cached prefix. `Usage.total` deliberately excludes `cache_read`.

**`system` rows with `subtype: local_command`** carry the same XML-ish wrappers
as user rows (`<command-name>`, `<local-command-stdout>`). Route them through
`strip_wrappers` or raw markup lands in the log.

**Raw HTML in a transcript is content, not markup.** Someone writing
"maybe the `<aside>` option" means those characters; marked treats it as an HTML
block and swallows the paragraph. The viewer overrides marked's `html` renderer
to escape.

**`.chip.note` and the rail's note element must not share a class.** They did
once, and the chips inherited `position: absolute`. The rail element is
`.rail-note`.

## Messages from the page

`peer.py` puts a message typed in the viewer in front of a session. Claude
Code 2.1 registers every session in `~/.claude/sessions/<pid>.json` with a
`messagingSocketPath`, and publishes the token a peer must present in
`<pid>.<hash>.key` beside it. The wire is two JSON lines on that socket: an
`auth` frame, then a `user` frame whose content is wrapped in Claude Code's own
`<cross-session-message from-name="scribe">` envelope. The envelope is what
makes the transcript row carry `origin.name` and a clean `origin.body`;
`build.peer_message` keys on those, so a page message renders as `source: web`
and one from another Claude session as `peer`, with no prose parsing unless
`origin.body` is missing. Those rows are `isMeta: true`; the builder and
`turn_state` must treat a peer row as a prompt or the log shows a reply to
nothing.

`Hub.reply_via` decides the channel per session and the viewer only shows it:
`driver` when a headless child of ours is behind it, `inbox` when the registry
has the session, `queue` (Stop hook, opt-in) for a live process without one,
`spawn` when there is no process (the first message starts a driver). The
driver is checked before the inbox because its child registers an inbox of its
own. `is_live` trusts the driver and the registry before any hook, so a session
with either is never "done". `head.caps` says what the composer may offer on
that channel (attachments as blocks or paths, whether mode and model can be
set, whether there is something to interrupt); the page never guesses.

**Do not assert `from-mode`.** Claude Code holds a message that asserts no
permission mode when the recipient runs with permissions bypassed, and asks in
the terminal. That check is what stops a less trusted process steering a more
trusted session. scribe is such a process as far as Claude Code can tell; the
user's remedy is `crossSessionInbound: accept` in their own settings.

**`claude --bg --resume` forks.** It starts a copy under a new id. Only
`claude -p --resume <id>` appends to the same transcript, so that is what a
finished session gets. `peer.child_env` strips every `CLAUDE*` variable except
`CLAUDE_CONFIG_DIR`: the daemon is usually a grandchild of a session and would
otherwise hand the child its parent's id, inbox and token. When the child exits
the session is added to `ended`, or its fresh mtime would count as a process
for ten minutes and the composer would hide.

## The driver

`driver.py` keeps one `claude -p --input-format stream-json --output-format
stream-json --permission-prompt-tool stdio` child per driven session, alive
between turns, closed after `driver.idle_min`. Its docstring records what
2.1.272 actually does on that wire; the short version:

- **`--permission-prompt-tool stdio` or no prompts.** With the default flags a
  permission prompt is answered by a local deny and never reaches the host.
  With it, `can_use_tool` arrives as a `control_request`, and the daemon
  answers it from the same `control.PendingCall` hold the hook path uses. The
  `PermissionRequest` hook returns `{}` for a driven session so there is one
  card per call, and silence is a deny because there is no terminal to fall
  back to.
- **`system/init` comes with every turn**, not at startup; `initialize` is
  what returns the command catalogue with descriptions.
- **A mode switch writes no `permission-mode` row**; the next user row carries
  `permissionMode`. `turn_state` reads both.
- **Never `--bare`** (keychain auth). Hooks stay on: the child's own
  `SessionStart`/`SessionEnd` feed presence like any session.
- **Two writers.** If the registry shows a terminal process for a driven
  session, `reap_drivers` stops the child once idle and the channel flips to
  `inbox`.

**A session started from the page has no file for a second or two.**
`Hub.drafts` holds it meanwhile: `index_payload` prepends a draft card and
`snapshot` answers with a draft head, both flagged `draft`. The page shows
"starting" and reloads when the `sessions` event carries the id without the
flag; the draft is dropped the moment the index sees the file. Uploads made
on the new-session view live under `uploads/new/` and are moved under the id
by `rehome_uploads` before the first message goes out.

**The slash catalogue has two halves.** Disk (`catalog.scan`: user, project
and plugin skills/commands, front matter read without YAML) knows scope; only
a driver's `initialize` answer knows the bundled skills and built-ins, with
descriptions, so `catalog.remember` keeps the last one under
`~/.scribe/cache/commands.json` for sessions with no driver. `available`
decides per channel: a built-in on an inbox session is refused on the page
with the reason, because the TUI reads a cross-session message as prose.

**Uploads are files first.** `Hub.save_upload` writes to
`~/.scribe/uploads/<session>/<id>-<name>` and the id is resolved by scanning
that directory, so a daemon restart forgets nothing. The browser's
`Content-Type` is recorded but the `image` flag comes from the bytes
(`sniff_image`): only a real PNG/JPEG/GIF/WebP becomes a content block. The
page shows thumbnails through `blob:` URLs, which is why `img-src` allows
`blob:`.

**A fresh mtime is not a reason to hide the composer.** `presence_kind` ranks
what says a process exists (driver, inbox, hook, mtime). The board keeps
treating a recent file as live, but `reply_via` offers `spawn` when the only
evidence is the mtime: an interactive Claude Code always has an inbox, so a
recent file with none is a session that just ended.

`tests/fake_claude.py` speaks the same wire and writes real transcript rows,
so `test_driver.py` and `TestDriverDelivery` run without Claude. Point
`SCRIBE_CLAUDE` at any binary to drive something else.

## The rail

`scribe/viewer/rail.js` minimises total squared displacement from each note's anchor,
subject to no overlap. Substituting `x[i] = top[i] - cumulativeOffset[i]` turns
the ordering constraint into "x is non-decreasing", making it isotonic
regression, solved exactly by pool-adjacent-violators in O(n).

A naive forward pass (`top = max(anchor, prevBottom + gap)`) is a ratchet: notes
only move down, so one tall note pushes every note below it permanently off its
anchor. That was the previous viewer's bug.

Focus is a large weight on one note, then a pass that pins it exactly and pushes
the two sides apart — which is the "snap together" interaction, out of the same
solver. The module is DOM-free so `tests/test_rail.mjs` can drive it directly,
including a brute-force optimality check.

## The board

`#/board` shows live sessions as cards in columns: *needs you*, *planning*,
*working*, *your turn*, and a collapsed *done* strip for everything with no
process behind it. The column is `build.turn_state(rows)`, a function of the
transcript's tail like everything else: `stop_reason: end_turn` is your turn,
a trailing `tool_use` is working, a trailing `AskUserQuestion` or
`ExitPlanMode` needs you, the latest `permission-mode` row says whether
working is planning. `peek` computes it for the index from the same tail slice
it reads the title from; `LiveSession.rebuild` computes it from the whole file
so the elapsed clock can find the prompt behind a megabyte of tool output.

The daemon adds only what a file cannot know, in `Hub.card_for`: whether a
process is alive (`presence`, fed by every hook and cleared by `SessionEnd`,
with a ten-minute mtime grace for machines without hooks), whether an approval
is being held here, and the last `Notification` type (`permission_prompt`
means a dialog is up in the terminal; `idle_prompt` means Claude has been
waiting). Those refine the phase; they never replace it. Cards travel on the
`sessions` and `card` SSE events, which every stream receives, so the board
subscribes with no session id.

The board is redrawn whole on every change. Unlike the conversation column a
card holds no state worth preserving, so the reconcile-in-place rule below
does not apply to it.

`peek` results are cached on (size, mtime). The index is rescanned every four
seconds, and without the cache every rescan re-read the tail of every
transcript on the machine.

## Viewer state

**A restarted daemon reloads the page.** The SSE `hello` frame carries
`asset_stamp()`, a hash of the viewer files computed once per process. The
page keeps the first stamp it sees and reloads when a reconnect brings a
different one, so a restart on new code never leaves yesterday's script
reading today's payloads.


Every round and item carries a server-assigned `key` (`_key_round` in
`daemon.py`; tool calls key on their id, everything else on position — safe
because transcripts are append-only). An update replaces only the nodes whose
payload changed, so open `<details>`, scroll position, and selection survive
because they are never touched. **Never re-render `#column` wholesale.**

## Security

The daemon binds loopback, rejects foreign `Host` headers (DNS rebinding), and
serves `default-src 'none'; script-src 'self'` — which is why the theme
bootstrap is a file rather than an inline script. Transcript content is
sanitised in the DOM as well. All of this matters because the page shares an
origin with an API that can approve tool calls.

## Testing

```
python3 -m unittest discover -s tests -t tests
node tests/test_rail.mjs
node tests/test_compose.mjs
```

`tests/helpers.py::Isolated` redirects `SCRIBE_HOME` and `CLAUDE_CONFIG_DIR`
to temp dirs; inherit from it for anything touching disk. The hook tests run the
real `bin/scribe-hook` executable against a real socket, because the contract
worth testing is what Claude Code actually sees on stdout and how long it waits.

Both suites must pass before committing.

## The home page

`#/` is the board. There is no landing page of tiles and charts; the
question the page answers on arrival is "what needs me", and the board's
columns answer it. `#/new` is the one other page: a folder picker over the
composer, from which a driver starts a fresh session.

`search.py` still fills `session_stats` in the same incremental pass that
builds the FTS index, and `overview()` sums it for a range; nothing in the
viewer reads it now, but it is one blob per session and costs the index
nothing to keep.

## Search

`search.py` keeps an FTS5 index at `~/.scribe/index.db`, one document per
*item* — a prompt, a reply paragraph, a thought, a tool call. Coarser and a hit
in a long session tells you nothing about where to look; finer and snippets lose
context. Sync is incremental on (size, mtime) and runs on a worker thread, never
on the request path.

`fts_query()` quotes every term. Users type `rm -rf`, `a:b`, a lone `"` — all
FTS5 syntax that would otherwise raise mid-keystroke. A trailing `*` survives as
a prefix match.

Snippets are delimited with `\x02`/`\x03` rather than markup, so a hit can
never carry HTML out of a transcript and into the page.

A "hit" is a matching document, not a term occurrence. Two mentions in one
prompt are one hit.

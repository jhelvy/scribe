# scribe

**A permanent archive of every Claude Code conversation, and a live web view to
read it in.**

```
scribe install     # register the hooks
scribe serve       # open the viewer
```

Python 3 standard library only. Nothing here writes to a transcript, and
recording adds nothing to the session's context window.

---

## Why this exists

Claude Code deletes your session transcripts. `cleanupPeriodDays` defaults to
**30**, the sweep runs quietly in the background, and there is no warning.

On the machine this was built on, the cleanup had already run that morning:
136 MB of transcripts remained, the oldest dated exactly 28 days back, and
everything before it was gone.

There are already several good Claude Code session viewers — [claude-code-viewer](https://github.com/d-kimuson/claude-code-viewer),
[claude-code-trace](https://github.com/delexw/claude-code-trace),
[claude-code-log](https://github.com/daaain/claude-code-log) among them. Every
one of them reads `~/.claude/projects` and stops there, so every one of them
inherits that expiry. scribe's reason to exist is that it copies first.

**Compaction is not the threat.** `/compact` appends a boundary marker and keeps
writing to the same file; the earlier rows stay. In a real session that dropped
460,573 tokens from context, all 440 pre-compaction rows were still on disk.
What loses conversations is the 30-day sweep.

---

## What it does

**Archives every session, permanently.** A byte-for-byte copy into
`~/.scribe/archive/`, incremental and append-only. A session is four things on
disk, and all four are captured:

```
<project>/<session>.jsonl                        the conversation
<project>/<session>/subagents/agent-*.jsonl      subagent conversations
<project>/<session>/subagents/agent-*.meta.json  which Task spawned each
<project>/<session>/tool-results/<id>.txt        outputs too large to inline
```

When Claude Code deletes the original, the session keeps working — it stays
listed, searchable, renderable and exportable, marked `kept` in the sidebar.

**Searches every conversation.** Full-text across the whole corpus — live and
archived — grouped by conversation, ranked, with highlighted context. Indexing
107 sessions takes under a second and the index is 12 MB.

```
$ scribe search "reveal.js fragment"
15 matches in 4 conversations

ba25902e  Improve scroll responsiveness and restore reveal state
        quarto-lexis · 2026-07-28 16:33 · 8 hits
        r1 …the rest of that controller and Quarto's hash/fragment defaults.
```

In the browser, the sidebar box searches everything; clicking a result opens
that conversation centred on the round that matched.

**Renders readable markdown.** One file per session under `~/.scribe/logs/`,
regenerated from the archive at any time. Greppable, diffable, and readable in
ten years when this program no longer exists.

**Serves a live web view.** One daemon for every project, one URL to bookmark.
Updates stream over SSE and are applied in place, so an open tool call stays
open and your scroll position holds while the log grows underneath you.

**Shows every live session on a board.** Press `b` or click *board*: sessions
sit in columns by what they are waiting on — *needs you* (an approval, a
question, a plan), *planning*, *working*, *your turn* (Claude replied) — with
the running command, the reply's first line, or the approval countdown on the
card, and approve/deny right there. Everything without a process behind it is
the collapsed *done* column. The column is read off the transcript, so it is
right even when the daemon was started after the session.

**Explains opaque tool calls in plain English.** A `python3 - <<'EOF'` heredoc
becomes a sentence in the margin, next to the call — ideally while you are still
deciding whether to approve it. Answers cache by content, so a command explained
once is annotated everywhere it ever appears.

**Can talk back** (both opt-in, both off by default). Approve or deny a tool call
from the browser, edit the command first, or type a reply delivered when the
turn ends.

---

## Install

Requires `python3` (3.9+) and Claude Code. No dependencies. macOS and Linux;
on Windows see [Things worth knowing](#things-worth-knowing).

```bash
uv tool install git+https://github.com/jhelvy/scribe
```

or `pipx install git+https://github.com/jhelvy/scribe`, or
`pip install git+https://github.com/jhelvy/scribe` into a virtualenv. Any of
them puts a `scribe` command on your PATH. Then, in this order:

```bash
scribe archive     # 1. back up everything you still have, right now
scribe install     # 2. register hooks in ~/.claude/settings.json
scribe serve       # 3. start the daemon and open the viewer
```

**Do step 1 first.** It is the only step with a deadline: it copies transcripts
that Claude Code may delete out from under you, and nothing else here can bring
those back. It is also safe to run at any time, on a machine where scribe is
not yet installed, and repeatedly.

Step 2 backs `settings.json` up first, writes *through* a symlink rather than
replacing it (dotfiles setups keep working), and only touches entries it
recognises as its own. `scribe install --dry-run` shows the diff without
writing; `scribe install --uninstall` restores exactly what was there before,
leaving the archive intact.

After step 2 the daemon starts itself whenever a session begins, so step 3 is
only needed the first time — after that, `scribe open`.

<details>
<summary><b>Running from a clone instead</b></summary>

No install step; `bin/scribe` runs straight out of the working tree.

```bash
git clone https://github.com/jhelvy/scribe
cd scribe
./bin/scribe archive
./bin/scribe install     # registers this checkout's path with Claude Code
./bin/scribe serve
```

Everywhere this README says `scribe`, use `./bin/scribe`. Note that `install`
writes the checkout's absolute path into `settings.json`, so moving or deleting
the clone breaks the hooks until you re-run it.

</details>

### Where it puts things

Everything lives in `~/.scribe`, mode `0700`, outside every repository:

```
~/.scribe/archive/     the permanent byte-for-byte copies — the irreplaceable part
~/.scribe/logs/        rendered markdown, one file per session (regenerable)
~/.scribe/index.db     the search index (regenerable)
~/.scribe/config.json  settings
```

Only `archive/` holds anything that cannot be rebuilt. Back up that directory
and you have kept everything; `scribe build --all` regenerates the rest.
`SCRIBE_HOME` moves the whole tree elsewhere.

---

## Commands

```
scribe search <query>                # full-text across every conversation
scribe archive [session]             # copy transcripts into the archive
scribe serve [--port N] [--background] [--no-browser]
scribe open [session]                # browser, at this project's latest session
scribe list [-n N] [--all] [--json]  # sessions, newest first
scribe build <session|--all>         # (re)generate markdown
scribe path|show <session>           # the markdown path, or its contents
scribe export <session> -o f.html    # one self-contained HTML file
scribe status | stop
scribe config list|get|set <key> <value>
scribe install [--uninstall] [--project] [--dry-run]
scribe replay <session> [--speed N]
```

A session argument can be a full id, a unique prefix, or a path. Leave it out
and scribe uses the newest session for the current directory.

---

## The archive

Incremental: an unchanged session costs one `stat`. A growing one copies only
the new bytes. Backing up 107 sessions and 134 MB took 0.2s cold, and 0.0s warm.

The one case that could destroy data is a source file being rewritten or
truncated underneath us. Rather than overwrite, scribe rotates the existing
archive to `<session>.gen1.jsonl` and starts fresh — so both incarnations
survive. Sessions that outlive their originals are never fed back into the
archive as sources.

`scribe status` reports how much is held and how many sessions exist only
because they were archived.

---

## The viewer

- **Sidebar collapsed to projects.** Click to expand; the active project opens
  itself, and typing a filter opens whatever matches.
- **Click anything to centre it.** A message that fits is centred; one that
  doesn't is top-aligned, because centring a long message would start it above
  the fold.
- **Margin notes behave like Google Docs comments.** A cluster distributes
  *around* its anchors instead of ratcheting downward, and clicking a note (or
  its tool call) snaps them into exact alignment while neighbours move aside. A
  dashed tether shows the pairing. The layout is an exact solve — see
  `scribe/viewer/rail.js`.
- **Tool calls** show command, stdout, stderr and status; edits render as
  red/green diffs; subagent conversations nest inside the call that spawned
  them, matched by `toolUseId` rather than guessed.
- **Explain any call, any time** — hover a tool card, click *explain*. Works on
  sessions recorded months ago.
- Search with match counts, `j`/`k` to step rounds, `/` to find, `f`/`.` for
  follow, `t` for theme, dark and light throughout.

---

## Continuing a conversation from the page

The compose box at the bottom of a session sends a message into that session.
It is on by default because every message is something you typed and pressed
send on; nothing is injected on your behalf. How it gets there depends on what
is behind the session, and the placeholder text says which:

**A running session.** Claude Code 2.1 gives every session an inbox: a Unix
socket registered in `~/.claude/sessions/`, the same channel one Claude
session uses to message another. scribe writes your message there and it lands
exactly as a prompt typed in the terminal would: it starts a turn if Claude is
waiting for you, and waits its turn if Claude is busy. No hooks needed. The
message is recorded in the transcript as an ordinary row, so the log shows it
as *you · web* with Claude's reply underneath.

One thing Claude Code enforces: a session running with permissions bypassed
(`--dangerously-skip-permissions`, or auto mode) holds a message from any other
process and asks in the terminal before delivering it. scribe does not claim
otherwise on your behalf. To let page messages through without the prompt, set
`"crossSessionInbound": "accept"` in your Claude Code settings.

**A finished session.** With no process behind it, sending starts a headless
Claude Code child of scribe's own, `claude -p --resume <id>` speaking Claude
Code's stream-json protocol, in the session's own directory. It appends to the
same transcript under the same id, so the page updates as the turn runs and
`claude --resume` in a terminal later picks up from there. The child stays
between turns (follow-ups go straight in, a message sent mid-turn is queued)
and closes after `driver.idle_min` of silence. Because scribe is the host of
that process, the page gets what a terminal has: pictures in the message, the
permission mode and model to pick, a stop button, and the session's skills and
commands, and a tool that needs permission is approved in the margin rail
rather than refused. The board's *done* strip offers *continue* for these.

If you open the same session in a terminal, scribe retires its child after the
current turn so two processes never write one transcript.

**Attachments.** The `+` button, a paste, or a drop onto the compose box
attaches files; they are kept under `~/.scribe/uploads/<session>/` and never
enter a repository. On a page-driven session an image goes to Claude as an
image (it sees the picture); anything else, and everything on a terminal
session, is named by path so Claude reads it with its own tools. `uploads.max_mb`
caps the size.

**Slash commands and `@` files.** Typing `/` at the start of the box lists
what this session can run: your skills (`~/.claude/skills`), your commands,
the project's own under `.claude/`, every enabled plugin's, and, once a
page-driven session has started, the bundled skills and built-in commands
Claude Code itself reported (kept for later, so they show on other sessions
too). Entries a channel cannot carry are greyed with the reason: a built-in
like `/compact` only works at Claude Code's own prompt, so it is refused on a
terminal session rather than sent as prose. `@` lists files under the
session's folder (from the driver when there is one, else `git ls-files`).
↑↓ move, Tab or Enter completes, Esc closes.

**Mode and model.** On a page-driven session the two chips under the compose
box switch the permission mode (`Shift+Tab` cycles, like the terminal) and the
model; on a terminal session they show what the transcript says and point you
at the terminal. `Esc` while Claude is working stops the turn.

**A running session without an inbox** (an older Claude Code, or messaging
turned off there) falls back to the Stop-hook queue below when that is enabled.

```bash
scribe config set messaging.enabled false   # hide the compose box entirely
scribe config set driver.enabled false      # never start a process
scribe config set driver.idle_min 10        # close an idle child sooner
scribe config set driver.default_mode plan  # mode a started session begins in
scribe config set driver.allow_bypass true  # offer bypassPermissions on the page
```

## Two-way control through hooks

Off by default. Turn on deliberately.

```bash
scribe config set remote_approval.enabled true
scribe config set reply_queue.enabled true
```

**Approvals.** Press *approvals* to arm a session. The next permission request
appears in the browser with its explanation, and the `PermissionRequest` hook
waits for you. It releases immediately if you close the tab or disarm, and after
`remote_approval.wait_s` (default 120) regardless.

The honest cost: while the hook is held your terminal shows nothing, because
Claude Code's dialog does not appear until the hook returns. That is why arming
is explicit, per-session, and shows a countdown.

Note this goes through Claude Code's own hook system, not the Agent SDK — so it
is not subject to the subscription-account restriction that affects SDK-based
tools.

**The Stop-hook queue.** The older reply path, kept for sessions the inbox
cannot reach. Text is queued and delivered through the `Stop` hook when the
turn ends. Guards: `stop_hook_active` is honoured so a blocked stop never
triggers another, and `reply_queue.max_chain` (default 5) caps consecutive
injections. A prompt typed in the terminal resets the count. A message
delivered this way reaches Claude as the reason for a blocked stop rather than
as a user row, so the daemon splices it into the view itself.

These are the features that put text *into* a conversation. A message enters
the context window exactly as a typed one would — which is the point of it, but
it is why the blanket claim at the top of this file is about recording.

---

## Configuration

`scribe config list` shows everything; `~/.scribe/config.json` holds it.

| Key | Default | |
|---|---|---|
| `port` | `4517` | the daemon's port |
| `markdown.tools` | `full` | `full`, `summary`, or `none` |
| `markdown.max_output_chars` | `4000` | per tool call, markdown only |
| `explain.enabled` | `true` | plain-English margin notes |
| `explain.model` | `claude-haiku-4-5` | |
| `messaging.enabled` | `true` | the compose box: message a session from the page |
| `driver.enabled` | `true` | start a headless Claude Code child for a session with no process behind it |
| `driver.idle_min` | `30` | close that child after this many idle minutes |
| `driver.default_mode` | `""` | permission mode for a started session (`""` = Claude Code's `permissions.defaultMode`) |
| `driver.allow_bypass` | `false` | offer `bypassPermissions` on the page |
| `uploads.max_mb` | `20` | largest file the compose box accepts |
| `remote_approval.enabled` | `false` | approve from the browser |
| `reply_queue.enabled` | `false` | Stop-hook replies, for sessions without an inbox |
| `redact.enabled` | `true` | scrub secrets |

---

## Privacy

The archive holds complete transcripts, which means whatever the agent read.
Everything lives in `~/.scribe` at mode `0700`, outside every repository — an
in-project log is one `git add -A` away from being published.

`redact.enabled` scrubs the shapes that leak most often: provider key prefixes
(`sk-ant-`, `ghp_`, AWS, Slack, Google), `Authorization` headers, private key
blocks, and `NAME=value` assignments for password/secret/token-ish names. It
applies to the rendered markdown and the viewer, **not** to the raw archive —
the archive is deliberately verbatim, because a redacted archive is not a
reproducible one. Add patterns with `redact.extra_patterns`. It is a safety net,
not a guarantee.

Explanations are the only feature that sends anything anywhere: the call's
arguments go to Anthropic. Cheap calls never leave your machine, answers are
cached so nothing is sent twice, and `explain.enabled false` turns it off.

---

## Things worth knowing

- **The markdown is regenerated, not appended to.** Hand-editing a log will be
  overwritten; rename the file to keep annotations.
- **A `Read` records that it happened, not the file's contents.** One session had
  782 of them. The archive keeps the raw record regardless.
- **Token counts exclude cache reads.** Every message re-reads the cached prefix,
  so summing that field reports tens of millions for a session that produced a
  few hundred thousand.
- **`scribe serve` binds loopback only**, checks `Host` against DNS rebinding, and
  serves a CSP forbidding inline script and every external origin. Transcript
  content is escaped, never rendered as HTML.
- **`--background` is POSIX-only** (it forks) and hooks need `AF_UNIX`. On Windows
  run the daemon in the foreground; capture still works.

---

## Development

```bash
python3 -m unittest discover -s tests -t tests   # 167 tests
node tests/test_rail.mjs                          # the rail's layout solver
node tests/test_compose.mjs                       # the composer's key and token rules
```

`SCRIBE_HOME` and `CLAUDE_CONFIG_DIR` redirect everything, which is how the tests
stay off your real data.

MIT.

# scribe

**A permanent archive of every Claude Code conversation, and a live web view to
read it in.**

```
scribe install     # register the hooks
scribe serve       # open the viewer
```

Python 3 standard library only. Nothing here writes to a transcript, and
nothing enters the session's context window.

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

**Explains opaque tool calls in plain English.** A `python3 - <<'EOF'` heredoc
becomes a sentence in the margin, next to the call — ideally while you are still
deciding whether to approve it. Answers cache by content, so a command explained
once is annotated everywhere it ever appears.

**Can talk back** (both opt-in, both off by default). Approve or deny a tool call
from the browser, edit the command first, or type a reply delivered when the
turn ends.

---

## Install

Requires `python3` (3.9+) and Claude Code. No dependencies.

```bash
git clone https://github.com/jhelvy/scribe
cd scribe
./bin/scribe archive          # back up everything you still have, right now
./bin/scribe install          # register hooks in ~/.claude/settings.json
./bin/scribe serve            # start the daemon and open the viewer
```

Run `scribe archive` before anything else. It is the step with a deadline.

`install` backs the settings file up first, writes *through* a symlink rather
than replacing it (dotfiles setups keep working), and only touches entries it
recognises as its own. `--uninstall` restores exactly what was there before;
`--dry-run` shows the diff without writing.

Once installed, the daemon starts itself when a session begins.

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

## Two-way control

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

**Replies.** A compose box queues text, delivered through the `Stop` hook when
the turn ends. Guards: `stop_hook_active` is honoured so a blocked stop never
triggers another, and `reply_queue.max_chain` (default 5) caps consecutive
injections. A prompt typed in the terminal resets the count.

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
| `remote_approval.enabled` | `false` | approve from the browser |
| `reply_queue.enabled` | `false` | type replies from the browser |
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
```

`SCRIBE_HOME` and `CLAUDE_CONFIG_DIR` redirect everything, which is how the tests
stay off your real data.

MIT.

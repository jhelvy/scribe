"""The ``scribe`` command line.

Anything with a right answer belongs here rather than in prose a model has to
interpret — a lesson the previous generation of this tool learned expensively,
when a skill told the model to "find the project root", it picked the git root,
and the served log was silently not the one being written.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from . import config, paths, store, transcript
from .render_md import fmt_datetime, human_duration, human_tokens


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    handler = getattr(args, "handler", None)
    if handler is None:
        parser.print_help()
        return 0
    try:
        return handler(args) or 0
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scribe",
        description="A readable, regenerable copy of every Claude Code session.",
    )
    from . import __version__

    parser.add_argument("--version", action="version", version=f"scribe {__version__}")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("serve", help="run the local daemon and web view")
    p.add_argument("--port", type=int, default=None)
    p.add_argument("--background", action="store_true", help="detach and return")
    p.add_argument("--no-browser", action="store_true")
    p.add_argument("--foreground", action="store_true", help="run attached (default)")
    p.set_defaults(handler=cmd_serve)

    p = sub.add_parser("open", help="open the web view in a browser")
    p.add_argument("session", nargs="?", help="session id, or blank for this project's latest")
    p.set_defaults(handler=cmd_open)

    p = sub.add_parser("list", help="list captured sessions")
    p.add_argument("-n", "--limit", type=int, default=20)
    p.add_argument("--project", help="filter by project slug or path")
    p.add_argument("--all", action="store_true", help="every session, not just this project")
    p.add_argument("--json", action="store_true")
    p.set_defaults(handler=cmd_list)

    p = sub.add_parser("build", help="(re)generate markdown from transcripts")
    p.add_argument("session", nargs="?", help="session id or transcript path")
    p.add_argument("--all", action="store_true", help="every session on this machine")
    p.add_argument("--project", help="only sessions for this project path")
    p.add_argument("--quiet", action="store_true")
    p.set_defaults(handler=cmd_build)

    p = sub.add_parser("search", help="search every conversation")
    p.add_argument("query", nargs="+", help="words, \"quoted phrases\", or prefix*")
    p.add_argument("-n", "--limit", type=int, default=20, help="conversations to show")
    p.add_argument("--json", action="store_true")
    p.add_argument("--reindex", action="store_true", help="rebuild the index first")
    p.set_defaults(handler=cmd_search)

    p = sub.add_parser("archive", help="copy transcripts into the permanent archive")
    p.add_argument("session", nargs="?", help="one session, or blank for all")
    p.add_argument("--quiet", action="store_true")
    p.set_defaults(handler=cmd_archive)

    p = sub.add_parser("path", help="print the markdown path for a session")
    p.add_argument("session", nargs="?")
    p.set_defaults(handler=cmd_path)

    p = sub.add_parser("show", help="print a session's markdown to stdout")
    p.add_argument("session", nargs="?")
    p.set_defaults(handler=cmd_show)

    p = sub.add_parser("export", help="write a standalone, self-contained HTML file")
    p.add_argument("session", nargs="?")
    p.add_argument("-o", "--output", help="target file (default: alongside the markdown)")
    p.set_defaults(handler=cmd_export)

    p = sub.add_parser("install", help="register scribe's hooks with Claude Code")
    p.add_argument("--project", action="store_true", help="write .claude/settings.json here")
    p.add_argument("--uninstall", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(handler=cmd_install)

    p = sub.add_parser("status", help="show daemon and capture status")
    p.set_defaults(handler=cmd_status)

    p = sub.add_parser("stop", help="stop the daemon")
    p.set_defaults(handler=cmd_stop)

    p = sub.add_parser("config", help="read and write settings")
    p.add_argument("action", choices=["list", "get", "set", "path"], nargs="?", default="list")
    p.add_argument("key", nargs="?")
    p.add_argument("value", nargs="?")
    p.set_defaults(handler=cmd_config)

    p = sub.add_parser("replay", help="replay a transcript into a scratch session (dev/demo)")
    p.add_argument("source", help="transcript path or session id to replay")
    p.add_argument("--speed", type=float, default=20.0, help="times real time (0 = as fast as possible)")
    p.add_argument("--rounds", type=int, default=0, help="stop after N rounds")
    p.set_defaults(handler=cmd_replay)

    p = sub.add_parser("hook", help=argparse.SUPPRESS)
    p.set_defaults(handler=cmd_hook)

    return parser


# ---------------------------------------------------------------- resolution


def resolve_ref(token: str | None, cwd: str | None = None) -> transcript.SessionRef | None:
    """Turn a user-supplied session token into a transcript.

    Accepts a path, a full session id, or a unique id prefix. With nothing at
    all, picks the most recent session for the current directory — which is
    almost always what someone typing ``scribe open`` in a repo means.
    """
    if token:
        candidate = Path(token).expanduser()
        if candidate.is_file():
            return transcript.peek(candidate)
        exact = transcript.find_transcript(token)
        if exact:
            return transcript.peek(exact)
        matches = [r for r in transcript.index_sessions() if r.session_id.startswith(token)]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            sys.stderr.write(f"scribe: '{token}' matches {len(matches)} sessions\n")
            for ref in matches[:8]:
                sys.stderr.write(f"  {ref.session_id}  {ref.title}\n")
            return None
        return None

    here = os.path.abspath(cwd or os.getcwd())
    local = transcript.transcripts_for_cwd(here)
    if local:
        return local[0]
    # Fall back to the newest session anywhere; better than a bare failure when
    # someone runs this from a directory Claude Code has never been used in.
    everything = transcript.index_sessions()
    return everything[0] if everything else None


def _need_ref(token, cwd=None):
    ref = resolve_ref(token, cwd)
    if ref is None:
        sys.stderr.write(
            "scribe: no session found. Run this inside a directory you have "
            "used Claude Code in, or pass a session id (see `scribe list --all`).\n"
        )
    return ref


# ---------------------------------------------------------------- commands


def cmd_list(args) -> int:
    refs = transcript.index_sessions()
    if not args.all:
        target = os.path.abspath(os.path.expanduser(args.project or os.getcwd()))
        if args.project and not os.path.isdir(target):
            refs = [r for r in refs if paths.project_slug(r.cwd or "") == args.project]
        else:
            refs = [r for r in refs if r.cwd and os.path.abspath(r.cwd) == target]
    refs = refs[: args.limit] if args.limit else refs

    if args.json:
        print(json.dumps([r.as_dict() for r in refs], indent=2))
        return 0

    if not refs:
        print("No sessions found. Try `scribe list --all`.")
        return 0

    width = max(len(paths.project_slug(r.cwd or "")) for r in refs) if refs else 8
    for ref in refs:
        project = paths.project_slug(ref.cwd) if ref.cwd else ref.project_dir
        size = f"{ref.size / 1024:.0f}K" if ref.size < 1_048_576 else f"{ref.size / 1_048_576:.1f}M"
        print(
            f"{ref.session_id[:8]}  {fmt_datetime(ref.updated or ref.started):<16}  "
            f"{project:<{width}}  {size:>6}  {ref.title}"
        )
    return 0


def cmd_build(args) -> int:
    cfg = config.load()
    paths.ensure_dirs()

    if args.all or args.project:
        refs = transcript.index_sessions()
        if args.project:
            target = os.path.abspath(os.path.expanduser(args.project))
            refs = [r for r in refs if r.cwd and os.path.abspath(r.cwd) == target]
    else:
        ref = _need_ref(args.session)
        if ref is None:
            return 1
        refs = [ref]

    from . import explain

    shared = explain.Explainer(cfg)  # one cache read for the whole run
    started = time.time()
    written = failed = 0
    for ref in refs:
        try:
            session, path = store.build_one(ref.path, cfg, shared)
            store.prune_stale_logs(session)
        except Exception as exc:  # a single bad transcript must not abort a bulk run
            failed += 1
            sys.stderr.write(f"scribe: {ref.session_id[:8]}: {type(exc).__name__}: {exc}\n")
            continue
        written += 1
        if not args.quiet:
            print(f"{path}  ({len(session.rounds)} rounds, {session.tool_count} tools)")

    if len(refs) > 1:
        elapsed = time.time() - started
        print(f"\n{written} log{'s' if written != 1 else ''} written in {elapsed:.1f}s", end="")
        print(f", {failed} failed" if failed else "")
    return 1 if failed and not written else 0


HL_OPEN, HL_CLOSE = "\x02", "\x03"


def cmd_search(args) -> int:
    from . import search

    index = search.get_index()
    query = " ".join(args.query)

    stale = index.stats()["sessions"] == 0
    if args.reindex or stale:
        sys.stderr.write("indexing…\r")
        sys.stderr.flush()
        index.sync(force=args.reindex)
        sys.stderr.write(" " * 20 + "\r")
    else:
        index.sync()  # incremental; unchanged sessions cost one lookup each

    result = index.search(query, limit=1000)
    if args.json:
        print(json.dumps(result, indent=2))
        return 0

    if result.get("error"):
        sys.stderr.write(f"scribe: {result['error']}\n")
        return 2
    if not result["sessions"]:
        print(f"no matches for {query!r}")
        return 1

    bold, dim, reset = ("\033[1m", "\033[2m", "\033[0m") if sys.stdout.isatty() else ("", "", "")
    shown = result["sessions"][: args.limit]
    print(
        f"{result['total']} match{'es' if result['total'] != 1 else ''} in "
        f"{len(result['sessions'])} conversation{'s' if len(result['sessions']) != 1 else ''}\n"
    )
    for entry in shown:
        flag = " [kept]" if entry["archived"] else ""
        print(f"{bold}{entry['id'][:8]}{reset}  {entry['title']}{flag}")
        print(f"        {dim}{entry['project']} · {fmt_datetime(entry['updated'])} · "
              f"{entry['hits']} hit{'s' if entry['hits'] != 1 else ''}{reset}")
        for match in entry["matches"][:2]:
            snippet = " ".join(match["snippet"].split())
            snippet = snippet.replace(HL_OPEN, bold).replace(HL_CLOSE, reset)
            print(f"        {dim}r{match['round']}{reset} {snippet}")
        print()
    if len(result["sessions"]) > len(shown):
        print(f"{dim}… {len(result['sessions']) - len(shown)} more (-n to show){reset}")
    print(f"{dim}open one with: scribe open <id>{reset}")
    return 0


def cmd_archive(args) -> int:
    from . import archive

    if args.session:
        ref = _need_ref(args.session)
        if ref is None:
            return 1
        refs = [ref]
    else:
        refs = transcript.index_sessions()

    started = time.time()
    stats = archive.sweep(refs)
    if not args.quiet:
        print(
            f"archived {stats.sessions} session{'s' if stats.sessions != 1 else ''}: "
            f"{stats.files} file{'s' if stats.files != 1 else ''}, "
            f"{archive.human_bytes(stats.bytes_copied)} new in {time.time() - started:.1f}s"
        )
        if stats.rotated:
            print(f"  {stats.rotated} rewritten source(s) preserved as generations")
        if stats.errors:
            print(f"  {stats.errors} error(s)")
        total = archive.summary()
        print(f"  archive now holds {total['sessions']} sessions, "
              f"{archive.human_bytes(total['bytes'])} at {total['path']}")
    return 0


def cmd_path(args) -> int:
    ref = _need_ref(args.session)
    if ref is None:
        return 1
    session = store.load(ref.path)
    print(store.log_path_for(session))
    return 0


def cmd_show(args) -> int:
    ref = _need_ref(args.session)
    if ref is None:
        return 1
    session, path = store.build_one(ref.path)
    try:
        sys.stdout.write(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        sys.stderr.write(f"scribe: {exc}\n")
        return 1
    return 0


def cmd_export(args) -> int:
    from . import export

    ref = _need_ref(args.session)
    if ref is None:
        return 1
    session = store.load(ref.path)
    target = Path(args.output).expanduser() if args.output else Path(session.log_path).with_suffix(".html")
    export.write_standalone(session, target)
    print(target)
    return 0


def cmd_serve(args) -> int:
    from . import daemon

    return daemon.run(
        port=args.port,
        background=args.background,
        open_browser=not args.no_browser,
    )


def cmd_open(args) -> int:
    from . import daemon

    return daemon.open_browser_for(args.session)


def cmd_status(args) -> int:
    from . import daemon

    return daemon.print_status()


def cmd_stop(args) -> int:
    from . import daemon

    return daemon.stop()


def cmd_install(args) -> int:
    from . import install

    if args.uninstall:
        return install.uninstall(project=args.project, dry_run=args.dry_run)
    return install.install(project=args.project, dry_run=args.dry_run)


def cmd_config(args) -> int:
    cfg = config.load()
    if args.action == "path":
        print(paths.config_file())
        return 0
    if args.action == "list":
        for key, value in config.flatten(cfg):
            print(f"{key} = {json.dumps(value)}")
        return 0
    if args.action == "get":
        if not args.key:
            sys.stderr.write("scribe: config get needs a key\n")
            return 2
        print(json.dumps(config.get_in(cfg, args.key)))
        return 0
    if args.action == "set":
        if not args.key or args.value is None:
            sys.stderr.write("scribe: config set needs a key and a value\n")
            return 2
        if not config.known_key(args.key):
            sys.stderr.write(
                f"scribe: unknown key '{args.key}'. See `scribe config list`.\n"
            )
            return 2
        stored = config.load()
        config.set_in(stored, args.key, config.coerce(args.key, args.value))
        config.save(stored)
        print(f"{args.key} = {json.dumps(config.get_in(stored, args.key))}")
        from . import daemon

        daemon.notify_config_changed()
        return 0
    return 2


def cmd_replay(args) -> int:
    from . import replay

    return replay.run(args.source, speed=args.speed, max_rounds=args.rounds)


def cmd_hook(args) -> int:
    from . import hookclient

    return hookclient.main()


if __name__ == "__main__":
    sys.exit(main())

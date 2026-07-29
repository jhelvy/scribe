"""scribe — a readable, regenerable copy of every Claude Code session.

Reads Claude Code's own JSONL transcripts, builds a session model, and emits a
markdown log plus a live web view. Nothing here writes to a transcript, and
nothing enters the session's context window.
"""

__version__ = "0.1.0"

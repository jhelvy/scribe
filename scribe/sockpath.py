"""Where the control socket lives.

Its own module because two very different callers must agree on the answer: the
daemon, which binds it, and the hook client, which is kept free of package
imports so it starts fast. A disagreement here would silently disable every
hook, so there is exactly one implementation and a test that pins it.

The wrinkle is that ``AF_UNIX`` paths are capped by ``sun_path`` — 104 bytes on
macOS, 108 on Linux — regardless of how long a normal filesystem path may be.
``~/.scribe/run/control.sock`` fits comfortably, but ``SCRIBE_HOME`` pointed
at a deep directory does not, and the failure is an unhelpful
``OSError: AF_UNIX path too long`` at bind time. So: use the natural path when
it fits, and a short hashed name in the system temp directory when it does not.
"""

from __future__ import annotations

import hashlib
import os
import tempfile

# Conservative: the shortest real limit is 104 including the trailing NUL, and
# leaving headroom costs nothing.
MAX_SUN_PATH = 92


def scribe_root() -> str:
    override = os.environ.get("SCRIBE_HOME")
    if override:
        return os.path.abspath(os.path.expanduser(override))
    return os.path.join(os.path.expanduser("~"), ".scribe")


def control_socket_path() -> str:
    root = scribe_root()
    natural = os.path.join(root, "run", "control.sock")
    if len(natural) <= MAX_SUN_PATH:
        return natural
    digest = hashlib.sha1(root.encode("utf-8")).hexdigest()[:10]
    return os.path.join(tempfile.gettempdir(), f"scribe-{digest}.sock")

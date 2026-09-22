"""Entry point module for running CLV via `python -m clv` or PyInstaller.

The spawn handshake below runs **before** any of CLV is imported, and the
ordering is the whole point rather than a style choice.

An isolated plugin (`clv/plugins/host.py`) runs in a child started with the
spawn method, and spawn starts that child by re-running ``sys.executable``. In a
frozen build ``sys.executable`` is the CLV binary itself, launched as
``clv --multiprocessing-fork …``; unless that argv is recognised and handled
here, the child runs the application instead — a terminal app launching copies
of itself, once per isolated plugin call, which is a worse failure than the one
isolation exists to prevent.

**Two details, both load-bearing, both easy to "clean up" into a bug.**

*It is `multiprocessing.spawn.freeze_support`, not `multiprocessing`'s.* The
documented spelling delegates to this one only where the running interpreter
thinks it is needed, and on **3.11 — the version the release binaries are built
with — that check is `sys.platform == 'win32'`**, so the documented call does
nothing at all on the Linux bundle this exists for. 3.14 widened it to any
frozen build. The implementation underneath is the same on both and detects a
spawned child from argv, so calling it directly is correct on both and on
whatever the next version decides.

*It is guarded by `sys.frozen`, so a source run imports nothing.* Outside a
frozen build spawn launches its child as ``python -c …`` and never re-enters
this file, so the import would be ~30 ms of startup bought for nothing on every
`python -m clv`. The console scripts (``clv.app:run``) do not come through here
at all. If `sys.frozen` were ever not set in a bundle the child would break
loudly rather than silently, and the release workflow's isolation smoke test is
what catches it — nothing in the test suite can, because the suite only ever has
an interpreter.
"""

import sys

if getattr(sys, "frozen", False):  # pragma: no cover - only in a frozen build
    from multiprocessing.spawn import freeze_support

    freeze_support()

from clv.app import run  # noqa: E402 - must not precede the spawn handshake


def main() -> None:
    run()


if __name__ == "__main__":
    main()

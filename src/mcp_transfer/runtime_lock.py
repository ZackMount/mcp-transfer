from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def profile_lock(home: Path):
    """Only one background agent may rotate a profile's refresh credential."""
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(home / "agent.lock", "a+b") as handle:
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise ValueError("This profile is already running. Open its WebUI or restart its scheduled task.") from error
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

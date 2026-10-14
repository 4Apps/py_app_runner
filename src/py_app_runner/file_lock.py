"""flock-based locks, for a database with no advisory locks of its own (SQLite).

The kernel releases a flock when the holding process dies, which is the property the
Postgres advisory locks are used for. Lock files are never deleted: unlinking one while
another process waits on it would hand the two of them different files.
"""

import fcntl
import os


def try_lock(path: str) -> int | None:
    """The locked file descriptor, or None when another holder has it."""

    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    except BaseException:
        os.close(fd)
        raise

    return fd


def lock(path: str) -> int:
    """Blocks until the lock is free."""

    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
    except BaseException:
        os.close(fd)
        raise

    return fd


def unlock(fd: int) -> None:
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)

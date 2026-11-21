"""Bounded interprocess locks for privileged hardware and authentication changes."""
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import stat
import time

ROOT = Path('/run/oh-my-panasonic-locks')


@contextmanager
def private_lock(name, timeout=12):
    ROOT.mkdir(mode=0o700, exist_ok=True)
    directory = os.open(ROOT, os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(directory)
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise PermissionError('控制锁目录权限异常')
        if not name or '/' in name or name in ('.','..'):
            raise ValueError('Invalid lock name')
        fd = os.open(name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=directory)
    finally:
        os.close(directory)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise PermissionError('控制锁权限异常')
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError('另一个操作尚未完成，请稍后重试')
                time.sleep(0.05)
        yield
    finally:
        os.close(fd)

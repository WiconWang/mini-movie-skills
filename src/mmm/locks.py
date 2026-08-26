"""跨进程流水线锁：防止同名任务或同一源视频被并行改写。"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import os
from contextlib import contextmanager
from pathlib import Path

from .db import PROJECT_ROOT


class LockBusy(RuntimeError):
    """互斥资源已被另一个流水线进程持有。"""


def _lock_path(key: str) -> Path:
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return PROJECT_ROOT / ".locks" / f"{digest}.lock"


def _holder_pid(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return None


@contextmanager
def exclusive_lock(key: str):
    """获取非阻塞跨进程互斥锁；冲突时快速失败，避免覆盖中间产物。"""
    path = _lock_path(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    acquired = False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                pid = _holder_pid(path)
                holder = f"PID {pid}" if pid is not None else "另一个进程"
                raise LockBusy(f"资源 {key} 正在被 {holder} 处理") from exc
            raise
        acquired = True
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode("ascii"))
        yield path
    finally:
        if acquired:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


@contextmanager
def exclusive_locks(*keys: str):
    """同时持有多个锁；按键排序获取，避免两个多视频任务互相死锁。"""
    held = []
    try:
        for key in sorted(set(keys)):
            cm = exclusive_lock(key)
            path = cm.__enter__()
            held.append(cm)
        yield
    finally:
        for cm in reversed(held):
            cm.__exit__(None, None, None)

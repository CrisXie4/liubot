"""单实例锁 — 防止 main.py 双开导致消息被重复处理（双倍回复/重复禁言）。

原理: 对 data/bot.lock 做操作系统级文件锁（POSIX: flock / Windows: msvcrt.locking）。
持锁进程死亡（含 kill -9）时内核自动释放，无需善后；
锁文件内容为持锁 PID，仅供人工排查，不参与判定。

Windows 细节: msvcrt 的字节区间锁会屏蔽其他句柄对锁定字节的读写，
因此锁的是偏移 4096 处的字节（避开文件开头的 PID 内容区），PID 读写不受影响。
"""
import logging
import os

logger = logging.getLogger("qqbot")

_DEFAULT_LOCK_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "bot.lock"
)

_LOCK_BYTE_OFFSET = 4096

_lock_fd: int | None = None


def acquire_single_instance_lock(lock_path: str | None = None) -> bool:
    """尝试获取单实例锁。成功返回 True；已有实例持锁返回 False（应退出启动）。"""
    global _lock_fd
    if _lock_fd is not None:
        return True
    path = lock_path or _DEFAULT_LOCK_PATH
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        if os.name == "nt":
            import msvcrt
            os.lseek(fd, _LOCK_BYTE_OFFSET, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        old_pid = ""
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                old_pid = f.read().strip()
        except OSError:
            pass
        os.close(fd)
        logger.error(
            "检测到已有 Bot 实例正在运行（%s 记录 PID=%s），"
            "为避免双开导致消息重复处理，本次启动退出。",
            path,
            old_pid or "未知",
        )
        return False
    _lock_fd = fd
    os.lseek(fd, 0, os.SEEK_SET)
    pid_bytes = f"{os.getpid()}\n".encode()
    os.write(fd, pid_bytes)
    os.ftruncate(fd, len(pid_bytes))
    logger.info("单实例锁已获取 (PID=%d, lock=%s)", os.getpid(), path)
    return True


def release_single_instance_lock() -> None:
    """释放单实例锁（正常仅测试/特殊场景使用；进程退出时内核会自动释放）。"""
    global _lock_fd
    if _lock_fd is None:
        return
    try:
        os.close(_lock_fd)
    except OSError:
        pass
    _lock_fd = None

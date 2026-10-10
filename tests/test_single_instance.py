"""单实例锁测试 — python tests/test_single_instance.py

验证防双开锁的核心行为:
  1. 同进程重复 acquire 直接放行
  2. 其他进程持锁时 acquire 被拒
  3. 持锁进程死亡后锁自动释放（内核回收，部署重启场景）
  4. 锁文件记录持锁 PID，方便排查
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import single_instance

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestSingleInstanceLock(unittest.TestCase):
    def setUp(self):
        single_instance.release_single_instance_lock()

    def _tmp_lock_path(self):
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(d, ignore_errors=True))
        return os.path.join(d, "bot.lock")

    def test_acquire_release_cycle(self):
        path = self._tmp_lock_path()
        self.assertTrue(single_instance.acquire_single_instance_lock(path))
        self.assertTrue(single_instance.acquire_single_instance_lock(path))
        single_instance.release_single_instance_lock()
        self.assertTrue(single_instance.acquire_single_instance_lock(path))

    def test_lock_file_records_pid(self):
        path = self._tmp_lock_path()
        self.assertTrue(single_instance.acquire_single_instance_lock(path))
        with open(path, encoding="utf-8") as f:
            self.assertEqual(f.read().strip(), str(os.getpid()))

    def test_other_process_denied_and_auto_release_on_death(self):
        path = self._tmp_lock_path()
        code = (
            "import sys, time\n"
            f"sys.path.insert(0, {REPO_ROOT!r})\n"
            "from bot import single_instance as si\n"
            f"assert si.acquire_single_instance_lock({path!r}), 'child should get lock'\n"
            "print('LOCKED', flush=True)\n"
            "time.sleep(60)\n"
        )
        p = subprocess.Popen(
            [sys.executable, "-c", code],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            self.assertEqual(p.stdout.readline().strip(), "LOCKED")
            self.assertFalse(single_instance.acquire_single_instance_lock(path))
        finally:
            p.terminate()
            p.wait(timeout=10)
        self.assertTrue(single_instance.acquire_single_instance_lock(path))


if __name__ == "__main__":
    unittest.main(verbosity=2)

"""并发：读请求不等写锁，写请求仍然串行。

这些用例不追求压测，只证明两件事：
1. 一个线程握着全局写锁时，读方法仍能完成；
2. 每个线程用自己的 SQLite 连接，并发写不会串数据。
"""

from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from store.service import Store
from tests.test_service import SKILL, skill_manifest


class ConcurrencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.store = Store(root / "store.db", root / "blobs")
        self.addCleanup(self.store.close)
        _, self.publisher = self.store.create_principal("作者", "publisher")
        _, self.admin = self.store.create_principal("管理员", "admin")

    def _publish_one(self) -> None:
        result = self.store.submit(self.publisher, skill_manifest(), {"SKILL.md": SKILL.encode()})
        self.store.approve(self.admin, result.submission_id)

    def test_read_completes_while_the_write_lock_is_held(self) -> None:
        self._publish_one()
        done = threading.Event()
        error: list[BaseException] = []

        def read() -> None:
            try:
                self.store.search("")
            except BaseException as exc:  # noqa: BLE001 - 测试里原样带回
                error.append(exc)
            finally:
                done.set()

        self.store._lock.acquire()
        try:
            worker = threading.Thread(target=read)
            worker.start()
            self.assertTrue(done.wait(2.0), "读方法被全局写锁挡住了")
        finally:
            self.store._lock.release()
        worker.join(2.0)
        self.assertEqual(error, [])

    def test_each_thread_gets_its_own_connection(self) -> None:
        ids: list[int] = []
        lock = threading.Lock()
        barrier = threading.Barrier(4)

        def worker() -> None:
            connection = self.store._db
            with lock:
                ids.append(id(connection))
            barrier.wait(2.0)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5.0)
        self.assertEqual(len(ids), 4)
        self.assertEqual(len(set(ids)), 4, "四个线程应该拿到四条连接")

    def test_concurrent_submits_do_not_lose_rows(self) -> None:
        results: list[str] = []
        errors: list[BaseException] = []
        lock = threading.Lock()

        def worker() -> None:
            try:
                result = self.store.submit(
                    self.publisher, skill_manifest(), {"SKILL.md": SKILL.encode()}
                )
                with lock:
                    results.append(result.status)
            except BaseException as exc:  # noqa: BLE001
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(20.0)
        self.assertEqual(errors, [])
        self.assertEqual(results, ["pending"] * 6)
        count = self.store._db.execute("SELECT COUNT(*) FROM submissions").fetchone()[0]
        self.assertEqual(count, 6)

    def test_memory_database_still_works(self) -> None:
        store = Store(Path(":memory:"), Path(self.tmp.name) / "mem-blobs")
        self.addCleanup(store.close)
        _, publisher = store.create_principal("作者", "publisher")
        result = store.submit(publisher, skill_manifest(), {"SKILL.md": SKILL.encode()})
        self.assertEqual(result.status, "pending")
        self.assertEqual(len(store.list_my_submissions(publisher)), 1)


if __name__ == "__main__":
    unittest.main()

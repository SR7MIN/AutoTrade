import tempfile
import unittest
from pathlib import Path

from autotrade.errors import InstanceLockError
from autotrade.locking import SingleInstanceLock, lock_owner_active


class LockingTests(unittest.TestCase):
    def test_only_one_writer_can_hold_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "writer.lock"
            first = SingleInstanceLock(path)
            second = SingleInstanceLock(path)
            first.acquire()
            try:
                self.assertTrue(lock_owner_active(path))
                with self.assertRaises(InstanceLockError):
                    second.acquire()
            finally:
                first.release()
            self.assertFalse(lock_owner_active(path))
            second.acquire()
            second.release()

    def test_stale_lock_is_not_reported_as_active(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "writer.lock"
            path.write_text('{"pid": 2147483647}', encoding="utf-8")
            self.assertFalse(lock_owner_active(path))


if __name__ == "__main__":
    unittest.main()

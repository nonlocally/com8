#!/usr/bin/env python3
"""Control-token file boundaries, with no daemon, sockets, or existing state."""
import multiprocessing
import os
from pathlib import Path
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import com8


def rig(root):
    instance = object.__new__(com8.Com8)
    instance.root = str(root)
    return instance


def concurrent_creator(root, barrier, results):
    # All processes finish writing their candidate before any can publish it.
    # This exercises the first-creation race on real filesystem operations.
    original_link = os.link

    def publish(*args, **kwargs):
        barrier.wait(timeout=15)
        return original_link(*args, **kwargs)

    try:
        with mock.patch.object(com8.os, "link", publish):
            results.put((True, rig(root)._ensure_control_token()))
    except Exception as error:
        results.put((False, repr(error)))


class ControlTokenTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="com8-token-")
        self.root = Path(self.temp.name)
        self.token = self.root / "control.token"
        self.com8 = rig(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def test_legacy_mode_repaired_without_changing_value_or_bytes(self):
        self.token.write_bytes(b"  existing-protocol-token\n")
        self.token.chmod(0o644)
        self.assertEqual(self.com8._ensure_control_token(), "existing-protocol-token")
        self.assertEqual(self.token.read_bytes(), b"  existing-protocol-token\n")
        self.assertEqual(stat.S_IMODE(self.token.stat().st_mode), 0o600)

    def test_symlink_target_untouched_and_dangling_alias_not_created(self):
        target = self.root / "other-secret"
        target.write_bytes(b"unrelated-secret")
        target.chmod(0o644)
        for destination in [target, self.root / "missing-secret"]:
            with self.subTest(destination=destination):
                self.token.symlink_to(destination)
                with self.assertRaises(OSError):
                    self.com8._ensure_control_token()
                self.assertTrue(self.token.is_symlink())
                self.assertEqual(target.read_bytes(), b"unrelated-secret")
                self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o644)
                self.assertFalse((self.root / "missing-secret").exists())
                self.token.unlink()

    def test_nonregular_paths_refused_without_blocking(self):
        self.token.mkdir()
        before = self.token.stat().st_mode
        with self.assertRaises(PermissionError):
            self.com8._ensure_control_token()
        self.assertEqual(self.token.stat().st_mode, before)
        self.token.rmdir()
        os.mkfifo(self.token, 0o644)
        with self.assertRaises(PermissionError):
            self.com8._ensure_control_token()
        self.assertTrue(stat.S_ISFIFO(self.token.stat().st_mode))

    def test_foreign_inode_refused_before_chmod_or_read(self):
        self.token.write_bytes(b"foreign-secret")
        foreign = SimpleNamespace(st_mode=stat.S_IFREG | 0o644, st_uid=os.getuid() + 1)
        with mock.patch.object(com8.os, "fstat", return_value=foreign), \
                mock.patch.object(com8.os, "fchmod") as chmod:
            with self.assertRaises(PermissionError):
                self.com8._ensure_control_token()
            chmod.assert_not_called()
        self.assertEqual(self.token.read_bytes(), b"foreign-secret")

    def test_permission_failure_does_not_replace_existing_token(self):
        self.token.write_bytes(b"existing-secret")
        self.token.chmod(0o644)
        with mock.patch.object(com8.os, "fchmod", side_effect=PermissionError("fixture")):
            with self.assertRaises(PermissionError):
                self.com8._ensure_control_token()
        self.assertEqual(self.token.read_bytes(), b"existing-secret")
        self.assertEqual(list(self.root.iterdir()), [self.token])

    @unittest.skipIf(os.getuid() == 0, "root bypasses file read permissions")
    def test_unreadable_file_refused_without_overwrite(self):
        self.token.write_bytes(b"unreadable-secret")
        self.token.chmod(0)
        try:
            with self.assertRaises(PermissionError):
                self.com8._ensure_control_token()
        finally:
            self.token.chmod(0o600)
        self.assertEqual(self.token.read_bytes(), b"unreadable-secret")

    def test_invalid_existing_contents_refused_without_regeneration(self):
        for contents in [b"", b" \n", b"\xff"]:
            with self.subTest(contents=contents):
                self.token.write_bytes(contents)
                with self.assertRaises((ValueError, UnicodeError)):
                    self.com8._ensure_control_token()
                self.assertEqual(self.token.read_bytes(), contents)

    def test_new_token_is_private_even_under_restrictive_umask(self):
        previous = os.umask(0o777)
        try:
            value = self.com8._ensure_control_token()
        finally:
            os.umask(previous)
        self.assertRegex(value, r"^[a-f0-9]{48}$")
        self.assertEqual(self.token.read_text(), value)
        self.assertEqual(stat.S_IMODE(self.token.stat().st_mode), 0o600)
        self.assertEqual(self.com8._ensure_control_token(), value)
        self.assertEqual(list(self.root.iterdir()), [self.token])

    def test_concurrent_first_creation_returns_one_complete_token(self):
        context = multiprocessing.get_context("spawn")
        count = 8
        barrier, results = context.Barrier(count), context.Queue()
        workers = [context.Process(target=concurrent_creator, args=(self.root, barrier, results))
                   for _ in range(count)]
        try:
            for worker in workers:
                worker.start()
            replies = [results.get(timeout=25) for _ in workers]
            for worker in workers:
                worker.join(timeout=5)
                self.assertEqual(worker.exitcode, 0)
            self.assertTrue(all(success for success, value in replies), replies)
            values = {value for success, value in replies}
            self.assertEqual(len(values), 1)
            self.assertEqual(self.token.read_text(), values.pop())
            self.assertEqual(stat.S_IMODE(self.token.stat().st_mode), 0o600)
            self.assertEqual(list(self.root.iterdir()), [self.token])
        finally:
            for worker in workers:
                if worker.is_alive():
                    worker.terminate()
                worker.join(timeout=5)
            results.close()


if __name__ == "__main__":
    unittest.main()

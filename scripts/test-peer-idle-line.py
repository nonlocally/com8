#!/usr/bin/env python3
"""A peer that listens on a session socket is seen as live by a prober that is slow.

`com8 status` calls a session live when a connection to its socket is still open after a short wait, and dead
when the line was closed before that. A listener therefore has to keep a connection on which nothing arrives
open for several times that wait. Otherwise a prober that is held up for a moment between its connect and its
read, as on a loaded machine, finds the line closed and reports a live session as stored.

Real listeners in an isolated folder; no daemon, no client and no network.
"""
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
sys.dont_write_bytecode = True
import cc_peer  # noqa: E402
import com8  # noqa: E402

PEER = str(ROOT / "lib/cc_peer.py")


class IdleLineTest(unittest.TestCase):
    def setUp(self):
        # Unix socket paths are short, so the fixture sits directly under /tmp as the other suites do.
        self.temp = Path(tempfile.mkdtemp(prefix="com8-idle-", dir="/tmp"))
        self.addCleanup(shutil.rmtree, self.temp, ignore_errors=True)
        (self.temp / "sessions").mkdir()
        self.env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        for key in ("CLAUDE_CODE_MESSAGING_SOCKET", "CODEX_THREAD_ID", "CODEX_SESSION_ID"):
            self.env.pop(key, None)

    def listen(self, role, *more):
        """Start one listener of cc_peer.py and return its socket path once it accepts connections."""
        path = str(self.temp / (role + ".sock"))
        listener = subprocess.Popen([sys.executable, PEER, role, "--socket", path, *more], env=self.env,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(self.end, listener)
        deadline = time.monotonic() + 20
        while True:
            self.assertIsNone(listener.poll(), "the %s listener ended before it listened" % role)
            try:
                with socket.socket(socket.AF_UNIX) as probe:
                    probe.connect(path)
                return path
            except OSError:
                self.assertLess(time.monotonic(), deadline, "the %s listener did not start" % role)
                time.sleep(0.05)

    def end(self, listener):
        """Stop one of this test's own listeners, by its handle."""
        listener.terminate()
        try:
            listener.wait(10)
        except subprocess.TimeoutExpired:
            listener.kill()
            listener.wait(10)

    def mailbox(self):
        box = self.temp / "box.jsonl"
        path = self.listen("mailbox", "--name", "carol", "--sessions-dir", str(self.temp / "sessions"),
                           "--mailbox", str(box))
        deadline = time.monotonic() + 20
        while not box.exists():  # it listens a moment before it has made its mailbox file
            self.assertLess(time.monotonic(), deadline, "the stand-in made no mailbox file")
            time.sleep(0.05)
        return path

    def codex_peer(self):
        # Nothing is sent to it, so the client it would start is never started.
        return self.listen("serve", "--device", "local", "--name", "dave", "--communicate", "/usr/bin/false",
                           "--sessions-dir", str(self.temp / "sessions"))

    def asker(self):
        # What `communicate ask` listens on for its answer, under a name that status can see.
        return self.listen("recv", "--timeout", "60", "--plant-name", "erin",
                           "--sessions-dir", str(self.temp / "sessions"))

    def responder(self):
        return self.listen("respond", "--reply", "noted", "--timeout", "60", "--loop")

    LISTENERS = ("mailbox", "codex_peer", "asker", "responder")

    def seconds_until_closed(self, path, limit):
        """How long the listener keeps a connection on which nothing is sent; `limit` if it is still open then."""
        with socket.socket(socket.AF_UNIX) as line:
            line.settimeout(limit)
            line.connect(path)
            began = time.monotonic()
            try:
                self.assertEqual(line.recv(1), b"")
            except socket.timeout:
                return limit
            return time.monotonic() - began

    def test_every_listener_keeps_an_idle_line_open(self):
        for kind in self.LISTENERS:
            with self.subTest(listener=kind):
                self.assertGreaterEqual(self.seconds_until_closed(getattr(self, kind)(), 1.5), 1.5)

    def test_a_prober_three_times_slower_than_the_shipped_one_still_finds_every_listener_live(self):
        for kind in self.LISTENERS:
            with self.subTest(listener=kind):
                path = getattr(self, kind)()
                self.assertEqual(com8.probe(path), "live")               # as shipped
                self.assertEqual(com8.probe(path, timeout=1.05), "live")  # held up, or simply slower

    def test_an_idle_line_is_closed_in_the_end(self):
        # A line nobody writes to is not kept for ever: the listener gives it up, and its thread with it.
        self.assertLess(self.seconds_until_closed(self.mailbox(), 6.0), 6.0)

    def test_a_message_is_taken_when_its_line_has_arrived_not_when_the_idle_time_is_over(self):
        path = self.mailbox()
        box = self.temp / "box.jsonl"
        message = {"type": "user", "message": {"role": "user", "content": "hello carol"},
                   "from": "uds:" + str(self.temp / "nobody.sock")}
        # The sender keeps its side open after the line, so nothing but the newline ends the listener's read.
        with socket.socket(socket.AF_UNIX) as sender:
            sender.connect(path)
            began = time.monotonic()
            sender.sendall((json.dumps(message) + "\n").encode())
            while not box.read_text().strip():
                self.assertLess(time.monotonic() - began, 1.5,
                                "a whole line waited for the idle time before it was taken")
                time.sleep(0.02)
        self.assertIn("hello carol", json.loads(box.read_text().splitlines()[0])["text"])

    def test_a_prober_that_hangs_up_frees_a_listener_that_takes_one_connection_at_a_time(self):
        # While the responder waits for a line on one connection it answers nothing else. A prober that hangs up
        # must end that wait at once: a message sent right after a probe is answered well inside the idle time.
        path = self.responder()
        answers_path = str(self.temp / "answers.sock")
        answers = socket.socket(socket.AF_UNIX)
        self.addCleanup(answers.close)
        answers.bind(answers_path)
        answers.listen(4)
        answers.settimeout(10)
        self.assertEqual(com8.probe(path), "live")
        message = {"type": "user", "message": {"role": "user", "content": "hello responder"},
                   "from": "uds:" + answers_path}
        began = time.monotonic()
        with socket.socket(socket.AF_UNIX) as sender:
            sender.connect(path)
            sender.sendall((json.dumps(message) + "\n").encode())
        conn, _ = answers.accept()
        waited = time.monotonic() - began
        with conn:
            conn.settimeout(10)
            reply = b""
            while b"\n" not in reply:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                reply += chunk
        self.assertIn("noted (you said: hello responder)", json.loads(reply)["message"]["content"])
        self.assertLess(waited, 1.0, "the prober's hang-up held the responder")

    def test_no_listener_and_a_closed_line_are_dead(self):
        self.assertEqual(com8.probe(str(self.temp / "nothing.sock")), "dead")
        # A listener that closes at once, as a process on its way out does.
        path = str(self.temp / "closing.sock")
        server = socket.socket(socket.AF_UNIX)
        self.addCleanup(server.close)
        server.bind(path)
        server.listen(4)
        server.settimeout(5)
        import threading

        def close_at_once():
            conn, _ = server.accept()
            conn.close()
        closer = threading.Thread(target=close_at_once, daemon=True)
        closer.start()
        self.assertEqual(com8.probe(path), "dead")
        closer.join(5)


if __name__ == "__main__":
    unittest.main()

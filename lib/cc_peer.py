#!/usr/bin/env python3
"""cc_peer — speak the Claude Code peer ("cc-socks") wire protocol.

Two roles:

  serve  Stand up a unix socket that Claude's discovery will list as a native
         peer. Each inbound message is handed to a Codex agent (via the sibling
         `communicate codex ask`, which runs `codex exec` over ssh), and the
         reply is written back to the sender's socket so it appears in their
         Claude session as a cross-session message.

  send   Raw-inject a single user message into any peer socket (the programmatic
         equivalent of the binary's own hint:
             echo '{"type":"user","message":{...}}' | socat - UNIX-CONNECT:<sock>)

Wire facts (reverse-engineered + verified live):
  * Messages are newline-delimited JSON written to the peer's unix socket.
  * An inbound user message is: {"type":"user","message":{"role":"user",
    "content": <string | [content-blocks]>}, "from":"uds:<socket-path>", ...}.
  * `from` is "uds:" + an absolute .sock path; a reply/receipt is delivered by
    connecting to that path and writing another user message.
  * Liveness discovery merely connects to the socket; accepting is enough.
    COM8's own probe (com8.probe) also waits a moment and needs the line to
    stay open, so every listener here keeps an idle line for IDLE_LINE_S.
"""
import argparse, json, os, re, signal, socket, subprocess, sys, threading, time, uuid


def _extract_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                parts.append(b.get("text", ""))
            elif isinstance(b, str):
                parts.append(b)
        return "".join(parts)
    return "" if content is None else str(content)


def _addr_from(uds):
    if isinstance(uds, str) and uds.startswith("uds:"):
        return uds[4:]
    return None


def _xml_attr(s):
    return (str(s).replace("&", "&amp;").replace('"', "&quot;")
            .replace("<", "&lt;").replace(">", "&gt;"))


def _wrap(text, from_sock, from_name):
    """Wrap a reply body as a cross-session-message so the receiver renders it
    with proper `from` / `from-name` attribution (matches PCr() in the binary).
    Delivery works with a raw string too, but the wrapper gives us identity."""
    # Never let the body forge the closing tag.
    body = text.replace("</cross-session-message", "</ cross-session-message")
    attrs = 'from="uds:%s"' % _xml_attr(from_sock)
    if from_name:
        attrs += ' from-name="%s"' % _xml_attr(from_name)
    return "<cross-session-message %s>\n%s\n</cross-session-message>" % (attrs, body)


def deliver(to_sock, text, from_sock, from_name=None, orig_msg_id=None):
    """Write one user message to `to_sock` so it lands in that Claude session.

    content MUST be a nonempty plain string (the listener hard-rejects
    non-strings); session_id is deliberately omitted (a mismatched one is
    dropped by the receiver)."""
    content = _wrap(text, from_sock, from_name) if from_name is not None else text
    if not content:
        return
    msg = {
        "type": "user",
        "message": {"role": "user", "content": content},
        "priority": "next",
        "from": "uds:" + from_sock,
        "msg_id": uuid.uuid4().hex,
    }
    data = (json.dumps(msg) + "\n").encode("utf-8")
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(10)
    try:
        s.connect(to_sock)
        s.sendall(data)
    finally:
        s.close()


# How long a listener keeps a connection on which no whole line has arrived. A prober calls a session live when
# the line is still open after a fraction of a second (com8.probe) and dead when it was closed before that, so
# this is several times that wait: a prober that is held up for a moment between its connect and its read must
# not find a live listener's line closed. The durable daemon's own listeners keep an idle line as long. A message
# ends in a newline and is read as soon as it has arrived; a prober that hangs up ends the read at once.
IDLE_LINE_S = 2.0


def _read_line(conn, timeout=IDLE_LINE_S):
    conn.settimeout(timeout)
    buf = b""
    try:
        while b"\n" not in buf:
            chunk = conn.recv(65536)
            if not chunk:
                break
            buf += chunk
            if len(buf) > 8 * 1024 * 1024:
                break
    except socket.timeout:
        pass
    except OSError:
        pass
    return buf


# Cap concurrent codex subprocesses so a burst of messages can't fork-bomb the
# device with ssh+codex processes. Excess senders get a brief "busy" reply.
_SLOTS = threading.Semaphore(4)


def _handle(conn, opts):
    raw = _read_line(conn)
    try:
        conn.close()
    except OSError:
        pass
    line = raw.split(b"\n", 1)[0].strip()
    if not line:
        return  # liveness probe or empty; nothing to do
    try:
        msg = json.loads(line.decode("utf-8", "replace"))
    except Exception:
        return
    if msg.get("type") != "user":
        return
    text = _extract_text((msg.get("message") or {}).get("content"))
    sender = _addr_from(msg.get("from"))
    if not text or not sender:
        return

    if not _SLOTS.acquire(blocking=False):
        try:
            deliver(sender, "[peer busy — too many concurrent requests, retry shortly]",
                    opts.socket, from_name=opts.name, orig_msg_id=msg.get("msg_id"))
        except Exception:
            pass
        return
    try:
        _run_and_reply(text, sender, msg, opts)
    finally:
        _SLOTS.release()


def _run_and_reply(text, sender, msg, opts):

    # Hand the message to the Codex agent on the target device. Reuse the
    # hardened `communicate codex ask` (stdin-safe, thread-continuity). Passing
    # text as a single argv element keeps it injection-safe.
    cmd = [opts.communicate, "codex", "ask", opts.device,
           "--thread", "peer-" + opts.name]
    if opts.dir:
        cmd += ["--dir", opts.dir]
    if opts.auto:
        cmd += ["--auto"]
    cmd += ["--", text]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=opts.timeout)
        reply = (out.stdout or "").strip() or ("[codex error] " + (out.stderr or "").strip()[:500])
    except subprocess.TimeoutExpired:
        reply = "[codex timed out after %ss]" % opts.timeout
    except Exception as e:  # pragma: no cover
        reply = "[peer error] %s" % e

    try:
        deliver(sender, reply, opts.socket, from_name=opts.name, orig_msg_id=msg.get("msg_id"))
    except Exception as e:
        sys.stderr.write("deliver failed: %s\n" % e)


def _sidecar_obj(sock, name, pid):
    now = int(time.time() * 1000)
    return {
        "pid": pid,                       # a LIVE pid -> the discovery sweep never reaps us
        "sessionId": str(uuid.uuid4()),
        "cwd": "-",
        "startedAt": now,
        "version": "communicate-peer",
        "peerProtocol": 1,
        "kind": "interactive",
        "entrypoint": "cli",
        "messagingSocketPath": sock,
        "name": name,
        "status": "idle",
        "updatedAt": now,
        "statusUpdatedAt": now,
    }


def serve(opts):
    if os.path.exists(opts.socket):
        os.unlink(opts.socket)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(opts.socket)
    os.chmod(opts.socket, 0o600)
    srv.listen(64)

    pid = os.getpid()
    sidecar = os.path.join(opts.sessions_dir, "%d.json" % pid) if opts.sessions_dir else None
    stop = threading.Event()

    def plant():
        if not sidecar:
            return
        try:
            tmp = sidecar + ".tmp"
            with open(tmp, "w") as f:
                json.dump(_sidecar_obj(opts.socket, opts.name, pid), f)
            os.replace(tmp, sidecar)
        except Exception as e:  # pragma: no cover
            sys.stderr.write("plant failed: %s\n" % e)

    def cleanup(*_a):
        stop.set()
        for p in (sidecar, opts.socket):
            try:
                if p and os.path.exists(p):
                    os.unlink(p)
            except OSError:
                pass
        os._exit(0)

    def replanter():
        while not stop.wait(3):
            plant()

    plant()
    threading.Thread(target=replanter, daemon=True).start()
    signal.signal(signal.SIGTERM, cleanup)
    signal.signal(signal.SIGINT, cleanup)
    sys.stderr.write("cc_peer serving %s (pid %d) -> codex@%s; sidecar=%s\n"
                     % (opts.name, pid, opts.device, sidecar))
    while True:
        try:
            conn, _ = srv.accept()
        except KeyboardInterrupt:
            cleanup()
        except OSError:
            continue
        threading.Thread(target=_handle, args=(conn, opts), daemon=True).start()


def _unwrap_leading(text):
    """Strip ONE leading cross-session-message wrapper. Returns (body, from_name).
    A wrapper appearing mid-text is content and is left alone."""
    m = re.match(r"^<cross-session-message\b([^>]*)>\n?(.*?)\n?</cross-session-message>\s*$",
                 text, re.S)
    if not m:
        return text, None
    nm = re.search(r'from-name="([^"]*)"', m.group(1))
    return m.group(2), (nm.group(1) if nm else None)


def recv(opts):
    """Listen on a socket and print the body of the first real message that
    arrives (ignoring empty liveness probes), then exit. With --plant-name the
    listener is a first-class bus citizen for its lifetime: a numeric sidecar
    (own live pid) so ListAgents lists it and native SendMessage can reply."""
    if os.path.exists(opts.socket):
        os.unlink(opts.socket)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(opts.socket)
    os.chmod(opts.socket, 0o600)
    srv.listen(8)
    srv.settimeout(opts.timeout)
    sidecar = None
    if getattr(opts, "plant_name", "") and getattr(opts, "sessions_dir", ""):
        sidecar = os.path.join(opts.sessions_dir, "%d.json" % os.getpid())
        with open(sidecar, "w") as f:
            json.dump(_sidecar_obj(opts.socket, opts.plant_name, os.getpid()), f)
    try:
        while True:
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                sys.stderr.write("recv: timed out\n")
                return 2
            raw = _read_line(conn)
            try:
                conn.close()
            except OSError:
                pass
            line = raw.split(b"\n", 1)[0].strip()
            if not line:
                continue
            try:
                msg = json.loads(line.decode("utf-8", "replace"))
            except Exception:
                continue
            if msg.get("type") != "user":
                continue
            body = _extract_text((msg.get("message") or {}).get("content"))
            if getattr(opts, "unwrap", False):
                body, from_name = _unwrap_leading(body)
                if from_name:
                    sys.stderr.write("recv: reply from %s\n" % from_name)
            sys.stdout.write(body)
            sys.stdout.flush()
            return 0
    finally:
        try:
            os.unlink(opts.socket)
        except OSError:
            pass
        if sidecar:
            try:
                os.unlink(sidecar)
            except OSError:
                pass


def mailbox(opts):
    """Stand up a persistent peer identity (a listening socket + a maintained
    sidecar) that does NOT back a codex agent — instead every inbound message is
    appended to a JSONL mailbox file. This is the orchestrator's return address:
    it sends peer messages with from=<this socket> and then tails the mailbox
    for the reply. Sweep-proof exactly like serve() (own pid names the sidecar,
    re-planted every 3s)."""
    if os.path.exists(opts.socket):
        os.unlink(opts.socket)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(opts.socket)
    os.chmod(opts.socket, 0o600)
    srv.listen(64)
    pid = os.getpid()
    sidecar = os.path.join(opts.sessions_dir, "%d.json" % pid) if opts.sessions_dir else None
    stop = threading.Event()
    lock = threading.Lock()

    def plant():
        if not sidecar:
            return
        try:
            tmp = sidecar + ".tmp"
            with open(tmp, "w") as f:
                # Compact separators: claude.sh locates a sidecar by grepping for
                # "messagingSocketPath":"<sock>" with no spaces, like Claude's own.
                json.dump(_sidecar_obj(opts.socket, opts.name, pid), f,
                          separators=(",", ":"))
            os.replace(tmp, sidecar)
        except Exception as e:  # pragma: no cover
            sys.stderr.write("plant failed: %s\n" % e)

    def cleanup(*_a):
        stop.set()
        for p in (sidecar, opts.socket):
            try:
                if p and os.path.exists(p):
                    os.unlink(p)
            except OSError:
                pass
        os._exit(0)

    def append(obj):
        line = json.dumps(obj) + "\n"
        with lock:
            with open(opts.mailbox, "a", encoding="utf-8") as f:
                f.write(line)
                f.flush()

    def handle(conn):
        raw = _read_line(conn)
        try:
            conn.close()
        except OSError:
            pass
        line = raw.split(b"\n", 1)[0].strip()
        if not line:
            return
        try:
            msg = json.loads(line.decode("utf-8", "replace"))
        except Exception:
            return
        if msg.get("type") != "user":
            return
        text = _extract_text((msg.get("message") or {}).get("content"))
        if not text:
            return
        append({"ts": time.time(), "from": _addr_from(msg.get("from")) or "",
                "text": text})

    def replanter():
        while not stop.wait(3):
            plant()

    open(opts.mailbox, "a").close()  # ensure the file exists
    plant()
    threading.Thread(target=replanter, daemon=True).start()
    signal.signal(signal.SIGTERM, cleanup)
    signal.signal(signal.SIGINT, cleanup)
    sys.stderr.write("cc_peer mailbox %s (pid %d); sidecar=%s mailbox=%s\n"
                     % (opts.name, pid, sidecar, opts.mailbox))
    while True:
        try:
            conn, _ = srv.accept()
        except KeyboardInterrupt:
            cleanup()
        except OSError:
            continue
        threading.Thread(target=handle, args=(conn,), daemon=True).start()


def respond(opts):
    """Listen on a socket; on the first real inbound message, reply to its
    `from` address with a fixed text, then exit. A cooperating-peer stand-in:
    it lets a cross-device reply round-trip be tested deterministically without
    a human-driven Claude session on the far end. Returns 0 on reply sent."""
    if os.path.exists(opts.socket):
        os.unlink(opts.socket)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(opts.socket)
    os.chmod(opts.socket, 0o600)
    srv.listen(8)
    if not opts.loop:
        srv.settimeout(opts.timeout)
    try:
        while True:
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                sys.stderr.write("respond: timed out\n")
                return 2
            raw = _read_line(conn)
            try:
                conn.close()
            except OSError:
                pass
            line = raw.split(b"\n", 1)[0].strip()
            if not line:
                continue
            try:
                msg = json.loads(line.decode("utf-8", "replace"))
            except Exception:
                continue
            if msg.get("type") != "user":
                continue
            sender = _addr_from(msg.get("from"))
            if not sender:
                continue
            # Echo the request body back with the fixed reply, so a caller can
            # see its message made the full round trip.
            asked = _extract_text((msg.get("message") or {}).get("content"))
            asked = _WRAP_INNER(asked)
            body = opts.reply
            if asked:
                body = "%s (you said: %s)" % (opts.reply, asked[:200])
            try:
                deliver(sender, body, opts.socket, from_name=opts.name)
                sys.stderr.write("respond: replied to %s\n" % sender)
                if not opts.loop:
                    return 0
            except Exception as e:
                sys.stderr.write("respond: reply failed: %s\n" % e)
                if not opts.loop:
                    return 1
    finally:
        try:
            os.unlink(opts.socket)
        except OSError:
            pass


import re as _re
_WRAP_RE = _re.compile(r"<cross-session-message[^>]*>\s*(.*?)\s*</cross-session-message>",
                       _re.DOTALL)


def _WRAP_INNER(text):
    m = _WRAP_RE.search(text or "")
    return m.group(1).strip() if m else (text or "").strip()


def main():
    ap = argparse.ArgumentParser(prog="cc_peer")
    sub = ap.add_subparsers(dest="role", required=True)

    s = sub.add_parser("serve")
    s.add_argument("--socket", required=True)
    s.add_argument("--device", required=True)
    s.add_argument("--name", required=True)
    s.add_argument("--communicate", required=True, help="path to the communicate CLI")
    s.add_argument("--sessions-dir", dest="sessions_dir", default="",
                   help="Claude sessions dir; the daemon plants+maintains its own sidecar here")
    s.add_argument("--dir", default="")
    s.add_argument("--auto", action="store_true")
    s.add_argument("--timeout", type=int, default=180)

    d = sub.add_parser("send")
    d.add_argument("--to", required=True, help="target socket path")
    d.add_argument("--text", required=True)
    d.add_argument("--from", dest="from_sock", required=True, help="our socket path (reply address)")
    d.add_argument("--name", default=None, help="from-name attribution to render")

    r = sub.add_parser("recv")
    r.add_argument("--socket", required=True)
    r.add_argument("--timeout", type=float, default=60)
    r.add_argument("--unwrap", action="store_true",
                   help="strip a LEADING cross-session-message wrapper; attribution goes to stderr")
    r.add_argument("--plant-name", default="",
                   help="plant a numeric sidecar with this name while listening (with --sessions-dir)")
    r.add_argument("--sessions-dir", dest="sessions_dir", default="")

    mb = sub.add_parser("mailbox")
    mb.add_argument("--socket", required=True)
    mb.add_argument("--name", default="orchestrator")
    mb.add_argument("--sessions-dir", dest="sessions_dir", required=True)
    mb.add_argument("--mailbox", required=True, help="JSONL file inbound messages are appended to")

    rp = sub.add_parser("respond")
    rp.add_argument("--socket", required=True)
    rp.add_argument("--reply", required=True, help="text to reply to the sender with")
    rp.add_argument("--name", default="responder", help="from-name attribution on the reply")
    rp.add_argument("--timeout", type=float, default=60)
    rp.add_argument("--loop", action="store_true", help="keep answering instead of exiting after one reply")

    a = ap.parse_args()
    if a.role == "serve":
        serve(a)
    elif a.role == "recv":
        sys.exit(recv(a))
    elif a.role == "mailbox":
        mailbox(a)
    elif a.role == "respond":
        sys.exit(respond(a))
    else:
        deliver(a.to, a.text, a.from_sock, from_name=a.name)


if __name__ == "__main__":
    main()

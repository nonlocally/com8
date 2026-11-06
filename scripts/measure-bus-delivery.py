#!/usr/bin/env python3
"""Measure the outbound worker's delivery cadence to one service recipient.

A local TLS hub, a fake service endpoint and this checkout's real outbound
worker (started by `communicate bus register-service`) run in isolated
temporary state. N messages are sent to the service within a window. The
harness records every hub operation the worker makes (operation, timing, the
lease size of each device poll), every bridge call the fake service receives
and the hub's own receipts, then reports the worker's poll cadence, lease
sizes, round trips per message, the delivery rate at the service and the
per-message latencies.

No external network, live agent, user configuration or credential is used.
The fake service answers at once: in `immediate` mode `deliver` returns the
terminal reply; in `poll` mode (the dispatch-service pattern) `deliver` returns
`{"pending": true}` and the first `poll` returns the reply.

    python3 scripts/measure-bus-delivery.py --messages 100 --window 10 --mode poll --json

Stdlib only; `openssl` makes the fixture certificate.
"""
import argparse
import base64
import http.server
import json
import os
from pathlib import Path
import shutil
import ssl
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT / "lib"))
from bus_broker import Broker, handler_factory  # noqa: E402

CERT_CONFIG = """[req]
prompt = no
distinguished_name = subject
x509_extensions = extensions
[subject]
CN = localhost
[extensions]
basicConstraints = critical,CA:TRUE
keyUsage = critical,digitalSignature,keyEncipherment,keyCertSign
extendedKeyUsage = serverAuth
subjectAltName = DNS:localhost
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid:always
"""


def make_certificate(temp):
    """One isolated self-signed CA for localhost; never the runner's openssl.cnf."""
    cert, key, config = temp / "cert.pem", temp / "key.pem", temp / "openssl.cnf"
    config.write_text(CERT_CONFIG)
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key),
                    "-out", str(cert), "-days", "1", "-sha256", "-config", str(config)],
                   capture_output=True, check=True)
    return cert, key


class Recorder:
    """Thread-safe event log shared by the hub wrapper and the fake service."""

    def __init__(self):
        self.lock = threading.Lock()
        self.hub = []
        self.bridge = []

    def record(self, kind, event):
        with self.lock:
            getattr(self, kind).append(event)

    def snapshot(self, kind):
        with self.lock:
            return list(getattr(self, kind))


def instrument(broker, recorder):
    """Record each broker operation the worker performs, with its timing and result size."""
    original = broker.handle

    def handle(token, request, **context):
        started = time.time()
        result = original(token, request, **context)
        event = {"at": started, "elapsed": time.time() - started, "op": request.get("op") if isinstance(request, dict) else None,
                 "ok": bool(result.get("ok"))}
        if event["op"] == "poll_device":
            event["agents"] = len(request.get("agents") or [])
            if "batch" in request:
                event["batch"] = request["batch"]
            event["messages"] = len(result.get("messages") or [])
            event["ids"] = [item.get("id") for item in result.get("messages") or []]
        elif event["op"] == "ack":
            event["id"], event["status"] = request.get("id"), request.get("status")
        elif event["op"] == "reply":
            event["reply_to"], event["id"] = request.get("id"), result.get("id")
        elif event["op"] == "heartbeat":
            event["agents"] = len(request.get("agents") or [])
        recorder.record("hub", event)
        return result

    broker.handle = handle


def tls_server(cert, key, handler):
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def start_hub(temp, cert, key, recorder):
    broker = Broker(temp / "hub")
    instrument(broker, recorder)
    server, thread = tls_server(cert, key, handler_factory(broker))
    return broker, server, thread, "https://localhost:%d" % server.server_port


def start_service(cert, key, token, mode, recorder):
    """A fake service answering every bridge call at once, per mode."""
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            arrived = time.time()
            if self.headers.get("Authorization") != "Bearer " + token:
                self.send_error(401)
                return
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            envelope = payload.get("envelope") or {}
            if mode == "immediate" or payload.get("op") == "poll":
                answer = {"pending": False, "reply": "measured reply to " + str(envelope.get("id"))}
            else:
                answer = {"pending": True}
            recorder.record("bridge", {"at": arrived, "op": payload.get("op"), "id": envelope.get("id"),
                                       "answer": "reply" if not answer["pending"] else "pending"})
            body = json.dumps(answer).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server, thread = tls_server(cert, key, Handler)
    return server, thread, "https://localhost:%d/bridge" % server.server_port


def client_environment(temp, cert):
    """An isolated HOME and state for the CLI and the worker it starts; no Tailscale or sessions."""
    home = temp / "home"
    home.mkdir()
    env = dict(os.environ, HOME=str(home), COMM_STATE=str(home / "state"), CLAUDE_CONFIG_DIR=str(home / ".claude"),
               CODEX_HOME=str(home / ".codex"), SSL_CERT_FILE=str(cert), COMM_BUS_PORT="0", PYTHONDONTWRITEBYTECODE="1")
    shadow = home / "bin"
    shadow.mkdir()
    tailscale = shadow / "tailscale"
    tailscale.write_text("#!" + sys.executable + "\nimport json\nprint(json.dumps({'Self': {'HostName': 'measure', "
                         "'DNSName': 'measure.fixture.ts.net.'}}))\n")
    tailscale.chmod(0o755)
    env["PATH"] = str(shadow) + os.pathsep + env["PATH"]
    for key in ("CLAUDE_CODE_MESSAGING_SOCKET", "CODEX_THREAD_ID", "CODEX_SESSION_ID", "COMM_CODEX_INDEX"):
        env.pop(key, None)
    return env


def cli(env, *args):
    proc = subprocess.run([str(ROOT / "bin/communicate"), "bus", *args], env=env, text=True,
                          capture_output=True, timeout=60)
    if proc.returncode:
        raise RuntimeError("communicate bus %s failed: %s%s" % (" ".join(args), proc.stdout, proc.stderr))
    try:
        return json.loads(proc.stdout)
    except ValueError:
        return proc.stdout.strip()


def invitation(broker, url, user):
    invite = broker.handle(broker.admin_token, {"op": "invite", "bus": "general", "ttl": 300, "user": user})
    if not invite.get("ok"):
        raise RuntimeError("invite failed: %r" % (invite,))
    raw = json.dumps({"url": url, "invite": invite["invite"]}).encode()
    return "commbus1." + base64.urlsafe_b64encode(raw).decode().rstrip("=")


def quantiles(values):
    if not values:
        return {"count": 0}
    ordered = sorted(values)
    def at(fraction):
        return ordered[min(len(ordered) - 1, int(round(fraction * (len(ordered) - 1))))]
    return {"count": len(ordered), "min": ordered[0], "p50": at(.5), "p90": at(.9), "max": ordered[-1],
            "mean": statistics.fmean(ordered)}


def rounded(value, digits=3):
    if isinstance(value, float):
        return round(value, digits)
    if isinstance(value, dict):
        return {key: rounded(item, digits) for key, item in value.items()}
    if isinstance(value, list):
        return [rounded(item, digits) for item in value]
    return value


def analyze(parameters, sends, recorder, broker, service_id, worker_state):
    hub, bridge = recorder.snapshot("hub"), recorder.snapshot("bridge")
    sent_at = {item["id"]: item["at"] for item in sends}
    polls = [event for event in hub if event["op"] == "poll_device"]
    acks = [event for event in hub if event["op"] == "ack"]
    replies = [event for event in hub if event["op"] == "reply" and event["ok"]]
    heartbeats = [event for event in hub if event["op"] == "heartbeat"]
    intervals = [later["at"] - earlier["at"] for earlier, later in zip(polls, polls[1:])]
    delivers = [event for event in bridge if event["op"] == "deliver"]
    bridge_polls = [event for event in bridge if event["op"] == "poll"]
    first_deliver = {}
    for event in delivers:
        first_deliver.setdefault(event["id"], event["at"])
    replied_at = {}
    with broker._connect() as db:
        for row in db.execute("SELECT in_reply_to,created_at FROM messages WHERE sender=?", (service_id,)):
            replied_at.setdefault(row["in_reply_to"], row["created_at"])
        receipts = {row["id"]: row["status"] for row in db.execute(
            "SELECT id,status FROM messages WHERE target=?", (service_id,))}
    terminal = {}
    for mid in sent_at:
        terminal[receipts.get(mid, "missing")] = terminal.get(receipts.get(mid, "missing"), 0) + 1
    send_to_deliver = [first_deliver[mid] - sent_at[mid] for mid in sent_at if mid in first_deliver]
    deliver_to_reply = [replied_at[mid] - first_deliver[mid] for mid in first_deliver if mid in replied_at]
    send_to_reply = [replied_at[mid] - sent_at[mid] for mid in sent_at if mid in replied_at]
    delivered_ids = [event["id"] for event in delivers]
    span = (max(first_deliver.values()) - min(first_deliver.values())) if len(first_deliver) > 1 else 0.0
    wall = (max(first_deliver.values()) - min(sent_at.values())) if first_deliver else 0.0
    sends_span = (max(sent_at.values()) - min(sent_at.values())) if len(sent_at) > 1 else 0.0
    count = len(sent_at)
    lease_sizes = [event["messages"] for event in polls]
    report = {
        "parameters": parameters,
        "hub": {
            "poll_device": {"count": len(polls), "empty": sum(1 for size in lease_sizes if size == 0),
                            "interval_s": quantiles(intervals), "lease_size": quantiles(lease_sizes),
                            "elapsed_s": quantiles([event["elapsed"] for event in polls]),
                            "batch_requested": any("batch" in event for event in polls)},
            "ack": {"count": len(acks), "elapsed_s": quantiles([event["elapsed"] for event in acks]),
                    "statuses": {status: sum(1 for event in acks if event["status"] == status)
                                 for status in sorted({event["status"] for event in acks})}},
            "reply": {"count": len(replies), "elapsed_s": quantiles([event["elapsed"] for event in replies])},
            "heartbeat": {"count": len(heartbeats)},
            "operations_per_message": (len(polls) + len(acks) + len(replies)) / count if count else None,
        },
        "service": {
            "deliver": len(delivers), "poll": len(bridge_polls), "distinct_delivered": len(first_deliver),
            "duplicate_delivers": len(delivered_ids) - len(set(delivered_ids)),
            "operations_per_message": (len(delivers) + len(bridge_polls)) / count if count else None,
            "deliver_span_s": span, "rate_per_minute": (len(first_deliver) - 1) / span * 60 if span > 0 else None,
            "wall_rate_per_minute": len(first_deliver) / wall * 60 if wall > 0 else None,
            "first_deliver_after_first_send_s": (min(first_deliver.values()) - min(sent_at.values())) if first_deliver else None,
        },
        "messages": {
            "sent": count, "send_span_s": sends_span, "terminal": terminal, "replied": len(send_to_reply),
            "latency_s": {"send_to_deliver": quantiles(send_to_deliver), "deliver_to_reply": quantiles(deliver_to_reply),
                          "send_to_reply": quantiles(send_to_reply)},
        },
        "worker": {key: worker_state.get(key) for key in ("state", "adapters", "errors", "runtime")},
    }
    report["timeline"] = {"sends": sends, "hub": hub, "bridge": bridge}
    return report


def measure(messages=100, window=10.0, mode="poll", timeout=900.0, state=None, keep=False, progress=None):
    """Run one measurement; returns the report dictionary (timeline included)."""
    if mode not in ("immediate", "poll"):
        raise ValueError("mode must be immediate or poll")
    temp = Path(tempfile.mkdtemp(prefix="bus-measure-", dir=state))
    recorder = Recorder()
    servers = []
    env = None
    try:
        cert, key = make_certificate(temp)
        broker, hub_server, hub_thread, hub_url = start_hub(temp, cert, key, recorder)
        servers.append((hub_server, hub_thread))
        token = "measure-bridge-token-" + uuid.uuid4().hex + uuid.uuid4().hex
        service_server, service_thread, endpoint = start_service(cert, key, token, mode, recorder)
        servers.append((service_server, service_thread))
        env = client_environment(temp, cert)
        cli(env, "connect", invitation(broker, hub_url, "measure-account"))
        token_file = temp / "service.token"
        token_file.write_text(token)
        token_file.chmod(0o600)
        service_id = str(uuid.uuid4())
        registered = cli(env, "register-service", "measured-service", "--service-id", service_id,
                         "--endpoint", endpoint, "--token-file", str(token_file), "--contract-version", "2", "--json")
        recipient = registered["id"]
        sender = broker.handle(broker.admin_token, {"op": "register", "session_key": "measure-sender",
                                                    "name": "measure-sender", "kind": "codex"})
        if not sender.get("ok"):
            raise RuntimeError("sender registration failed: %r" % (sender,))
        sender = sender["id"]
        sends = []
        pause = window / messages if messages > 1 else 0.0
        started = time.time()
        for index in range(messages):
            due = started + index * pause
            delay = due - time.time()
            if delay > 0:
                time.sleep(delay)
            result = broker.handle(broker.admin_token, {"op": "send", "sender": sender, "target": recipient,
                                                        "bus": "general", "message": "measurement %d of %d" % (index + 1, messages)})
            if not result.get("ok"):
                raise RuntimeError("send %d failed: %r" % (index + 1, result))
            sends.append({"id": result["id"], "at": time.time(), "index": index})
        deadline = time.monotonic() + timeout
        complete = False
        last_report = 0.0
        while time.monotonic() < deadline:
            bridge = recorder.snapshot("bridge")
            delivered = {event["id"] for event in bridge if event["op"] == "deliver"}
            answered = {event["id"] for event in bridge if event["answer"] == "reply"}
            replied = sum(1 for event in recorder.snapshot("hub") if event["op"] == "reply" and event["ok"])
            if progress and time.monotonic() - last_report >= 10:
                progress("delivered %d/%d, answered %d, replies at hub %d" % (len(delivered), messages, len(answered), replied))
                last_report = time.monotonic()
            if len(delivered) >= messages and len(answered) >= messages and replied >= messages:
                complete = True
                break
            time.sleep(.25)
        worker_state = {}
        state_dir = Path(env["COMM_STATE"]) / "bus"
        try:
            worker_state = json.loads((state_dir / "worker.json").read_text())
        except (OSError, ValueError):
            pass
        parameters = {"messages": messages, "window_s": window, "mode": mode, "timeout_s": timeout, "complete": complete,
                      "hub": hub_url, "endpoint": endpoint, "state": str(temp)}
        report = analyze(parameters, sends, recorder, broker, recipient, worker_state)
        try:
            report["worker"]["log_tail"] = (state_dir / "worker.log").read_text()[-2000:]
        except OSError:
            report["worker"]["log_tail"] = ""
        return report
    finally:
        if env is not None:
            subprocess.run([str(ROOT / "bin/communicate"), "bus", "stop"], env=env, capture_output=True, timeout=60)
        for server, thread in servers:
            server.shutdown()
            server.server_close()
            thread.join(2)
        if not keep:
            shutil.rmtree(temp, ignore_errors=True)


def summarize(report):
    hub, service, messages = report["hub"], report["service"], report["messages"]
    poll = hub["poll_device"]
    lines = [
        "messages: %d sent over %.1f s (%s mode); complete=%s" % (
            messages["sent"], messages["send_span_s"], report["parameters"]["mode"], report["parameters"]["complete"]),
        "hub poll_device: %d calls, %d empty; interval p50 %.2f s, p90 %.2f s, max %.2f s; lease size max %s, mean %.2f" % (
            poll["count"], poll["empty"], poll["interval_s"].get("p50", 0), poll["interval_s"].get("p90", 0),
            poll["interval_s"].get("max", 0), poll["lease_size"].get("max"), poll["lease_size"].get("mean", 0)),
        "hub ack: %d (%s); reply: %d; heartbeat: %d; hub operations per message: %.2f" % (
            hub["ack"]["count"], hub["ack"]["statuses"], hub["reply"]["count"], hub["heartbeat"]["count"],
            hub["operations_per_message"] or 0),
        "service: %d deliver (%d distinct, %d duplicates), %d poll; %.2f bridge calls per message" % (
            service["deliver"], service["distinct_delivered"], service["duplicate_delivers"], service["poll"],
            service["operations_per_message"] or 0),
        "delivery rate at the service: %s per minute over %.1f s (wall rate %s per minute; first delivery %.2f s after the first send)" % (
            "%.1f" % service["rate_per_minute"] if service["rate_per_minute"] else "n/a", service["deliver_span_s"],
            "%.1f" % service["wall_rate_per_minute"] if service["wall_rate_per_minute"] else "n/a",
            service["first_deliver_after_first_send_s"] or 0),
        "latency send->deliver p50 %.2f s, p90 %.2f s, max %.2f s; deliver->reply p50 %.2f s; send->reply p50 %.2f s, max %.2f s" % (
            messages["latency_s"]["send_to_deliver"].get("p50", 0), messages["latency_s"]["send_to_deliver"].get("p90", 0),
            messages["latency_s"]["send_to_deliver"].get("max", 0), messages["latency_s"]["deliver_to_reply"].get("p50", 0),
            messages["latency_s"]["send_to_reply"].get("p50", 0), messages["latency_s"]["send_to_reply"].get("max", 0)),
        "receipts: %s; worker errors: %s" % (messages["terminal"], report["worker"].get("errors")),
    ]
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--messages", type=int, default=100)
    parser.add_argument("--window", type=float, default=10.0, help="seconds over which the messages are sent")
    parser.add_argument("--mode", choices=("immediate", "poll"), default="poll")
    parser.add_argument("--timeout", type=float, default=900.0, help="seconds to wait for every reply")
    parser.add_argument("--state", help="parent directory for the temporary fixture state")
    parser.add_argument("--keep", action="store_true", help="retain the fixture state for inspection")
    parser.add_argument("--json", action="store_true", help="print the full report, timeline included")
    parser.add_argument("--out", help="write the full JSON report to this file")
    args = parser.parse_args(argv)
    report = measure(args.messages, args.window, args.mode, args.timeout, args.state, args.keep,
                     progress=lambda text: print(text, file=sys.stderr, flush=True))
    if args.out:
        Path(args.out).write_text(json.dumps(rounded(report), indent=1))
    if args.json:
        print(json.dumps(rounded(report), indent=1))
    else:
        print(summarize(report))
    return 0 if report["parameters"]["complete"] else 1


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Keep the gateway key away from code nobody has reviewed: a relay on 127.0.0.1 that adds it.

The self-heal run lets a model with a shell edit the repository, then runs what it wrote (the tests and
a rebuild). On GitHub both happen as another user (lib/sandbox.py) that is given stand-ins instead of the
key: ICA_BASE_URL points at this relay and ICA_API_KEY holds a token made for this run. A request
carrying the token is sent on with the real key in its place; a request without it, or for a path no
model call needs, is refused. The real key is also taken out of every answer: the ICA gateway's 401
echoes the key it refused. (Bob Shell cannot work through a relay; lib/bob.py keeps its key away from
any command instead.)

    python scripts/key_relay.py --env-out FILE

serves until it is stopped, after writing the stand-ins to FILE (NAME=value, one per line, mode 600).
Without the key in its own environment there is nothing to relay and FILE stays empty.
"""
from __future__ import annotations

import argparse
import http.client
import os
import secrets
import signal
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.common import log  # noqa: E402

# name: (real address variable, its default, key variable, paths a model call needs; "/x/" = under /x/)
ROUTES = {
    "ica": ("ICA_BASE_URL", "", "ICA_API_KEY", ("/chat/completions", "/models")),
}
HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer",
              "transfer-encoding", "upgrade", "host", "content-length", "accept-encoding"}
TIMEOUT = 900   # a model's answer can take minutes


class Relay:
    """One route: a local address and token standing in for `upstream` and `key`."""

    def __init__(self, name: str, upstream: str, key: str, paths: tuple[str, ...]):
        self.name, self.key, self.paths = name, key, paths
        self.upstream = urllib.parse.urlsplit(upstream.rstrip("/"))
        self.token = f"relay-{name}-{secrets.token_urlsafe(24)}"
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.server.daemon_threads = True
        self.server.relay = self

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"

    def allowed(self, path: str) -> bool:
        return any(path == p or (p.endswith("/") and path.startswith(p)) for p in self.paths)

    def scrub(self, data: bytes) -> bytes:
        return data.replace(self.key.encode(), f"[{self.name} key removed]".encode())

    def connect(self) -> http.client.HTTPConnection:
        kind = http.client.HTTPSConnection if self.upstream.scheme == "https" else http.client.HTTPConnection
        return kind(self.upstream.hostname, self.upstream.port, timeout=TIMEOUT)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        self._relay()

    def do_POST(self):
        self._relay()

    def log_message(self, format, *args):   # noqa: A002 — the stand-in token must not reach the log either
        pass

    def _relay(self):
        relay: Relay = self.server.relay
        if not relay.allowed(urllib.parse.urlsplit(self.path).path):
            return self._answer(404, b"not a model call")
        headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP_BY_HOP}
        if not any(relay.token in v for v in headers.values()):
            return self._answer(403, b"this relay's token is missing")
        headers = {k: v.replace(relay.token, relay.key) for k, v in headers.items()}
        headers["Host"] = relay.upstream.netloc
        headers["Accept-Encoding"] = "identity"   # answers are scrubbed, so they must be plain
        body = self._body()
        conn = relay.connect()
        try:
            conn.request(self.command, relay.upstream.path + self.path, body=body, headers=headers)
            resp = conn.getresponse()
        except OSError as exc:
            conn.close()
            return self._answer(502, f"{relay.name} unreachable: {type(exc).__name__}".encode())
        try:
            self.send_response(resp.status, resp.reason)
            for name, value in resp.getheaders():
                if name.lower() not in HOP_BY_HOP:
                    self.send_header(name, relay.scrub(value.encode("latin-1")).decode("latin-1"))
            if resp.status >= 400:   # an error is read whole: the key it echoes may span two reads
                data = relay.scrub(resp.read())
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            else:   # streamed as it comes (a model's answer arrives piece by piece)
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                while chunk := resp.read1(65536):
                    chunk = relay.scrub(chunk)
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                self.wfile.write(b"0\r\n\r\n")
        except OSError:
            pass   # the caller went away
        finally:
            conn.close()
            self.close_connection = True

    def _body(self) -> bytes | None:
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            data = b""
            while size := int(self.rfile.readline().split(b";")[0].strip() or b"0", 16):
                data += self.rfile.read(size)
                self.rfile.readline()
            while self.rfile.readline() not in (b"\r\n", b"\n", b""):
                pass
            return data
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else None

    def _answer(self, status: int, text: bytes):
        self.send_response(status)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(text)))
        self.end_headers()
        self.wfile.write(text)
        self.close_connection = True


def start(env=None) -> list[Relay]:
    """A running relay for each route whose key is set in `env`."""
    env = os.environ if env is None else env
    relays = []
    for name, (url_var, default, key_var, paths) in ROUTES.items():
        upstream, key = env.get(url_var) or default, env.get(key_var, "")
        if upstream and key:
            relay = Relay(name, upstream, key, paths)
            threading.Thread(target=relay.server.serve_forever, daemon=True).start()
            relays.append(relay)
    return relays


def stand_ins(relays: list[Relay]) -> dict[str, str]:
    """What the sandbox gets in place of each relayed key and address."""
    out = {}
    for relay in relays:
        url_var, _, key_var, _ = ROUTES[relay.name]
        out[url_var], out[key_var] = relay.url, relay.token
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--env-out", required=True, help="where to write the stand-ins (NAME=value lines)")
    args = ap.parse_args()
    relays = start()
    out, part = Path(args.env_out), Path(args.env_out + ".part")
    part.touch(mode=0o600)
    part.chmod(0o600)
    part.write_text("".join(f"{k}={v}\n" for k, v in stand_ins(relays).items()), encoding="utf-8")
    part.replace(out)   # whoever waits for the file finds it whole
    log("key relay: " + (", ".join(f"{r.name} on {r.url}" for r in relays) or "no key set, nothing to relay"))
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    stop.wait()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

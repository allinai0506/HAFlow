"""herdr/egress_proxy.py

Controlled outbound egress proxy for sandboxed AI reviewer subprocesses.
Enforces Section IV security boundary & PR #185 P1 resolution:
- Intercepts outbound network requests from AI reviewer subprocess.
- Strictly validates destination host against whitelist (e.g. generativelanguage.googleapis.com).
- Rejects non-whitelisted destinations (preventing credential exfiltration to arbitrary HTTPS servers).
- Forbids open forwarding: only approved LLM API destinations are reached.
- Chains through host upstream proxy if present in host environment.
"""

import fnmatch
import logging
import os
import re
import select
import socket
import socketserver
import threading
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

logger = logging.getLogger("herdr.egress_proxy")

DEFAULT_ALLOWED_DESTINATIONS = [
    "generativelanguage.googleapis.com",
]


def is_destination_allowed(host: str, port: int, allowed_patterns: List[str]) -> bool:
    """Verify destination host and port against strict whitelist.

    Only port 443 (HTTPS) to whitelisted hostnames is permitted.
    """
    if port != 443:
        return False

    host_clean = host.strip().lower()
    for pattern in allowed_patterns:
        pat_clean = pattern.strip().lower()
        if pat_clean.startswith("*."):
            suffix = pat_clean[1:]  # e.g. .googleapis.com
            if host_clean.endswith(suffix) or host_clean == pat_clean[2:]:
                return True
        elif fnmatch.fnmatch(host_clean, pat_clean):
            return True
        elif host_clean == pat_clean:
            return True
    return False


class ControlledEgressHandler(socketserver.BaseRequestHandler):
    """Handles HTTP CONNECT tunneling with strict destination host validation."""

    server: "ControlledEgressProxy"

    def handle(self):
        client = self.request
        client.settimeout(15.0)
        upstream = None
        try:
            req_data = b""
            while b"\r\n\r\n" not in req_data and b"\n\n" not in req_data:
                chunk = client.recv(4096)
                if not chunk:
                    break
                req_data += chunk
                if len(req_data) > 65536:
                    break

            if not req_data:
                return

            lines = req_data.decode("utf-8", errors="ignore").splitlines()
            if not lines:
                return

            first_line = lines[0].strip()
            parts = first_line.split()
            if len(parts) < 2:
                return

            method = parts[0].upper()
            target = parts[1]

            target_host = ""
            target_port = 443

            if method == "CONNECT":
                if ":" in target:
                    h, p = target.split(":", 1)
                    target_host = h
                    try:
                        target_port = int(p)
                    except ValueError:
                        target_port = 443
                else:
                    target_host = target
                    target_port = 443
            else:
                m = re.match(r"^https?://([^/:]+)(?::(\d+))?", target, re.IGNORECASE)
                if m:
                    target_host = m.group(1)
                    target_port = int(m.group(2)) if m.group(2) else 80
                else:
                    for line in lines[1:]:
                        if line.lower().startswith("host:"):
                            h_val = line.split(":", 1)[1].strip()
                            if ":" in h_val:
                                target_host, p_str = h_val.split(":", 1)
                                try:
                                    target_port = int(p_str)
                                except ValueError:
                                    target_port = 80
                            else:
                                target_host = h_val
                                target_port = 80
                            break

            # P1 Security Check: Validate destination host and port against strict whitelist
            if not is_destination_allowed(target_host, target_port, self.server.allowed_patterns):
                self.server.record_audit_event({
                    "timestamp": time.time(),
                    "action": "DENIED",
                    "host": target_host,
                    "port": target_port,
                    "client": self.client_address[0],
                })
                logger.warning(
                    "Controlled egress proxy blocked unauthorized destination: %s:%d (from %s)",
                    target_host,
                    target_port,
                    self.client_address[0],
                )
                err_resp = (
                    b"HTTP/1.1 403 Forbidden\r\n"
                    b"Content-Type: text/plain\r\n"
                    b"Connection: close\r\n\r\n"
                    b"Egress destination denied by security policy: host not permitted.\r\n"
                )
                try:
                    client.sendall(err_resp)
                except Exception:
                    pass
                return

            # Approved destination
            self.server.record_audit_event({
                "timestamp": time.time(),
                "action": "ALLOWED",
                "host": target_host,
                "port": target_port,
                "client": self.client_address[0],
            })

            upstream = self.server.connect_upstream(target_host, target_port)
            if upstream is None:
                client.sendall(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n")
                return

            if method == "CONNECT":
                client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            else:
                upstream.sendall(req_data)

            self._tunnel(client, upstream)
        except Exception as e:
            logger.debug("Proxy connection error: %s", e)
        finally:
            if upstream:
                try:
                    upstream.close()
                except Exception:
                    pass

    def _tunnel(self, sock1: socket.socket, sock2: socket.socket):
        sockets = [sock1, sock2]
        t0 = time.time()
        max_duration = 300.0
        while time.time() - t0 < max_duration:
            rlist, _, xlist = select.select(sockets, [], sockets, 2.0)
            if xlist:
                break
            if not rlist:
                continue
            closed = False
            for r in rlist:
                other = sock2 if r is sock1 else sock1
                try:
                    data = r.recv(16384)
                    if not data:
                        closed = True
                        break
                    other.sendall(data)
                except Exception:
                    closed = True
                    break
            if closed:
                break


class ControlledEgressProxy(socketserver.ThreadingTCPServer):
    """Threaded TCP server acting as an egress-filtering HTTP CONNECT proxy."""

    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self,
        bind_host: str = "127.0.0.1",
        bind_port: int = 0,
        allowed_patterns: Optional[List[str]] = None,
        upstream_proxy_url: Optional[str] = None,
    ):
        super().__init__((bind_host, bind_port), ControlledEgressHandler)
        self.allowed_patterns = list(allowed_patterns or DEFAULT_ALLOWED_DESTINATIONS)
        self.upstream_proxy_url = upstream_proxy_url
        self.audit_log: List[Dict[str, Any]] = []
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None

    def record_audit_event(self, event: Dict[str, Any]):
        with self._lock:
            self.audit_log.append(event)

    def get_audit_summary(self) -> Dict[str, Any]:
        with self._lock:
            total = len(self.audit_log)
            allowed = sum(1 for e in self.audit_log if e["action"] == "ALLOWED")
            denied = sum(1 for e in self.audit_log if e["action"] == "DENIED")
            return {
                "total": total,
                "allowed": allowed,
                "denied": denied,
                "events": list(self.audit_log),
            }

    def _connect_via_upstream(self, target_host: str, target_port: int) -> Optional[socket.socket]:
        if not self.upstream_proxy_url:
            return None
        parsed = urlparse(self.upstream_proxy_url)
        proxy_host = parsed.hostname or "127.0.0.1"
        proxy_port = parsed.port or 80
        s = socket.create_connection((proxy_host, proxy_port), timeout=10.0)
        connect_cmd = (
            f"CONNECT {target_host}:{target_port} HTTP/1.1\r\n"
            f"Host: {target_host}:{target_port}\r\n"
            f"Proxy-Connection: keep-alive\r\n\r\n"
        ).encode("utf-8")
        s.sendall(connect_cmd)
        resp = b""
        while b"\r\n\r\n" not in resp and b"\n\n" not in resp:
            chunk = s.recv(4096)
            if not chunk:
                s.close()
                return None
            resp += chunk
        status_line = resp.splitlines()[0]
        if b"200" not in status_line:
            s.close()
            return None
        return s

    def connect_upstream(self, target_host: str, target_port: int) -> Optional[socket.socket]:
        """Connect to target destination, tunneling through host upstream proxy if configured."""
        if self.upstream_proxy_url:
            try:
                s = self._connect_via_upstream(target_host, target_port)
                if s is not None:
                    return s
            except Exception as e:
                logger.debug("Upstream proxy attempt failed (%s): %s", self.upstream_proxy_url, e)

        # Fallback to direct connection
        return socket.create_connection((target_host, target_port), timeout=10.0)

    def start(self) -> int:
        """Start proxy server in a background thread and return assigned port."""
        self._thread = threading.Thread(target=self.serve_forever, daemon=True, name="ControlledEgressProxy")
        self._thread.start()
        return self.server_address[1]

    def stop(self):
        """Stop proxy server and wait for thread to terminate."""
        try:
            self.shutdown()
            self.server_close()
        except Exception:
            pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)

import json
import mimetypes
import os
import queue
import socketserver
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from . import paths
from .util import tail_text


class Hub(object):
    def __init__(self):
        self._lock = threading.Lock()
        self._subscribers = set()

    def subscribe(self):
        client = queue.Queue(maxsize=20)
        with self._lock:
            self._subscribers.add(client)
        return client

    def unsubscribe(self, client):
        with self._lock:
            self._subscribers.discard(client)

    def client_count(self):
        with self._lock:
            return len(self._subscribers)

    def publish(self, payload):
        with self._lock:
            clients = list(self._subscribers)
        for client in clients:
            try:
                client.put_nowait(payload)
            except queue.Full:
                pass


class MonitorHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def _json_bytes(obj):
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


class WebHandler(BaseHTTPRequestHandler):
    server_version = "TrainMon/%s" % paths.VERSION

    def log_message(self, fmt, *args):
        return

    def _send_json(self, obj, status=200):
        body = _json_bytes(obj)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _send_text(self, text, status=200, content_type="text/plain; charset=utf-8"):
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _serve_static(self, name):
        name = os.path.basename(unquote(name))
        path = os.path.realpath(os.path.join(paths.WEB_DIR, name))
        if not path.startswith(os.path.realpath(paths.WEB_DIR) + os.sep):
            return self._send_text("not found", 404)
        try:
            with open(path, "rb") as handle:
                body = handle.read()
        except (IOError, OSError):
            return self._send_text("not found", 404)
        content_type = mimetypes.guess_type(path)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in (
                "application/javascript", "application/json"):
            content_type += "; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _sse_send(self, payload):
        self.wfile.write(b"data: " + _json_bytes(payload) + b"\n\n")
        self.wfile.flush()

    def _serve_download(self, monitor):
        path = monitor.manifest.get("log")
        try:
            handle = open(path, "rb")
            size = os.path.getsize(path)
        except (IOError, OSError, TypeError):
            return self._send_text("log not found", 404)
        name = os.path.basename(path) or "train.log"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Disposition", 'attachment; filename="%s"' % name)
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            while True:
                chunk = handle.read(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            handle.close()

    def _serve_events(self):
        agent = self.server.agent
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-store")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.close_connection = True
        client = agent.hub.subscribe()
        try:
            self._sse_send({"type": "hello", "ts": time.time(),
                            "runs": agent.snapshots()})
            while not agent.stopping.is_set():
                try:
                    payload = client.get(timeout=15.0)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                self._sse_send(payload)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            agent.hub.unsubscribe(client)

    def do_GET(self):
        try:
            parsed = urlparse(self.path)
            route = unquote(parsed.path)
            query = parse_qs(parsed.query)
            agent = self.server.agent
            agent.touch()
            if route == "/api/health":
                return self._send_json({
                    "ok": True,
                    "version": paths.VERSION,
                    "pid": os.getpid(),
                    "port": agent.port,
                    "runs": len(agent.runs),
                    "uptime": time.time() - agent.started_at,
                })
            if route == "/api/events":
                return self._serve_events()
            if route == "/api/runs":
                return self._send_json({
                    "ok": True,
                    "ts": time.time(),
                    "runs": agent.snapshots(),
                })
            if route.startswith("/api/runs/"):
                parts = [part for part in route.split("/") if part]
                # ["api", "runs", run_id] or ["api", "runs", run_id, "log"]
                if len(parts) >= 3:
                    run_id = parts[2]
                    monitor = agent.get_run(run_id)
                    if monitor is None:
                        return self._send_json({"ok": False, "error": "run not found"}, 404)
                    if len(parts) == 3:
                        payload = monitor.snapshot(include_samples=True)
                        payload["ok"] = True
                        return self._send_json(payload)
                    if len(parts) == 4 and parts[3] == "log":
                        try:
                            max_bytes = int(query.get("max_bytes", ["65536"])[0])
                        except (TypeError, ValueError):
                            max_bytes = 65536
                        max_bytes = max(4096, min(max_bytes, 262144))
                        return self._send_json({
                            "ok": True,
                            "run_id": run_id,
                            "text": monitor.log_tail(max_bytes),
                            "ts": time.time(),
                        })
                    if len(parts) == 4 and parts[3] == "download":
                        return self._serve_download(monitor)
            if route in ("/", "/index.html"):
                return self._serve_static("index.html")
            if route.startswith("/static/"):
                return self._serve_static(route[len("/static/"):])
            if route == "/favicon.ico":
                self.send_response(204)
                self.end_headers()
                return
            return self._send_text("not found", 404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            try:
                self._send_json({"ok": False, "error": str(exc)}, 500)
            except Exception:
                pass

    def do_POST(self):
        try:
            parsed = urlparse(self.path)
            route = unquote(parsed.path)
            agent = self.server.agent
            agent.touch()
            if route == "/api/agent/stop":
                if self.headers.get("X-TrainMon-Confirm") != "stop":
                    return self._send_json(
                        {"ok": False, "error": "confirmation required"}, 400)
                agent.request_shutdown(delay=0.6)
                return self._send_json({
                    "ok": True,
                    "message": "monitoring stopped; training is not affected",
                })
            return self._send_json({"ok": False, "error": "not found"}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            try:
                self._send_json({"ok": False, "error": str(exc)}, 500)
            except Exception:
                pass

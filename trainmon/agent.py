import argparse
import json
import logging
import logging.handlers
import os
import signal
import sys
import threading
import time
import traceback
from collections import deque

from . import paths
from .agent_client import agent_status
from .eta import EtaEstimator
from .manifest import list_manifests, read_state, write_state
from .parser import LogParser
from .tailer import Tailer
from .util import (atomic_write_json, format_duration, hostname, pid_alive,
                   read_json, tail_text)
from .web import Hub, MonitorHTTPServer, WebHandler

LOG = logging.getLogger("trainmon.agent")


def _to_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class RunMonitor(object):
    def __init__(self, manifest, hub=None):
        self.manifest = dict(manifest)
        self.hub = hub
        self.run_id = self.manifest.get("run_id")
        self.lock = threading.RLock()
        self.parser = LogParser()
        self.eta = EtaEstimator()
        self.log_tailer = Tailer(self.manifest.get("log"),
                                 initial_bytes=262144, chunk_bytes=262144)
        self.metrics_tailer = Tailer(self.manifest.get("metrics"),
                                     initial_bytes=2097152, chunk_bytes=2097152)
        self.samples = deque(maxlen=800)
        self.last_lines = deque(maxlen=60)
        self.started_at = _to_float(self.manifest.get("started_at")) or time.time()
        self.ended_at = None
        self.last_update = self.started_at
        self.last_log_at = None
        self.last_progress_at = self.started_at
        self.last_persist = 0.0
        self.last_sample_persist = 0.0
        self.step = None
        self.total_steps = _to_int(self.manifest.get("total_steps"))
        self.epoch = None
        self.total_epochs = None
        self.loss = None
        self.lr = None
        self.metrics = {}
        self.rate = None
        self.eta_seconds = None
        self.eta_fast = None
        self.eta_slow = None
        self.percent = None
        self.confidence = "low"
        self.reported_rate = None
        self.has_metrics = False
        self.status = "waiting"
        self.exit_code = None
        self.error = None
        self.done_flag = False
        self.fail_flag = False
        self.last_line = None
        self.terminal_locked = False
        self._last_manifest_mtime = self.manifest.get("_mtime") or 0.0
        self._restore()

    def _restore(self):
        saved = read_state(self.run_id)
        if not isinstance(saved, dict):
            return
        for key in ("step", "total_steps", "epoch", "total_epochs", "loss",
                    "lr", "rate", "eta_seconds", "eta_fast", "eta_slow",
                    "percent", "confidence", "status",
                    "exit_code", "error", "started_at", "ended_at",
                    "last_update", "last_log_at", "last_progress_at",
                    "last_line", "metrics"):
            if key in saved and saved[key] is not None:
                setattr(self, key, saved[key])
        if isinstance(saved.get("samples"), list):
            for sample in saved["samples"][-800:]:
                if isinstance(sample, dict) and sample.get("step") is not None:
                    self.samples.append(sample)
        if isinstance(saved.get("last_lines"), list):
            for line in saved["last_lines"][-60:]:
                self.last_lines.append(line)
        if self.status in ("completed", "failed", "lost", "terminated"):
            if not pid_alive(self.manifest.get("pid")):
                self.terminal_locked = True
            else:
                self.status = "running"

    def _reload_manifest(self):
        path = self.manifest.get("_path")
        if not path:
            return
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            return
        if mtime <= self._last_manifest_mtime:
            return
        data = read_json(path, None)
        if not isinstance(data, dict):
            return
        data["_path"] = path
        data["_mtime"] = mtime
        self.manifest = data
        self._last_manifest_mtime = mtime
        if data.get("total_steps") is not None:
            value = _to_int(data.get("total_steps"))
            if value:
                self.total_steps = value
        if data.get("stall_seconds") is not None:
            pass
        if data.get("name"):
            pass

    def _stall_seconds(self):
        value = _to_float(self.manifest.get("stall_seconds"))
        return value if value and value > 0 else 300.0

    def _sample(self, ts, step, loss=None, lr=None):
        step = _to_int(step)
        if step is None:
            return
        if self.samples:
            last = self.samples[-1]
            last_ts = _to_float(last.get("ts"))
            if last_ts is not None and ts < last_ts:
                ts = last_ts
            last_step = _to_int(last.get("step"))
            if last_step is not None and step < last_step:
                gap = last_step - step
                threshold = max(20, int((self.total_steps or 0) * 0.05))
                if gap > threshold:
                    # A real restart/reset, not just metrics lagging the log.
                    self.samples.clear()
                    self.eta.reset()
                else:
                    return
            elif last_step == step:
                if loss is not None:
                    last["loss"] = loss
                if lr is not None:
                    last["lr"] = lr
                last["ts"] = max(_to_float(last.get("ts")) or ts, ts)
                self.last_progress_at = ts
                return
        sample = {"ts": float(ts), "step": step}
        if loss is not None:
            sample["loss"] = loss
        if lr is not None:
            sample["lr"] = lr
        self.samples.append(sample)
        while len(self.samples) > 800:
            self.samples.popleft()
        self.eta.add(ts, step)
        self.last_progress_at = ts

    def _apply_metrics(self, record):
        if not isinstance(record, dict):
            return False
        ts = _to_float(record.get("ts")) or time.time()
        changed = False
        record_step = _to_int(record.get("step"))
        stale = (record_step is not None and self.step is not None
                 and record_step < self.step)
        if record_step is not None:
            if self.step is None or record_step > self.step:
                self.step = record_step
                changed = True
            self._sample(ts, record_step, _to_float(record.get("loss")),
                         _to_float(record.get("lr")))
            self.has_metrics = True
        if record.get("total_steps") is not None:
            value = _to_int(record.get("total_steps"))
            if value:
                self.total_steps = value
        if record.get("epoch") is not None and not stale:
            self.epoch = _to_float(record.get("epoch"))
        if record.get("total_epochs") is not None:
            self.total_epochs = _to_int(record.get("total_epochs"))
        if record.get("loss") is not None and not stale:
            self.loss = _to_float(record.get("loss"))
            changed = True
        if record.get("lr") is not None and not stale:
            self.lr = _to_float(record.get("lr"))
        if isinstance(record.get("metrics"), dict):
            for key, value in record["metrics"].items():
                self.metrics[str(key)] = value
        self.last_update = ts
        return changed

    def _apply_log_update(self, updates, fallback_ts=None):
        if not updates:
            return False
        changed = False
        now = float(fallback_ts) if fallback_ts else time.time()
        sample_ts = _to_float(updates.get("log_ts")) or now
        if updates.get("total_steps"):
            if not self.total_steps:
                self.total_steps = _to_int(updates.get("total_steps"))
        if updates.get("total_epochs"):
            self.total_epochs = _to_int(updates.get("total_epochs"))
        log_step = _to_int(updates.get("step"))
        newer_step = (log_step is not None
                      and (self.step is None or log_step > self.step))
        if log_step is not None and newer_step:
            self.step = log_step
            self._sample(sample_ts, log_step, self.loss, self.lr)
            # Use poll time for stall detection even when log lines are
            # replayed from the past.
            self.last_progress_at = now
            changed = True
        if updates.get("loss") is not None and (newer_step or not self.has_metrics):
            self.loss = _to_float(updates.get("loss"))
            changed = True
        if updates.get("lr") is not None and (newer_step or not self.has_metrics):
            self.lr = _to_float(updates.get("lr"))
        if updates.get("epoch") is not None and (newer_step or not self.has_metrics):
            self.epoch = _to_float(updates.get("epoch"))
        if updates.get("reported_rate") is not None:
            self.reported_rate = _to_float(updates.get("reported_rate"))
        if updates.get("percent") is not None:
            self.percent = _to_float(updates.get("percent"))
        if updates.get("done"):
            self.done_flag = True
        if updates.get("fail"):
            self.fail_flag = True
            if not self.error:
                self.error = self.last_line
        self.last_update = now
        return changed

    def _update_status(self):
        now = time.time()
        if self.status in ("completed", "failed", "lost", "terminated"):
            return
        last_activity = max(self.last_progress_at or 0.0, self.last_log_at or 0.0)
        if self.step is None:
            if now - max(last_activity, self.started_at) > self._stall_seconds():
                self.status = "stalled"
            elif now - self.started_at < 12:
                self.status = "waiting"
            else:
                self.status = "running"
        elif now - last_activity > self._stall_seconds():
            self.status = "stalled"
        else:
            self.status = "running"

    def _update_eta(self):
        rate, eta, confidence, eta_fast, eta_slow = self.eta.estimate(
            self.total_steps, self.step)
        if rate is None and self.reported_rate and self.total_steps and self.step is not None:
            rate = self.reported_rate
            if rate > 0:
                eta = max(0, self.total_steps - self.step) / rate
                eta_fast = eta * 0.85
                eta_slow = eta * 1.25
                confidence = "medium"
        if eta is None and self.percent and 0 < self.percent < 100:
            elapsed = max(1.0, time.time() - self.started_at)
            eta = elapsed * (100.0 - self.percent) / self.percent
            eta_fast = eta * 0.7
            eta_slow = eta * 1.5
            confidence = "low"
        if eta is None and self.total_epochs and self.epoch and self.epoch > 0:
            fraction = min(1.0, max(0.0, float(self.epoch) / float(self.total_epochs)))
            if 0 < fraction < 1:
                elapsed = max(1.0, time.time() - self.started_at)
                eta = elapsed * (1.0 - fraction) / fraction
                eta_fast = eta * 0.7
                eta_slow = eta * 1.5
                confidence = "low"
        self.rate = rate
        self.eta_seconds = eta
        self.eta_fast = eta_fast
        self.eta_slow = eta_slow
        self.confidence = confidence

    def _finalize(self):
        if self.status in ("completed", "failed", "lost", "terminated"):
            return
        saved = read_state(self.run_id)
        if isinstance(saved, dict) and saved.get("status"):
            self._apply_state(saved)
            return
        text = "\n".join(list(self.last_lines)[-40:])
        if self.fail_flag or self.parser.is_fail(text):
            self.status = "failed"
            if not self.error:
                for line in reversed(self.last_lines):
                    if self.parser.is_fail(line):
                        self.error = line
                        break
                if not self.error:
                    self.error = self.last_line or "training process failed"
        elif self.done_flag or (self.total_steps and self.step is not None
                                and self.step >= self.total_steps):
            self.status = "completed"
        else:
            self.status = "lost"
            self.error = "training process exited before reporting completion"
        self.ended_at = time.time()
        if self.status == "completed":
            self.eta_seconds = self.eta_fast = self.eta_slow = 0
        else:
            self.eta_seconds = self.eta_fast = self.eta_slow = None
        self.last_update = self.ended_at
        self.terminal_locked = True
        self._persist(force=True)
        LOG.info("run %s finalized: %s", self.run_id, self.status)

    def _apply_state(self, state):
        self.status = state.get("status") or self.status
        self.exit_code = state.get("exit_code")
        self.error = state.get("error") or self.error
        self.ended_at = _to_float(state.get("ended_at")) or time.time()
        if state.get("step") is not None:
            self.step = _to_int(state.get("step"))
        if state.get("total_steps") is not None:
            self.total_steps = _to_int(state.get("total_steps"))
        if state.get("loss") is not None:
            self.loss = _to_float(state.get("loss"))
        self.last_update = self.ended_at
        if self.status == "completed":
            self.eta_seconds = self.eta_fast = self.eta_slow = 0
        else:
            self.eta_seconds = self.eta_fast = self.eta_slow = None
        self.terminal_locked = True
        self._persist(force=True)

    def _drain_once(self):
        changed = False
        poll_ts = time.time()
        # Metrics are exact and should win over rounded log values.
        for line in self.metrics_tailer.poll():
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if self._apply_metrics(record):
                changed = True
        for line in self.log_tailer.poll():
            self.last_line = line
            self.last_log_at = poll_ts
            self.last_lines.append(line)
            try:
                updates = self.parser.parse_line(line)
            except Exception:
                updates = None
            if self._apply_log_update(updates, poll_ts):
                changed = True
        return changed

    def poll(self):
        with self.lock:
            if self.terminal_locked:
                return False
            self._reload_manifest()
            state = read_state(self.run_id)
            if isinstance(state, dict) and state.get("status") in (
                    "completed", "failed", "lost", "terminated"):
                # Read the last bytes before locking the run, otherwise the
                # final step/log lines are lost when finish() races the poll.
                self._drain_once()
                self._apply_state(state)
                return True
            changed = self._drain_once()
            if not pid_alive(self.manifest.get("pid")):
                self._finalize()
                return True
            self._update_status()
            self._update_eta()
            if changed:
                self.last_update = time.time()
            self._persist()
            return changed

    def _state_dict(self, include_samples=True):
        state = {
            "run_id": self.run_id,
            "name": self.manifest.get("name"),
            "host": self.manifest.get("host"),
            "pid": self.manifest.get("pid"),
            "status": self.status,
            "step": self.step,
            "total_steps": self.total_steps,
            "epoch": self.epoch,
            "total_epochs": self.total_epochs,
            "loss": self.loss,
            "lr": self.lr,
            "metrics": self.metrics,
            "rate": self.rate,
            "eta_seconds": self.eta_seconds,
            "eta_fast": self.eta_fast,
            "eta_slow": self.eta_slow,
            "percent": self.percent,
            "confidence": self.confidence,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "last_update": self.last_update,
            "last_log_at": self.last_log_at,
            "last_progress_at": self.last_progress_at,
            "last_line": self.last_line,
            "exit_code": self.exit_code,
            "error": self.error,
            "log": self.manifest.get("log"),
            "last_lines": list(self.last_lines)[-20:],
        }
        if include_samples:
            state["samples"] = list(self.samples)
        return state

    def _persist(self, force=False):
        now = time.time()
        if not force and now - self.last_persist < 10.0:
            return
        self.last_persist = now
        include_samples = force or (now - self.last_sample_persist >= 300.0)
        if include_samples:
            self.last_sample_persist = now
        try:
            write_state(self.run_id, self._state_dict(include_samples))
        except Exception:
            LOG.exception("persist state failed for %s", self.run_id)

    def snapshot(self, include_samples=False):
        with self.lock:
            progress = None
            if self.total_steps and self.step is not None and self.total_steps > 0:
                progress = max(0.0, min(1.0, float(self.step) / float(self.total_steps)))
            if self.status == "completed":
                progress = 1.0
            base = self.manifest.get("agent_url")
            url = None
            if base:
                url = base.rstrip("/") + "/?run=" + self.run_id
            payload = {
                "run_id": self.run_id,
                "name": self.manifest.get("name") or self.run_id,
                "host": self.manifest.get("host") or hostname(),
                "pid": self.manifest.get("pid"),
                "status": self.status,
                "step": self.step,
                "total_steps": self.total_steps,
                "epoch": self.epoch,
                "total_epochs": self.total_epochs,
                "loss": self.loss,
                "lr": self.lr,
                "metrics": self.metrics,
                "rate": self.rate,
                "eta_seconds": self.eta_seconds,
                "eta_text": format_duration(self.eta_seconds),
                "eta_low": self.eta_fast,
                "eta_high": self.eta_slow,
                "eta_finish_at": ((time.time() + self.eta_seconds)
                                  if self.eta_seconds is not None
                                  and self.status in ("running", "waiting")
                                  else (self.ended_at if self.status == "completed" else None)),
                "confidence": self.confidence,
                "percent": self.percent,
                "progress": progress,
                "started_at": self.started_at,
                "ended_at": self.ended_at,
                "last_update": self.last_update,
                "last_log_at": self.last_log_at,
                "last_line": self.last_line,
                "exit_code": self.exit_code,
                "error": self.error,
                "log": self.manifest.get("log"),
                "url": url,
                "dropped_log_lines": self.log_tailer.dropped,
            }
            if include_samples:
                payload["samples"] = list(self.samples)
                payload["recent_lines"] = list(self.last_lines)[-20:]
            return payload

    def log_tail(self, max_bytes=65536):
        path = self.manifest.get("log")
        if not path:
            return ""
        return tail_text(path, max_bytes)


class Agent(object):
    def __init__(self, bind="127.0.0.1", preferred_port=8787, idle_exit=None):
        self.bind = bind
        self.preferred_port = int(preferred_port)
        self.port = None
        self.server = None
        self.runs = {}
        self.lock = threading.RLock()
        self.hub = Hub()
        self.stopping = threading.Event()
        self.started_at = time.time()
        self.last_client_at = time.time()
        self.http_thread = None
        if idle_exit is None:
            try:
                idle_exit = float(os.environ.get("TRAINMON_IDLE_EXIT", "0") or 0)
            except (TypeError, ValueError):
                idle_exit = 0.0
        self.idle_exit = max(0.0, float(idle_exit or 0))

    def touch(self):
        self.last_client_at = time.time()

    def request_shutdown(self, delay=0.5):
        def _stop():
            time.sleep(delay)
            self.stopping.set()
        thread = threading.Thread(target=_stop, name="trainmon-stop")
        thread.daemon = True
        thread.start()

    def _has_active_runs(self):
        with self.lock:
            runs = list(self.runs.values())
        for run in runs:
            if run.status in ("running", "waiting", "stalled"):
                return True
            # Keep the agent alive while the training PID is still present,
            # even if it is doing final evaluation after reporting completion.
            if pid_alive(run.manifest.get("pid")):
                return True
        return False

    def _should_idle_exit(self):
        if self.idle_exit <= 0:
            return False
        if self.hub.client_count() > 0:
            return False
        if self._has_active_runs():
            return False
        with self.lock:
            runs = list(self.runs.values())
        last_activity = self.last_client_at
        for run in runs:
            last_activity = max(last_activity, run.last_update or 0.0)
        return (time.time() - last_activity) > self.idle_exit

    def _write_agent_json(self):
        atomic_write_json(paths.AGENT_JSON, {
            "pid": os.getpid(),
            "port": self.port,
            "bind": self.bind,
            "url": "http://%s:%d/" % (self.bind, self.port),
            "started_at": self.started_at,
            "version": paths.VERSION,
            "host": hostname(),
        })

    def start(self):
        last_error = None
        for offset in range(0, 20):
            port = self.preferred_port + offset
            try:
                server = MonitorHTTPServer((self.bind, port), WebHandler)
            except OSError as exc:
                last_error = exc
                continue
            self.server = server
            self.port = port
            break
        if self.server is None:
            raise RuntimeError("cannot bind dashboard port: %s" % last_error)
        self.server.agent = self
        self._write_agent_json()
        self.http_thread = threading.Thread(
            target=self.server.serve_forever,
            kwargs={"poll_interval": 0.5},
            name="trainmon-http",
        )
        self.http_thread.daemon = True
        self.http_thread.start()
        LOG.info("agent listening on http://%s:%d (pid %d)",
                 self.bind, self.port, os.getpid())

    def discover(self):
        for manifest in list_manifests():
            run_id = manifest.get("run_id")
            if not run_id:
                continue
            with self.lock:
                if run_id in self.runs:
                    continue
                try:
                    self.runs[run_id] = RunMonitor(manifest, self.hub)
                    LOG.info("discovered run %s (%s)", run_id, manifest.get("log"))
                except Exception:
                    LOG.exception("cannot monitor run %s", run_id)

    def get_run(self, run_id):
        with self.lock:
            return self.runs.get(run_id)

    def snapshots(self):
        with self.lock:
            runs = list(self.runs.values())
        return [run.snapshot() for run in runs]

    def publish(self):
        self.hub.publish({
            "type": "snapshot",
            "ts": time.time(),
            "runs": self.snapshots(),
        })

    def tick(self):
        self.discover()
        with self.lock:
            runs = list(self.runs.values())
        for run in runs:
            try:
                run.poll()
            except Exception:
                LOG.exception("poll failed for %s", run.run_id)
        self.publish()

    def run_forever(self):
        self.start()
        if self.idle_exit > 0:
            LOG.info("idle exit enabled after %.0fs without runs/viewers", self.idle_exit)
        while not self.stopping.is_set():
            started = time.time()
            try:
                self.tick()
            except Exception:
                LOG.exception("agent tick failed")
            if self._should_idle_exit():
                LOG.info("no active runs or viewers for %.0fs; exiting to save resources",
                         self.idle_exit)
                self.stopping.set()
                break
            elapsed = time.time() - started
            self.stopping.wait(max(0.05, 1.0 - elapsed))
        self.shutdown()

    def shutdown(self):
        LOG.info("agent shutting down")
        with self.lock:
            runs = list(self.runs.values())
        for run in runs:
            try:
                run._persist(force=True)
            except Exception:
                pass
        if self.server is not None:
            try:
                self.server.shutdown()
                self.server.server_close()
            except Exception:
                pass
        info = read_json(paths.AGENT_JSON, None)
        if isinstance(info, dict) and info.get("pid") == os.getpid():
            try:
                os.unlink(paths.AGENT_JSON)
            except OSError:
                pass

    def handle_signal(self, signum, frame):
        self.stopping.set()


def _setup_logging(foreground=False):
    paths.ensure_layout()
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    handler = logging.handlers.RotatingFileHandler(
        paths.AGENT_LOG, maxBytes=2 * 1024 * 1024, backupCount=3,
        encoding="utf-8")
    handler.setFormatter(formatter)
    root.addHandler(handler)
    if foreground:
        stream = logging.StreamHandler()
        stream.setFormatter(formatter)
        root.addHandler(stream)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="trainmon.agent")
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--foreground", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--idle-exit", type=float, default=None,
                        help="exit after N seconds without active runs/viewers (0=never)")
    args = parser.parse_args(argv)
    _setup_logging(args.foreground)

    existing = agent_status()
    if existing and not args.force:
        print("trainmon agent already running: http://127.0.0.1:%d/" % existing["port"])
        return 0

    agent = Agent(bind=args.bind, preferred_port=args.port,
                  idle_exit=args.idle_exit)
    signal.signal(signal.SIGTERM, agent.handle_signal)
    signal.signal(signal.SIGINT, agent.handle_signal)
    try:
        agent.run_forever()
    except Exception:
        LOG.exception("agent crashed")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

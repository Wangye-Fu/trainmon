import atexit
import logging
import os
import sys
import time
from urllib.parse import quote

from . import paths
from .agent_client import dashboard_url, ensure_agent
from .manifest import run_id_for, write_manifest, write_state
from .reporter import Reporter, make_record
from .util import (clean_value, ensure_dir, env_bool, env_float, hostname,
                   sanitize_name)

VERSION = paths.VERSION
__version__ = VERSION

_HANDLES = []
_CURRENT = None
_HOOKS_INSTALLED = False
_LOG_HANDLERS = []


class RunHandle(object):
    def __init__(self, run_id=None, name=None, log=None, metrics=None,
                 total_steps=None, reporter=None, agent_info=None,
                 enabled=True):
        self.run_id = run_id
        self.name = name
        self.log = log
        self.metrics = metrics
        self.total_steps = total_steps
        self.reporter = reporter
        self.agent_info = agent_info
        self.enabled = enabled
        self.finished = False
        self.started_at = time.time()
        self.last_step = None
        self.last_loss = None

    @property
    def url(self):
        if not self.enabled or not self.agent_info:
            return None
        return dashboard_url(self.agent_info, self.run_id)

    def report(self, step=None, loss=None, lr=None, epoch=None,
               total_steps=None, total_epochs=None, metrics=None, **extra):
        if not self.enabled or not self.reporter or self.finished:
            return False
        if total_steps is not None:
            try:
                self.total_steps = int(total_steps)
            except (TypeError, ValueError):
                pass
        if step is not None:
            try:
                self.last_step = int(step)
            except (TypeError, ValueError):
                pass
        if loss is not None:
            try:
                self.last_loss = float(loss)
            except (TypeError, ValueError):
                pass
        merged = {}
        if metrics:
            merged.update(metrics)
        merged.update(extra)
        record = make_record(
            step=step,
            loss=loss,
            lr=lr,
            epoch=epoch,
            total_steps=total_steps if total_steps is not None else self.total_steps,
            total_epochs=total_epochs,
            metrics=merged or None,
        )
        return self.reporter.report(record)

    def finish(self, status="completed", exit_code=0, error=None):
        if not self.enabled or self.finished:
            return
        self.finished = True
        state = {
            "run_id": self.run_id,
            "status": status,
            "exit_code": exit_code,
            "error": error,
            "ended_at": time.time(),
            "step": self.last_step,
            "total_steps": self.total_steps,
            "loss": self.last_loss,
        }
        try:
            write_state(self.run_id, state)
        except Exception:
            pass
        if self.reporter:
            try:
                self.reporter.close()
            except Exception:
                pass
        try:
            sys.stderr.write("[trainmon] finished: %s (%s)\n" % (self.name, status))
            sys.stderr.flush()
        except Exception:
            pass


class _NoopHandle(object):
    enabled = False
    run_id = None
    name = None
    url = None

    def report(self, *args, **kwargs):
        return False

    def finish(self, *args, **kwargs):
        return None


def _rank():
    for key in ("RANK", "LOCAL_RANK", "SLURM_PROCID"):
        raw = os.environ.get(key)
        if raw is None:
            continue
        try:
            return int(raw)
        except (TypeError, ValueError):
            continue
    return 0


def _discover_log_file():
    candidates = []
    loggers = [logging.getLogger()]
    try:
        for name in logging.root.manager.loggerDict:
            loggers.append(logging.getLogger(name))
    except Exception:
        pass
    for logger in loggers:
        for handler in getattr(logger, "handlers", []) or []:
            if isinstance(handler, logging.FileHandler):
                filename = getattr(handler, "baseFilename", None)
                if filename:
                    candidates.append(os.path.abspath(filename))
    seen = set()
    best = None
    best_mtime = -1.0
    for path in candidates:
        if path in seen:
            continue
        seen.add(path)
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            mtime = 0.0
        if mtime > best_mtime:
            best = path
            best_mtime = mtime
    return best


def _add_monitor_log_handler(log_path):
    ensure_dir(os.path.dirname(log_path))
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setLevel(logging.INFO)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    if not root.handlers and root.level > logging.INFO:
        root.setLevel(logging.INFO)
    root.addHandler(handler)
    _LOG_HANDLERS.append(handler)
    return handler


def _install_exit_hooks():
    global _HOOKS_INSTALLED
    if _HOOKS_INSTALLED:
        return
    _HOOKS_INSTALLED = True

    def _atexit():
        for handle in list(_HANDLES):
            if handle.enabled and not handle.finished:
                handle.finish("completed", 0)

    old_excepthook = sys.excepthook

    def _excepthook(exc_type, exc, tb):
        for handle in list(_HANDLES):
            if handle.enabled and not handle.finished:
                handle.finish("failed", 1, "%s: %s" % (exc_type.__name__, exc))
        old_excepthook(exc_type, exc, tb)

    atexit.register(_atexit)
    sys.excepthook = _excepthook


def attach(name=None, run=None, log=None, metrics=None, total_steps=None,
           stall_seconds=None, enabled=True, start_agent=True,
           port=None):
    global _CURRENT
    if not enabled or env_bool("TRAINMON_DISABLE"):
        return _NoopHandle()
    if _rank() != 0:
        return _NoopHandle()

    paths.ensure_layout()
    display_name = sanitize_name(run or name or os.path.basename(sys.argv[0] or "run"))
    if not display_name:
        display_name = "run"
    run_id = run_id_for(display_name)
    run_dir = os.path.join(paths.RUNS_DIR, run_id)
    ensure_dir(run_dir)

    log_path = os.path.abspath(os.path.expanduser(log)) if log else _discover_log_file()
    if not log_path:
        log_path = os.path.join(run_dir, "train.log")
        _add_monitor_log_handler(log_path)

    metrics_path = (os.path.abspath(os.path.expanduser(metrics))
                    if metrics else os.path.join(run_dir, "metrics.jsonl"))
    ensure_dir(os.path.dirname(metrics_path))

    if total_steps is not None:
        try:
            total_steps = int(total_steps)
        except (TypeError, ValueError):
            total_steps = None
    if stall_seconds is None:
        stall_seconds = env_float("TRAINMON_STALL_SECONDS", 300.0)
    else:
        try:
            stall_seconds = float(stall_seconds)
        except (TypeError, ValueError):
            stall_seconds = 300.0

    manifest = {
        "run_id": run_id,
        "name": display_name,
        "host": hostname(),
        "pid": os.getpid(),
        "log": log_path,
        "metrics": metrics_path,
        "state": os.path.join(paths.STATE_DIR, run_id + ".json"),
        "total_steps": total_steps,
        "stall_seconds": stall_seconds,
        "started_at": time.time(),
        "cwd": os.getcwd(),
        "argv": list(sys.argv),
        "python": sys.executable,
        "trainmon_version": VERSION,
    }
    write_manifest(manifest)

    agent_info = None
    if start_agent and not env_bool("TRAINMON_NO_AGENT"):
        try:
            agent_info = ensure_agent(port)
        except Exception as exc:
            sys.stderr.write("[trainmon] agent start failed: %s\n" % exc)
    if agent_info:
        manifest["agent_url"] = dashboard_url(agent_info)
        manifest["port"] = int(agent_info.get("port"))
        write_manifest(manifest)

    reporter = Reporter(metrics_path)
    handle = RunHandle(
        run_id=run_id,
        name=display_name,
        log=log_path,
        metrics=metrics_path,
        total_steps=total_steps,
        reporter=reporter,
        agent_info=agent_info,
        enabled=True,
    )
    _HANDLES.append(handle)
    _CURRENT = handle
    _install_exit_hooks()

    try:
        url = handle.url
        sys.stderr.write(
            "[trainmon] run %s\n[trainmon] log %s\n[trainmon] metrics %s\n" %
            (display_name, log_path, metrics_path))
        if url:
            sys.stderr.write("[trainmon] dashboard %s\n" % url)
        else:
            sys.stderr.write("[trainmon] warning: agent unavailable, dashboard disabled\n")
        sys.stderr.flush()
    except Exception:
        pass
    return handle


def report(*args, **kwargs):
    if _CURRENT is None:
        return False
    return _CURRENT.report(*args, **kwargs)


def finish(status="completed", exit_code=0, error=None):
    if _CURRENT is None:
        return None
    return _CURRENT.finish(status, exit_code, error)


def current_run():
    return _CURRENT

import fcntl
import json
import os
import signal
import subprocess
import sys
import time
from urllib.request import urlopen

from . import paths
from .manifest import read_state
from .util import atomic_write_json, ensure_dir, pid_alive, read_json


def _http_json(url, timeout=1.5):
    try:
        response = urlopen(url, timeout=timeout)
        try:
            raw = response.read()
        finally:
            response.close()
        return json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        return None


def read_agent_info():
    info = read_json(paths.AGENT_JSON, None)
    if not isinstance(info, dict):
        return None
    if not info.get("port") or not pid_alive(info.get("pid")):
        return None
    return info


def agent_status(deep=True):
    info = read_agent_info()
    if not info:
        return None
    if deep:
        health = _http_json("http://127.0.0.1:%d/api/health" % int(info["port"]))
        if not health or not health.get("ok"):
            return None
        info = dict(info)
        info["health"] = health
    return info


def ensure_agent(preferred_port=None):
    info = agent_status()
    if info:
        return info
    try:
        preferred = int(preferred_port or os.environ.get("TRAINMON_PORT") or 8787)
    except (TypeError, ValueError):
        preferred = 8787

    paths.ensure_layout()
    lock_handle = open(paths.AGENT_LOCK, "w")
    try:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        info = agent_status()
        if info:
            return info
        stale = read_json(paths.AGENT_JSON, None)
        if stale and not pid_alive(stale.get("pid")):
            try:
                os.unlink(paths.AGENT_JSON)
            except OSError:
                pass
        log_handle = open(paths.AGENT_LOG, "ab", 0)
        try:
            env = os.environ.copy()
            env["TRAINMON_HOME"] = paths.HOME
            process = subprocess.Popen(
                [sys.executable, "-m", "trainmon.agent", "--port", str(preferred)],
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                cwd=os.path.expanduser("~"),
                env=env,
                close_fds=True,
                start_new_session=True,
            )
        finally:
            log_handle.close()
        deadline = time.time() + 10.0
        while time.time() < deadline:
            time.sleep(0.1)
            info = agent_status()
            if info:
                return info
            if process.poll() is not None:
                break
        return None
    finally:
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        lock_handle.close()


def stop_agent(timeout=5.0):
    info = read_agent_info()
    if not info:
        try:
            os.unlink(paths.AGENT_JSON)
        except OSError:
            pass
        return False
    pid = int(info.get("pid"))
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not pid_alive(pid):
            break
        time.sleep(0.1)
    try:
        os.unlink(paths.AGENT_JSON)
    except OSError:
        pass
    return True


def agent_url(info):
    if not info:
        return None
    return "http://127.0.0.1:%d/" % int(info["port"])


def dashboard_url(info, run_id=None):
    base = agent_url(info)
    if not base:
        return None
    if run_id:
        return base + "?run=" + run_id
    return base

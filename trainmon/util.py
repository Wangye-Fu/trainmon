import errno
import json
import math
import os
import re
import socket
import tempfile
import time


def ensure_dir(path):
    if not path:
        return
    try:
        os.makedirs(path)
    except OSError:
        if not os.path.isdir(path):
            raise


def atomic_write_json(path, obj):
    directory = os.path.dirname(os.path.abspath(path))
    ensure_dir(directory)
    fd, tmp = tempfile.mkstemp(prefix=".trainmon-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(obj, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (IOError, OSError, ValueError):
        return default


def pid_alive(pid):
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return False
        if exc.errno != errno.EPERM:
            return False
    try:
        with open("/proc/%d/stat" % pid, "r") as handle:
            parts = handle.read().split()
        if len(parts) > 2 and parts[2] == "Z":
            return False
    except (IOError, OSError):
        pass
    return True


def tail_text(path, max_bytes=65536):
    try:
        size = os.path.getsize(path)
    except (IOError, OSError):
        return ""
    start = max(0, size - max(0, int(max_bytes)))
    try:
        with open(path, "rb") as handle:
            handle.seek(start)
            data = handle.read()
    except (IOError, OSError):
        return ""
    text = data.decode("utf-8", "replace")
    if start > 0:
        cut = text.find("\n")
        if cut >= 0:
            text = text[cut + 1:]
    return text


def sanitize_name(value):
    value = (value or "").strip()
    value = re.sub(r"[^A-Za-z0-9_.-]+", "-", value)
    value = value.strip(".-")
    return value[:80]


def hostname():
    try:
        return socket.gethostname()
    except Exception:
        return "unknown"


def env_bool(name, default=False):
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def env_float(name, default):
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def clean_value(value):
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return value
    if isinstance(value, str):
        return value
    item = getattr(value, "item", None)
    if callable(item):
        try:
            return clean_value(item())
        except Exception:
            pass
    try:
        return clean_value(float(value))
    except Exception:
        return str(value)


def format_duration(seconds):
    if seconds is None:
        return "-"
    try:
        seconds = int(max(0, float(seconds)))
    except (TypeError, ValueError):
        return "-"
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return "%dh %02dm" % (hours, minutes)
    if minutes:
        return "%dm %02ds" % (minutes, secs)
    return "%ds" % secs


def iso_time(ts):
    if ts is None:
        return None
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(ts)))
    except (TypeError, ValueError):
        return None

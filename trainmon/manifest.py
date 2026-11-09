import binascii
import glob
import os
import time

from . import paths
from .util import atomic_write_json, read_json, sanitize_name

TERMINAL_STATES = ("completed", "failed", "lost", "terminated")


def run_id_for(name):
    base = sanitize_name(name) or "run"
    stamp = time.strftime("%Y%m%d-%H%M%S")
    suffix = binascii.hexlify(os.urandom(2)).decode("ascii")
    return "%s-%s-%s" % (base, stamp, suffix)


def manifest_path(run_id):
    return os.path.join(paths.RUNS_DIR, sanitize_name(run_id) + ".json")


def state_path(run_id):
    return os.path.join(paths.STATE_DIR, sanitize_name(run_id) + ".json")


def write_manifest(manifest):
    paths.ensure_layout()
    atomic_write_json(manifest_path(manifest["run_id"]), manifest)


def write_state(run_id, state):
    paths.ensure_layout()
    atomic_write_json(state_path(run_id), state)


def read_state(run_id):
    return read_json(state_path(run_id), None)


def list_manifests():
    paths.ensure_layout()
    result = []
    for path in sorted(glob.glob(os.path.join(paths.RUNS_DIR, "*.json"))):
        data = read_json(path, None)
        if not isinstance(data, dict) or not data.get("run_id"):
            continue
        try:
            data["_path"] = path
            data["_mtime"] = os.path.getmtime(path)
        except OSError:
            pass
        result.append(data)
    result.sort(key=lambda item: item.get("started_at") or 0)
    return result

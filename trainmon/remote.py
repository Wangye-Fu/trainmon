"""SSH-based remote helpers: deploy, start/stop agent, tunnel."""

import os
import shlex
import subprocess
import sys
import tarfile
import tempfile

from .config import ConfigError

EXCLUDED_DIRS = {
    ".git", ".hg", ".svn", "__pycache__", ".venv", "venv", "env",
    "build", "dist", "release", "logs", ".pytest_cache", ".mypy_cache",
}
EXCLUDED_FILES = {
    "trainmon.ini", "config.ini", ".env", ".env.local",
}
EXCLUDED_SUFFIXES = (".pyc", ".pyo", ".out", ".local.ini")


def project_root(source=None):
    candidates = []
    if source:
        candidates.append(source)
    else:
        # Prefer the current directory so the console script works from a
        # clone even when trainmon itself is installed in site-packages.
        candidates.append(os.getcwd())
        candidates.append(os.path.dirname(os.path.dirname(__file__)))
    for candidate in candidates:
        root = os.path.abspath(candidate)
        if os.path.isfile(os.path.join(root, "setup.py")) and os.path.isdir(
                os.path.join(root, "trainmon")):
            return root
    raise ConfigError(
        "source tree not found; run `trainmon deploy` from the repository "
        "or pass --source /path/to/trainmon")


def _archive_files(root):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            name for name in dirnames
            if name not in EXCLUDED_DIRS and not name.endswith(".egg-info"))
        for filename in sorted(filenames):
            if filename in EXCLUDED_FILES:
                continue
            if filename.endswith(EXCLUDED_SUFFIXES):
                continue
            path = os.path.join(dirpath, filename)
            yield path, os.path.relpath(path, root)


def build_source_archive(root):
    handle = tempfile.NamedTemporaryFile(prefix="trainmon-", suffix=".tar.gz",
                                         delete=False)
    path = handle.name
    handle.close()
    try:
        with tarfile.open(path, "w:gz") as archive:
            for source, arcname in _archive_files(root):
                archive.add(source, arcname=arcname)
    except Exception:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise
    return path


def _python(server):
    return shlex.quote(server.python)


def _remote_prefix(server):
    return "cd %s && " % server.remote_dir_cmd


def run_ssh(server, command, verbose=False):
    args = server.ssh_args() + [command]
    if verbose:
        print("+ %s" % " ".join(shlex.quote(part) for part in args))
    try:
        return subprocess.call(args)
    except OSError as exc:
        raise ConfigError("cannot run ssh: %s" % exc)


def remote_trainmon(server, args, env=None, verbose=False):
    command = _remote_prefix(server)
    if env:
        assignments = []
        for key, value in env.items():
            assignments.append("%s=%s" % (key, shlex.quote(str(value))))
        command += " ".join(assignments) + " "
    command += "%s -m trainmon %s" % (
        _python(server), " ".join(shlex.quote(str(arg)) for arg in args))
    return run_ssh(server, command, verbose=verbose)


def deploy(server, source=None, dry_run=False, verbose=False):
    root = project_root(source)
    archive_path = build_source_archive(root)
    remote_command = (
        "mkdir -p {dir} && tar xzf - -C {dir} && cd {dir} && "
        "{python} -m pip install --no-deps --no-build-isolation "
        "--upgrade --force-reinstall . && {python} -m trainmon doctor"
    ).format(
        dir=server.remote_dir_cmd,
        python=_python(server),
    )
    args = server.ssh_args() + [remote_command]
    try:
        if dry_run:
            print("+ tar -czf - . | %s" % " ".join(
                shlex.quote(part) for part in args))
            print("archive would contain %d files" % sum(1 for _ in _archive_files(root)))
            return 0
        if verbose:
            print("deploying %s -> %s (%s)" % (root, server.name, server.describe()))
        with open(archive_path, "rb") as handle:
            process = subprocess.Popen(args, stdin=handle)
            return process.wait()
    finally:
        try:
            os.unlink(archive_path)
        except OSError:
            pass


def start(server, verbose=False):
    env = {}
    if server.idle_exit and server.idle_exit > 0:
        env["TRAINMON_IDLE_EXIT"] = int(server.idle_exit)
    return remote_trainmon(server, ["agent", "start"], env=env, verbose=verbose)


def stop(server, verbose=False):
    return remote_trainmon(server, ["agent", "stop"], verbose=verbose)


def restart(server, verbose=False):
    env = {}
    if server.idle_exit and server.idle_exit > 0:
        env["TRAINMON_IDLE_EXIT"] = int(server.idle_exit)
    return remote_trainmon(server, ["agent", "restart"], env=env, verbose=verbose)


def status(server, verbose=False):
    return remote_trainmon(server, ["status"], verbose=verbose)


def doctor(server, verbose=False):
    return remote_trainmon(server, ["doctor"], verbose=verbose)


def url(server):
    return server.url


def tunnel_args(server, local_port=None, remote_port=None):
    local = int(local_port or server.local_port)
    remote = int(remote_port or server.remote_port)
    extra = [
        "-N",
        "-L", "%d:127.0.0.1:%d" % (local, remote),
        "-o", "ExitOnForwardFailure=yes",
        "-o", "ServerAliveInterval=30",
        "-o", "ServerAliveCountMax=3",
    ]
    return server.ssh_args(extra_options=extra), local, remote


def tunnel(server, local_port=None, remote_port=None, background=False,
           dry_run=False, verbose=False):
    args, local, remote = tunnel_args(server, local_port, remote_port)
    if dry_run:
        print(" ".join(shlex.quote(part) for part in args))
        print("then open http://127.0.0.1:%d/" % local)
        return 0
    if verbose:
        print("+ %s" % " ".join(shlex.quote(part) for part in args))
    if background:
        if not server.identity_file:
            sys.stderr.write(
                "[trainmon] warning: background mode needs SSH key/agent auth; "
                "if your host asks for a password, run in foreground instead\n")
        process = subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
        )
        print("tunnel started (pid %d): http://127.0.0.1:%d/ -> %s:%d" % (
            process.pid, local, server.host, remote))
        return 0
    try:
        return subprocess.call(args)
    except KeyboardInterrupt:
        return 0

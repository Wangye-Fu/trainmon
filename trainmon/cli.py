import argparse
import json
import os
import runpy
import sys
from urllib.request import urlopen

from . import VERSION, attach
from . import paths
from . import remote
from .agent_client import agent_status, dashboard_url, ensure_agent, stop_agent
from .config import ConfigError, find_config, load_config, write_example
from .manifest import list_manifests, read_state
from .parser import LogParser
from .util import format_duration


def _agent_runs():
    info = agent_status()
    if not info:
        return None, None
    try:
        response = urlopen("http://127.0.0.1:%d/api/runs" % int(info["port"]), timeout=2.0)
        try:
            raw = response.read()
        finally:
            response.close()
        return info, json.loads(raw.decode("utf-8", "replace")).get("runs", [])
    except Exception:
        return info, None


def _print_runs(runs):
    header = "%-28s %-11s %14s %10s %12s %10s" % (
        "RUN", "STATUS", "STEP", "ETA", "LOSS", "SPEED")
    print(header)
    print("-" * len(header))
    for run in runs:
        step = "%s/%s" % (run.get("step", "-"), run.get("total_steps", "-"))
        eta = run.get("eta_text") or format_duration(run.get("eta_seconds"))
        loss = run.get("loss")
        loss = "%.4f" % loss if isinstance(loss, (int, float)) else "-"
        rate = run.get("rate")
        rate = "%.2f/s" % rate if isinstance(rate, (int, float)) else "-"
        print("%-28s %-11s %14s %10s %12s %10s" % (
            (run.get("name") or run.get("run_id"))[:28],
            run.get("status", "-"),
            step,
            eta,
            loss,
            rate,
        ))


def _load(args):
    try:
        return load_config(args.config)
    except ConfigError as exc:
        print("error: %s" % exc)
        return None


def _server(args):
    config = _load(args)
    if config is None:
        return None
    try:
        return config.resolve(getattr(args, "server", None))
    except ConfigError as exc:
        print("error: %s" % exc)
        return None


def cmd_status(args):
    if getattr(args, "server", None):
        server = _server(args)
        if server is None:
            return 1
        return remote.status(server, verbose=args.verbose)
    info, runs = _agent_runs()
    if runs is not None:
        _print_runs(runs)
        print("\nagent: http://127.0.0.1:%d/  (pid %s)" % (info["port"], info.get("pid")))
        return 0
    manifests = list_manifests()
    if not manifests:
        print("no runs found under %s" % paths.RUNS_DIR)
        return 0
    print("agent is not running; showing persisted state")
    rows = []
    for manifest in manifests:
        state = read_state(manifest["run_id"]) or {}
        rows.append({
            "name": manifest.get("name"),
            "run_id": manifest.get("run_id"),
            "status": state.get("status", "unknown"),
            "step": state.get("step"),
            "total_steps": state.get("total_steps"),
            "eta_seconds": state.get("eta_seconds"),
            "loss": state.get("loss"),
            "rate": state.get("rate"),
        })
    _print_runs(rows)
    return 0


def cmd_url(args):
    info = agent_status(deep=False)
    if not info:
        print("agent is not running. Start it with: python -m trainmon agent start")
        return 1
    url = dashboard_url(info)
    if args.run:
        url = dashboard_url(info, args.run)
    print(url)
    return 0


def cmd_agent(args):
    if args.action == "start":
        info = ensure_agent(args.port)
        if not info:
            print("failed to start agent")
            return 1
        print("agent running: http://127.0.0.1:%d/ (pid %s)" % (info["port"], info.get("pid")))
        return 0
    if args.action == "stop":
        print("stopped" if stop_agent() else "agent was not running")
        return 0
    if args.action == "restart":
        stop_agent()
        info = ensure_agent(args.port)
        if not info:
            print("failed to restart agent")
            return 1
        print("agent running: http://127.0.0.1:%d/ (pid %s)" % (info["port"], info.get("pid")))
        return 0
    info = agent_status()
    if not info:
        print("agent is not running")
        return 1
    print(json.dumps(info, ensure_ascii=False, indent=2))
    return 0


def cmd_doctor(args):
    if getattr(args, "server", None):
        server = _server(args)
        if server is None:
            return 1
        return remote.doctor(server, verbose=args.verbose)
    print("trainmon %s" % VERSION)
    print("python   %s" % sys.version.split()[0])
    print("home     %s" % paths.HOME)
    print("runs     %s" % paths.RUNS_DIR)
    info = agent_status()
    if info:
        print("agent    OK  http://127.0.0.1:%d/  pid=%s  uptime=%.0fs" % (
            info["port"], info.get("pid"), info.get("health", {}).get("uptime", 0)))
    else:
        print("agent    NOT RUNNING (run: python -m trainmon agent start)")
    manifests = list_manifests()
    print("runs     %d manifest(s)" % len(manifests))
    ok = True
    for manifest in manifests:
        log = manifest.get("log")
        metrics = manifest.get("metrics")
        exists = bool(log and os.path.exists(log))
        readable = bool(exists and os.access(log, os.R_OK))
        state = read_state(manifest["run_id"]) or {}
        print("  %-30s log=%s readable=%s status=%s" % (
            (manifest.get("name") or manifest.get("run_id"))[:30],
            "OK" if exists else "MISSING",
            "yes" if readable else "no",
            state.get("status", "?"),
        ))
        if not exists:
            ok = False
        if metrics and not os.path.exists(metrics):
            print("    metrics MISSING: %s" % metrics)
    return 0 if ok else 1


def cmd_parse(args):
    path = os.path.expanduser(args.file)
    if not os.path.exists(path):
        print("file not found: %s" % path)
        return 1
    parser = LogParser()
    seen = 0
    last = None
    last_line = None
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.rstrip("\r\n")
            updates = parser.parse_line(line)
            if updates:
                seen += 1
                last = updates
                last_line = line
    print("parsed lines: %d" % seen)
    print("last update:", json.dumps(last, ensure_ascii=False))
    print("last line:  ", last_line)
    return 0


def cmd_run(args):
    script = os.path.abspath(os.path.expanduser(args.script))
    if not os.path.exists(script):
        print("script not found: %s" % script)
        return 2
    os.environ["TRAINMON_WRAPPER"] = "1"
    handle = attach(
        run=args.run,
        log=args.log,
        total_steps=args.total_steps,
        stall_seconds=args.stall_seconds,
    )
    old_argv = sys.argv
    sys.argv = [script] + list(args.script_args)
    try:
        runpy.run_path(script, run_name="__main__")
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
        handle.finish("completed" if code == 0 else "failed", code)
        return code
    except Exception as exc:
        handle.finish("failed", 1, "%s: %s" % (type(exc).__name__, exc))
        raise
    else:
        handle.finish("completed", 0)
        return 0
    finally:
        sys.argv = old_argv


def cmd_config(args):
    if args.action == "init":
        path = args.path or args.config or os.path.abspath("trainmon.ini")
        try:
            written = write_example(path, force=args.force)
        except ConfigError as exc:
            print("error: %s" % exc)
            return 1
        print("created %s" % written)
        print("edit it, then run: trainmon servers")
        return 0
    if args.action == "path":
        try:
            path = find_config(args.path or args.config)
        except ConfigError as exc:
            print("error: %s" % exc)
            return 1
        if not path:
            print("no config found; run: trainmon config init")
            return 1
        print(path)
        return 0
    try:
        config = load_config(args.path or args.config)
    except ConfigError as exc:
        print("error: %s" % exc)
        return 1
    print("config: %s" % config.path)
    if config.default:
        print("default: %s" % config.default)
    for server in config.servers.values():
        print("")
        print("[%s] %s" % (server.name, server.describe()))
        print("  remote_dir   %s" % server.remote_dir)
        print("  python       %s" % server.python)
        print("  dashboard    %s" % server.url)
        print("  ssh target   %s" % server.host)
        print("  ssh port     %s" % server.port)
        if server.user:
            print("  ssh user     %s" % server.user)
        if server.identity_file:
            print("  identity     %s" % server.identity_file)
        if server.idle_exit:
            print("  idle_exit    %.0fs" % server.idle_exit)
    return 0


def cmd_servers(args):
    config = _load(args)
    if config is None:
        return 1
    print("%-16s %-28s %-22s %s" % ("NAME", "SSH", "REMOTE_DIR", "DASHBOARD"))
    print("-" * 96)
    for server in config.servers.values():
        target = server.host if not server.user else "%s@%s" % (server.user, server.host)
        print("%-16s %-28s %-22s %s" % (
            server.name[:16], target[:28], server.remote_dir[:22], server.url))
    return 0


def cmd_deploy(args):
    server = _server(args)
    if server is None:
        return 1
    try:
        return remote.deploy(server, source=args.source, dry_run=args.dry_run,
                             verbose=args.verbose)
    except ConfigError as exc:
        print("error: %s" % exc)
        return 1


def cmd_start(args):
    server = _server(args)
    if server is None:
        return 1
    return remote.start(server, verbose=args.verbose)


def cmd_stop(args):
    server = _server(args)
    if server is None:
        return 1
    return remote.stop(server, verbose=args.verbose)


def cmd_restart(args):
    server = _server(args)
    if server is None:
        return 1
    return remote.restart(server, verbose=args.verbose)


def cmd_tunnel(args):
    server = _server(args)
    if server is None:
        return 1
    return remote.tunnel(
        server,
        local_port=args.local_port,
        remote_port=args.remote_port,
        background=args.background,
        dry_run=args.dry_run,
        verbose=args.verbose,
    )


def cmd_open(args):
    server = _server(args)
    if server is None:
        return 1
    print(server.url)
    return 0


def build_parser():
    parser = argparse.ArgumentParser(prog="trainmon")
    parser.add_argument("--version", action="version", version="trainmon " + VERSION)
    parser.add_argument("-c", "--config", help="path to trainmon.ini")
    sub = parser.add_subparsers(dest="command")

    status = sub.add_parser("status", help="show runs (local or --server NAME)")
    status.add_argument("--server", help="read status from a configured server")
    status.add_argument("--verbose", action="store_true")
    status.set_defaults(func=cmd_status)

    url = sub.add_parser("url", help="print local dashboard URL")
    url.add_argument("--run", help="run id to open")
    url.set_defaults(func=cmd_url)

    agent = sub.add_parser("agent", help="manage the local host agent")
    agent.add_argument("action", choices=["start", "stop", "restart", "status"])
    agent.add_argument("--port", type=int, default=None)
    agent.set_defaults(func=cmd_agent)

    doctor = sub.add_parser("doctor", help="check local or remote installation")
    doctor.add_argument("server", nargs="?", help="configured server name")
    doctor.add_argument("--verbose", action="store_true")
    doctor.set_defaults(func=cmd_doctor)

    parse = sub.add_parser("parse", help="test the log parser on a file")
    parse.add_argument("file")
    parse.set_defaults(func=cmd_parse)

    run = sub.add_parser("run", help="run a script with monitoring enabled")
    run.add_argument("--run", help="display name")
    run.add_argument("--log", help="log file to tail")
    run.add_argument("--total-steps", type=int, default=None)
    run.add_argument("--stall-seconds", type=float, default=None)
    run.add_argument("script")
    run.add_argument("script_args", nargs=argparse.REMAINDER)
    run.set_defaults(func=cmd_run)

    config = sub.add_parser("config", help="create or inspect trainmon.ini")
    config.add_argument("action", choices=["init", "path", "show"])
    config.add_argument("--path", help="config path (init/path)")
    config.add_argument("--force", action="store_true", help="overwrite on init")
    config.set_defaults(func=cmd_config)

    servers = sub.add_parser("servers", help="list configured servers")
    servers.set_defaults(func=cmd_servers)

    deploy = sub.add_parser("deploy", help="copy and install on a configured server")
    deploy.add_argument("server", nargs="?")
    deploy.add_argument("--source", help="source tree (default: repository root)")
    deploy.add_argument("--dry-run", action="store_true")
    deploy.add_argument("--verbose", action="store_true")
    deploy.set_defaults(func=cmd_deploy)

    start = sub.add_parser("start", help="start the agent on a configured server")
    start.add_argument("server", nargs="?")
    start.add_argument("--verbose", action="store_true")
    start.set_defaults(func=cmd_start)

    stop = sub.add_parser("stop", help="stop the agent on a configured server")
    stop.add_argument("server", nargs="?")
    stop.add_argument("--verbose", action="store_true")
    stop.set_defaults(func=cmd_stop)

    restart = sub.add_parser("restart", help="restart the agent on a configured server")
    restart.add_argument("server", nargs="?")
    restart.add_argument("--verbose", action="store_true")
    restart.set_defaults(func=cmd_restart)

    tunnel = sub.add_parser("tunnel", help="open an SSH tunnel to the dashboard")
    tunnel.add_argument("server", nargs="?")
    tunnel.add_argument("--local-port", type=int, default=None)
    tunnel.add_argument("--remote-port", type=int, default=None)
    tunnel.add_argument("--background", action="store_true")
    tunnel.add_argument("--dry-run", action="store_true")
    tunnel.add_argument("--verbose", action="store_true")
    tunnel.set_defaults(func=cmd_tunnel)

    open_cmd = sub.add_parser("open", help="print the configured dashboard URL")
    open_cmd.add_argument("server", nargs="?")
    open_cmd.set_defaults(func=cmd_open)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    return args.func(args)

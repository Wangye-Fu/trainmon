"""Configuration file handling for TrainMon.

TrainMon intentionally uses INI instead of YAML/TOML so the package keeps
zero third-party dependencies and still works on Python 3.6.
"""

import configparser
import os
import shlex
from collections import OrderedDict



EXAMPLE_CONFIG = """\
# TrainMon configuration.
#
# Copy this file to:
#   ./trainmon.ini                  (project-local)
#   ~/.trainmon/config.ini          (per-user, recommended)
#
# Do not commit the real file: trainmon.ini is in .gitignore.
#
# If `host` is an alias in ~/.ssh/config, you can omit user/port/identity_file
# and let SSH handle the connection details (including ProxyJump).

[general]
# default = gpu-01

[server:gpu-01]
# SSH host or ~/.ssh/config alias. Required.
host = gpu-01

# Optional SSH fields. Omit them to use your ~/.ssh/config / current user.
# user = your-user
# port = 22
# identity_file = ~/.ssh/id_ed25519
# extra_ssh_args = -o ProxyJump=bastion

# Remote directory where TrainMon is installed.
remote_dir = ~/trainmon

# Python interpreter used by the training job.
python = python3

# Local port for `trainmon tunnel` and remote agent port.
local_port = 8787
remote_port = 8787

# Agent exits after N seconds with no active runs/viewers. 0 = never.
idle_exit = 0
"""


class ConfigError(Exception):
    pass


def _expand(value):
    return os.path.abspath(os.path.expanduser(value))


def find_config(path=None):
    if path:
        candidate = _expand(path)
        if not os.path.isfile(candidate):
            raise ConfigError("config file not found: %s" % candidate)
        return candidate
    env_path = os.environ.get("TRAINMON_CONFIG")
    if env_path:
        candidate = _expand(env_path)
        if not os.path.isfile(candidate):
            raise ConfigError("TRAINMON_CONFIG points to a missing file: %s" % candidate)
        return candidate
    candidates = [
        os.path.abspath("trainmon.ini"),
        os.path.join(os.path.expanduser("~/.trainmon"), "config.ini"),
    ]
    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate
    return None


def remote_path_expr(path):
    """Quote a remote path while still allowing a leading ~."""
    path = (path or "~/trainmon").strip()
    if path == "~":
        return '"$HOME"'
    if path.startswith("~/"):
        return '"$HOME"/' + shlex.quote(path[2:])
    return shlex.quote(path)


class ServerConfig(object):
    def __init__(self, name, values):
        self.name = name
        self.host = (values.get("host") or "").strip()
        if not self.host:
            raise ConfigError("server [%s] is missing required field: host" % name)
        self.user = (values.get("user") or "").strip() or None
        self.port = _to_port(values.get("port"), 22)
        self.identity_file = (values.get("identity_file") or "").strip() or None
        self.extra_ssh_args = (values.get("extra_ssh_args") or "").strip()
        self.remote_dir = (values.get("remote_dir") or "~/trainmon").strip()
        self.python = (values.get("python") or "python3").strip()
        self.local_port = _to_port(values.get("local_port"), 8787)
        self.remote_port = _to_port(values.get("remote_port"), 8787)
        self.idle_exit = _to_float(values.get("idle_exit"), 0.0)
        self.label = (values.get("label") or name).strip()

    @property
    def remote_dir_cmd(self):
        return remote_path_expr(self.remote_dir)

    @property
    def url(self):
        return "http://127.0.0.1:%d/" % self.local_port

    def ssh_args(self, extra_options=None):
        args = ["ssh"]
        if self.port and self.port != 22:
            args += ["-p", str(self.port)]
        if self.identity_file:
            args += ["-i", os.path.expanduser(self.identity_file)]
        if self.extra_ssh_args:
            args += shlex.split(self.extra_ssh_args)
        args += ["-o", "ConnectTimeout=10"]
        if extra_options:
            args += list(extra_options)
        if self.user:
            args += ["-l", self.user]
        args.append(self.host)
        return args

    def describe(self):
        return self.host if not self.user else "%s@%s" % (self.user, self.host)


def _to_port(value, default):
    try:
        port = int(value)
    except (TypeError, ValueError):
        return default
    if port <= 0 or port > 65535:
        raise ConfigError("invalid port: %s" % value)
    return port


def _to_float(value, default):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class Config(object):
    def __init__(self, path):
        self.path = path
        self.servers = OrderedDict()
        self.default = None

    def resolve(self, name=None):
        if name:
            if name not in self.servers:
                raise ConfigError(
                    "server %r not found. Available: %s" % (
                        name, ", ".join(self.servers.keys()) or "(none)"))
            return self.servers[name]
        if self.default:
            if self.default not in self.servers:
                raise ConfigError("[general] default = %s is not a defined server" % self.default)
            return self.servers[self.default]
        if len(self.servers) == 1:
            return list(self.servers.values())[0]
        if not self.servers:
            raise ConfigError("no [server:NAME] sections found in %s" % self.path)
        raise ConfigError(
            "multiple servers configured; specify one: %s" % ", ".join(self.servers.keys()))


def load_config(path=None):
    config_path = find_config(path)
    if not config_path:
        raise ConfigError(
            "no config found. Run `trainmon config init` and edit trainmon.ini, "
            "or set TRAINMON_CONFIG=/path/to/config.ini")
    parser = configparser.ConfigParser(interpolation=None)
    try:
        with open(config_path, "r", encoding="utf-8") as handle:
            parser.read_file(handle)
    except (IOError, OSError) as exc:
        raise ConfigError("cannot read %s: %s" % (config_path, exc))
    except configparser.Error as exc:
        raise ConfigError("invalid config %s: %s" % (config_path, exc))

    config = Config(config_path)
    defaults = {}
    if parser.has_section("general"):
        general = dict(parser.items("general"))
        config.default = (general.get("default") or "").strip() or None
    if parser.has_section("defaults"):
        defaults.update(dict(parser.items("defaults")))

    for section in parser.sections():
        if not section.startswith("server:"):
            continue
        name = section.split(":", 1)[1].strip()
        if not name:
            raise ConfigError("invalid empty server name in [%s]" % section)
        values = dict(defaults)
        values.update(dict(parser.items(section)))
        config.servers[name] = ServerConfig(name, values)
    if not config.servers:
        raise ConfigError("no [server:NAME] sections found in %s" % config_path)
    return config


def write_example(path, force=False):
    path = _expand(path)
    directory = os.path.dirname(path)
    if directory and not os.path.isdir(directory):
        os.makedirs(directory)
    if os.path.exists(path) and not force:
        raise ConfigError("file already exists: %s (use --force to overwrite)" % path)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(EXAMPLE_CONFIG)
    return path

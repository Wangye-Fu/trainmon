import os

VERSION = "0.1.0"


def _home():
    raw = os.environ.get("TRAINMON_HOME") or "~/.trainmon"
    return os.path.abspath(os.path.expanduser(raw))


HOME = _home()
RUNS_DIR = os.path.join(HOME, "runs")
STATE_DIR = os.path.join(HOME, "state")
LOG_DIR = os.path.join(HOME, "logs")
AGENT_JSON = os.path.join(HOME, "agent.json")
AGENT_LOCK = os.path.join(HOME, "agent.lock")
AGENT_LOG = os.path.join(LOG_DIR, "agent.log")
WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")


def ensure_layout():
    for path in (HOME, RUNS_DIR, STATE_DIR, LOG_DIR):
        try:
            os.makedirs(path)
        except OSError:
            if not os.path.isdir(path):
                raise

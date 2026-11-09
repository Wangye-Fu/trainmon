import re
import time

_SEP = r"['\"]?\s*(?:[=:]\s*|\s+)"
_RE_STEP = re.compile(
    r"(?:global_)?step" + _SEP + r"(\d+)(?:\s*/\s*(\d+))?", re.I)
_RE_TQDM = re.compile(r"\b(\d+)\s*/\s*(\d+)\s*\[")
_RE_EPOCH = re.compile(
    r"epoch" + _SEP + r"(\d+(?:\.\d+)?)(?:\s*/\s*(\d+))?", re.I)
_RE_LOSS = re.compile(
    r"\bloss" + _SEP + r"([0-9]+(?:\.[0-9]+)?(?:[eE][+-]?\d+)?)", re.I)
_RE_LR = re.compile(
    r"\b(?:lr|learning_rate)" + _SEP +
    r"([0-9]+(?:\.[0-9]+)?(?:[eE][+-]?\d+)?)", re.I)
_RE_RATE = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*(?:it/s|steps?/s)", re.I)
_RE_PCT = re.compile(r"(\d{1,3})%\|")
_RE_TS = re.compile(r"^\s*(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})")
_RE_DONE = re.compile(
    r"\b(training\s+(?:complete|completed|finished|done)|finished\s+training|"
    r"training\s+end|done\s+training)\b",
    re.I,
)
_RE_FAIL = re.compile(
    r"(CUDA out of memory|OutOfMemoryError|Out of memory|"
    r"Traceback \(most recent call last\)|RuntimeError:|"
    r"Segmentation fault|Killed)",
    re.I,
)


class LogParser(object):
    def parse_line(self, line):
        if not line:
            return None
        line = line[:4000]
        updates = {}
        timestamp = _RE_TS.search(line)
        if timestamp:
            try:
                updates["log_ts"] = time.mktime(time.strptime(
                    timestamp.group(1) + " " + timestamp.group(2),
                    "%Y-%m-%d %H:%M:%S"))
            except (TypeError, ValueError):
                pass
        match = _RE_STEP.search(line)
        if match:
            updates["step"] = int(match.group(1))
            if match.group(2):
                updates["total_steps"] = int(match.group(2))
        else:
            match = _RE_TQDM.search(line)
            if match:
                updates["step"] = int(match.group(1))
                updates["total_steps"] = int(match.group(2))
        match = _RE_EPOCH.search(line)
        if match:
            try:
                updates["epoch"] = float(match.group(1))
            except ValueError:
                pass
            if match.group(2):
                try:
                    updates["total_epochs"] = int(match.group(2))
                except ValueError:
                    pass
        match = _RE_LOSS.search(line)
        if match:
            try:
                updates["loss"] = float(match.group(1))
            except ValueError:
                pass
        match = _RE_LR.search(line)
        if match:
            try:
                updates["lr"] = float(match.group(1))
            except ValueError:
                pass
        match = _RE_RATE.search(line)
        if match:
            try:
                updates["reported_rate"] = float(match.group(1))
            except ValueError:
                pass
        match = _RE_PCT.search(line)
        if match:
            try:
                updates["percent"] = int(match.group(1))
            except ValueError:
                pass
        if _RE_DONE.search(line):
            updates["done"] = True
        if _RE_FAIL.search(line):
            updates["fail"] = True
        return updates or None

    def is_done(self, text):
        return bool(_RE_DONE.search(text or ""))

    def is_fail(self, text):
        return bool(_RE_FAIL.search(text or ""))

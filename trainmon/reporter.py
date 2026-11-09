import io
import json
import os
import queue
import sys
import threading
import time

from .util import clean_value, ensure_dir


class Reporter(object):
    """Non-blocking JSONL writer. Training threads only enqueue."""

    def __init__(self, path, max_queue=10000):
        self.path = path
        ensure_dir(os.path.dirname(os.path.abspath(path)))
        self._queue = queue.Queue(maxsize=max_queue)
        self._stop = threading.Event()
        self._closed = False
        self.dropped = 0
        self._error_logged = False
        self._thread = threading.Thread(target=self._run, name="trainmon-reporter")
        self._thread.daemon = True
        self._thread.start()

    def report(self, record):
        if self._closed:
            return False
        try:
            self._queue.put_nowait(record)
            return True
        except queue.Full:
            self.dropped += 1
            return False

    def _run(self):
        handle = None
        try:
            handle = io.open(self.path, "a", encoding="utf-8")
        except (IOError, OSError) as exc:
            self._log_error("cannot open metrics file: %s" % exc)
            return
        try:
            while True:
                try:
                    item = self._queue.get(timeout=0.5)
                except queue.Empty:
                    if self._stop.is_set():
                        break
                    continue
                if item is None:
                    break
                batch = [item]
                while len(batch) < 500:
                    try:
                        nxt = self._queue.get_nowait()
                    except queue.Empty:
                        break
                    if nxt is None:
                        self._stop.set()
                        break
                    batch.append(nxt)
                try:
                    for record in batch:
                        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                    handle.flush()
                except (IOError, OSError) as exc:
                    self._log_error("cannot write metrics: %s" % exc)
                if self._stop.is_set():
                    # Drain anything left, then exit.
                    try:
                        if self._queue.empty():
                            break
                    except Exception:
                        break
        finally:
            try:
                handle.flush()
                handle.close()
            except Exception:
                pass

    def _log_error(self, message):
        if self._error_logged:
            return
        self._error_logged = True
        try:
            sys.stderr.write("[trainmon] reporter: %s\n" % message)
        except Exception:
            pass

    def close(self, timeout=4.0):
        if self._closed:
            return
        self._closed = True
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._queue.put_nowait(None)
            except queue.Full:
                pass
        self._thread.join(timeout)
        try:
            if self._thread.is_alive():
                self._stop.set()
        except Exception:
            pass


def make_record(step=None, loss=None, lr=None, epoch=None, total_steps=None,
                total_epochs=None, metrics=None, ts=None):
    record = {"ts": float(ts if ts is not None else time.time())}
    values = {
        "step": step,
        "loss": loss,
        "lr": lr,
        "epoch": epoch,
        "total_steps": total_steps,
        "total_epochs": total_epochs,
    }
    for key, value in values.items():
        cleaned = clean_value(value)
        if cleaned is not None:
            record[key] = cleaned
    if metrics:
        cleaned_metrics = {}
        for key, value in metrics.items():
            cleaned = clean_value(value)
            if cleaned is not None:
                cleaned_metrics[str(key)] = cleaned
        if cleaned_metrics:
            record["metrics"] = cleaned_metrics
    return record

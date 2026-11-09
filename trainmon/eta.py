import math
from collections import deque


class EtaEstimator(object):
    """Robust remaining-time estimator.

    Uses a least-squares slope of step over time in a sliding window, then
    derives a 95% prediction interval for the slope from the residuals.
    """

    def __init__(self, window_seconds=120.0, max_points=80, min_points=3):
        self.window_seconds = float(window_seconds)
        self.max_points = int(max_points)
        self.min_points = int(min_points)
        self.samples = deque()
        self._last_step = None

    def reset(self):
        self.samples.clear()
        self._last_step = None

    def add(self, ts, step):
        try:
            ts = float(ts)
            step = int(step)
        except (TypeError, ValueError):
            return
        if self._last_step is not None and step < self._last_step:
            self.reset()
        self.samples.append((ts, step))
        self._last_step = step
        cutoff = ts - self.window_seconds
        while self.samples and self.samples[0][0] < cutoff:
            self.samples.popleft()
        while len(self.samples) > self.max_points:
            self.samples.popleft()

    def _fit(self):
        """Return a dict with slope, R², standard error and sample geometry."""
        points = list(self.samples)
        if len(points) < self.min_points:
            return None
        t0 = points[0][0]
        xs = [ts - t0 for ts, _ in points]
        ys = [float(step) for _, step in points]
        n = float(len(points))
        sum_x = sum(xs)
        sum_y = sum(ys)
        sum_xx = sum(x * x for x in xs)
        sum_xy = sum(x * y for x, y in zip(xs, ys))
        sxx = sum_xx - sum_x * sum_x / n
        if sxx <= 0:
            return None
        slope = (sum_xy - sum_x * sum_y / n) / sxx
        intercept = (sum_y - slope * sum_x) / n
        mean_y = sum_y / n
        ss_tot = sum((y - mean_y) ** 2 for y in ys)
        residuals = [y - (intercept + slope * x) for x, y in zip(xs, ys)]
        ss_res = sum(r * r for r in residuals)
        r2 = 0.0 if ss_tot <= 0 else max(0.0, 1.0 - ss_res / ss_tot)
        se = 0.0
        if len(points) > 2:
            se = math.sqrt(max(0.0, ss_res / (len(points) - 2)) / sxx)
        # Log timestamps often have only 1s resolution, so a perfect fit can
        # look artificially certain. Keep a small uncertainty floor.
        floor = 0.05 if len(points) < 15 else 0.03
        se = max(se, abs(slope) * floor)
        return {
            "slope": slope,
            "r2": r2,
            "se": se,
            "n": len(points),
            "sxx": sxx,
        }

    def rate(self):
        fit = self._fit()
        if not fit or fit["slope"] <= 0:
            return None
        return fit["slope"]

    def confidence(self):
        fit = self._fit()
        if not fit or fit["slope"] <= 0:
            return "low"
        relative_error = fit["se"] / fit["slope"] if fit["slope"] else 1.0
        if fit["r2"] >= 0.97 and relative_error < 0.12:
            return "high"
        if fit["r2"] >= 0.85 and relative_error < 0.35:
            return "medium"
        return "low"

    def estimate(self, total_steps, step):
        """Return (rate, eta, confidence, eta_fast, eta_slow) in seconds."""
        fit = self._fit()
        if not fit or fit["slope"] <= 0:
            return None, None, "low", None, None
        slope = fit["slope"]
        if not total_steps or step is None:
            return slope, None, self.confidence(), None, None
        remaining = max(0, int(total_steps) - int(step))
        eta = remaining / slope if slope > 0 else None
        if eta is None:
            return slope, None, "low", None, None
        eta_fast = eta
        eta_slow = eta
        se = fit.get("se") or 0.0
        if se > 0:
            slope_low = slope - 1.96 * se
            slope_high = slope + 1.96 * se
            if slope_high > 0:
                eta_fast = remaining / slope_high
            if slope_low > 0:
                eta_slow = remaining / slope_low
            else:
                eta_slow = None  # too uncertain; UI shows an open-ended range
        return slope, eta, self.confidence(), eta_fast, eta_slow

"""BC smoothing filters — multi-mode.

Direct port of ESP32's BCFilter struct from measure.cpp.
Supports: median-of-3 only, median-of-3 + adaptive EMA, adaptive Kalman.
"""

FILT_MEDIAN3 = "median3"
FILT_MEDIAN3_EMA = "ema"
FILT_ADAPTIVE_KALMAN = "kalman"


class BCFilter:
    """Multi-mode BC filter. Port from ESP32 measure.cpp BCFilter struct.

    Modes:
        median3  — median-of-3 only (default, zero phase lag)
        ema      — median-of-3 + adaptive EMA (variance-based alpha)
        kalman   — median-of-3 + innovation-gated adaptive Kalman
    """

    def __init__(self, mode=FILT_MEDIAN3):
        self.mode = mode
        self.x = 0.0
        self.p = 1.0
        self.q_base = 0.8
        self.r = 2.5
        self.med = [0.0, 0.0, 0.0]
        self.mi = 0
        self.primed = False
        self.innov_ema = 0.0
        self.var_buf = [0.0] * 6
        self.var_idx = 0

    def reset(self, mode=None):
        if mode is not None:
            self.mode = mode
        self.x = 0.0
        self.p = 1.0
        self.q_base = 0.8
        self.r = 2.5
        self.mi = 0
        self.primed = False
        self.innov_ema = 0.0
        self.var_buf = [0.0] * 6
        self.var_idx = 0

    def update(self, raw: float) -> float:
        """Feed a new raw BC value and return the filtered estimate."""
        self.med[self.mi % 3] = raw
        self.mi += 1

        if self.mi < 3:
            self.x = raw
            self.p = 1.0
            return raw

        # Median-of-3
        a, b, c = self.med
        m = max(min(a, b), min(max(a, b), c))

        if self.mode == FILT_MEDIAN3:
            self.x = m
            return m

        if not self.primed:
            self.primed = True
            self.x = m
            self.p = 1.0
            return self.x

        if self.mode == FILT_MEDIAN3_EMA:
            self.var_buf[self.var_idx % 6] = m
            self.var_idx += 1
            if self.var_idx < 6:
                self.x = m
                return m
            s = sum(self.var_buf)
            s2 = sum(v * v for v in self.var_buf)
            v = (s2 / 6.0) - (s / 6.0) ** 2
            norm_v = min(v / 2e6, 1.0)
            alpha = 0.6 - 0.45 * norm_v  # 0.15..0.6
            self.x = alpha * m + (1.0 - alpha) * self.x
            return self.x

        # FILT_ADAPTIVE_KALMAN
        innov = m - self.x
        self.innov_ema = 0.4 * innov + 0.6 * self.innov_ema
        innov_ratio = (self.innov_ema ** 2) / (self.r + 1.0)
        q = self.q_base * (1.0 + 3.0 * min(innov_ratio, 5.0))
        pp = self.p + q
        k = pp / (pp + self.r)
        self.x = self.x + k * (m - self.x)
        self.p = (1.0 - k) * pp
        return self.x


def sigma_reject(values: list, sigma_limit: float = 3.0) -> tuple:
    """3-sigma outlier rejection on a list of values.

    Port from ESP32 measure.cpp lines 180-194.
    Returns (robust_mean, count_kept, count_rejected).
    """
    if not values:
        return 0.0, 0, 0

    n = len(values)
    if n < 4:
        return sum(values) / n, n, 0

    mean = sum(values) / n
    variance = sum((v - mean) ** 2 for v in values) / n
    std = variance ** 0.5

    if std < 1e-12:
        return mean, n, 0

    kept = [v for v in values if abs(v - mean) <= sigma_limit * std]

    if not kept:
        return mean, n, 0

    robust_mean = sum(kept) / len(kept)
    return robust_mean, len(kept), n - len(kept)

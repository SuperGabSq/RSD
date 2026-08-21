"""Sample-rate estimation.

The brief asks for an estimated sample rate "measured at every frame", so the estimate
is ``samples_in_frame / (t_n - t_{n-1})`` -- per frame, not averaged over the session.

That honest per-frame number is also unusably jittery on screen. At a 10 ms interval,
OS scheduling and TCP coalescing routinely move the arrival time by a millisecond or
more, which is a 10 % swing in the estimate. So this class reports both:

* ``instantaneous`` -- the raw per-frame estimate, shown on the log line.
* ``smoothed`` -- an EMA (alpha = 0.1, roughly a ten-frame time constant), shown on the
  gauge, where a readable needle matters more than per-frame fidelity.

Showing both means the smoothing hides nothing.

The clock is injected, never read here. Tests can therefore assert exact rates from
exact intervals without sleeping, and without flaking on a loaded CI box.
"""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_ALPHA = 0.1


@dataclass(frozen=True, slots=True)
class RateEstimate:
    """Both rates in samples/second, or ``None`` when not yet measurable."""

    instantaneous: float | None
    smoothed: float | None


class SampleRateEstimator:
    """Stateful per-session estimator. Not thread-safe; one instance per session,
    used only from the acquisition thread."""

    __slots__ = ("_alpha", "_last_monotonic_s", "_smoothed")

    def __init__(self, alpha: float = DEFAULT_ALPHA) -> None:
        if not 0.0 < alpha <= 1.0:
            raise ValueError("alpha must be in (0, 1]")
        self._alpha = alpha
        self._last_monotonic_s: float | None = None
        self._smoothed: float | None = None

    def reset(self) -> None:
        """Forget history. Called when a session ends, so a reconnect starts clean
        rather than smoothing across a gap of arbitrary length."""
        self._last_monotonic_s = None
        self._smoothed = None

    def update(self, sample_count: int, monotonic_s: float) -> RateEstimate:
        previous = self._last_monotonic_s
        self._last_monotonic_s = monotonic_s

        if previous is None:
            # First frame of the session: no interval exists, so there is nothing to
            # measure. Reporting anything here would be inventing a number.
            return RateEstimate(instantaneous=None, smoothed=self._smoothed)

        delta_s = monotonic_s - previous
        if delta_s <= 0.0:
            # Two frames sharing a clock tick. The interval is unmeasurable, not zero;
            # a division would produce infinity and poison the EMA permanently.
            return RateEstimate(instantaneous=None, smoothed=self._smoothed)

        instantaneous = sample_count / delta_s
        if self._smoothed is None:
            self._smoothed = instantaneous
        else:
            self._smoothed = self._alpha * instantaneous + (1.0 - self._alpha) * self._smoothed
        return RateEstimate(instantaneous=instantaneous, smoothed=self._smoothed)

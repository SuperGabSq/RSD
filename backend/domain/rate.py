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
    """Rates in samples/second, or ``None`` when not yet measurable.

    Three statistics of one measurement, because three consumers want different things
    from it and no single number serves all of them:

    * ``instantaneous`` -- the log line. The honest per-frame value the brief asks for.
    * ``smoothed`` -- the gauge. Readable, and responds to a real change within ~10 frames.
    * ``session_mean`` -- the frequency axis. Total samples over total elapsed time.

    The third exists because the first two cannot scale an axis. A frequency axis is
    fs/2 wide, so any error in fs moves *every* peak by the same proportion, and an EMA
    with a ten-frame constant swings hard when the transport stalls and then bursts --
    measured at 6 Msps on a 2 Msps stream under load, which puts a 50 kHz tone at
    157 kHz. The session mean cannot do that: it is a ratio of two monotonically growing
    quantities, so a burst that arrives early is cancelled by the gap before it. It still
    tracks a genuine rate change (``--rate-factor 0.5``), over seconds rather than
    frames -- the right time constant for a property of the hardware rather than of the
    link.
    """

    instantaneous: float | None
    smoothed: float | None
    session_mean: float | None = None


class SampleRateEstimator:
    """Stateful per-session estimator. Not thread-safe; one instance per session,
    used only from the acquisition thread."""

    __slots__ = ("_alpha", "_last_monotonic_s", "_smoothed", "_first_monotonic_s", "_samples_seen")

    def __init__(self, alpha: float = DEFAULT_ALPHA) -> None:
        if not 0.0 < alpha <= 1.0:
            raise ValueError("alpha must be in (0, 1]")
        self._alpha = alpha
        self._last_monotonic_s: float | None = None
        self._smoothed: float | None = None
        self._first_monotonic_s: float | None = None
        self._samples_seen = 0

    def reset(self) -> None:
        """Forget history. Called when a session ends, so a reconnect starts clean
        rather than smoothing across a gap of arbitrary length."""
        self._last_monotonic_s = None
        self._smoothed = None
        self._first_monotonic_s = None
        self._samples_seen = 0

    def _session_mean(self, monotonic_s: float) -> float | None:
        # The first frame's samples are deliberately excluded: they arrived before the
        # window opened, so counting them over an interval they did not occupy would bias
        # the mean high exactly when the denominator is smallest.
        if self._first_monotonic_s is None:
            return None
        elapsed = monotonic_s - self._first_monotonic_s
        return self._samples_seen / elapsed if elapsed > 0.0 else None

    def update(self, sample_count: int, monotonic_s: float) -> RateEstimate:
        previous = self._last_monotonic_s
        self._last_monotonic_s = monotonic_s

        if previous is None:
            # First frame of the session: no interval exists, so there is nothing to
            # measure. Reporting anything here would be inventing a number.
            self._first_monotonic_s = monotonic_s
            return RateEstimate(instantaneous=None, smoothed=self._smoothed)

        self._samples_seen += sample_count
        session_mean = self._session_mean(monotonic_s)

        delta_s = monotonic_s - previous
        if delta_s <= 0.0:
            # Two frames sharing a clock tick. The interval is unmeasurable, not zero;
            # a division would produce infinity and poison the EMA permanently.
            return RateEstimate(None, self._smoothed, session_mean)

        instantaneous = sample_count / delta_s
        if self._smoothed is None:
            self._smoothed = instantaneous
        else:
            self._smoothed = self._alpha * instantaneous + (1.0 - self._alpha) * self._smoothed
        return RateEstimate(instantaneous, self._smoothed, session_mean)

"""Rate-estimator tests.

Every timestamp here is injected. Nothing sleeps, nothing reads a clock, so these
assertions are exact rather than approximate and cannot flake on a loaded machine.
"""

from __future__ import annotations

import pytest

from backend.domain.rate import SampleRateEstimator

FRAME_SAMPLES = 20_000
FRAME_PERIOD_S = 0.01  # 100 Hz -> 2 Msps


@pytest.fixture
def estimator() -> SampleRateEstimator:
    return SampleRateEstimator(alpha=0.1)


def test_first_frame_reports_no_rate(estimator):
    """Assumption #10: the first frame of a session has no predecessor, so there is no
    interval to measure. A value derived from time-since-connect would be invented."""
    estimate = estimator.update(FRAME_SAMPLES, monotonic_s=100.0)
    assert estimate.instantaneous is None
    assert estimate.smoothed is None


def test_known_interval_yields_exactly_the_expected_rate(estimator):
    estimator.update(FRAME_SAMPLES, monotonic_s=100.0)
    estimate = estimator.update(FRAME_SAMPLES, monotonic_s=100.0 + FRAME_PERIOD_S)
    assert estimate.instantaneous == pytest.approx(2_000_000.0)


def test_first_measurable_frame_seeds_the_ema_rather_than_ramping_from_zero(estimator):
    """Seeding with the first real value avoids a ten-frame climb from 0 that would
    show the operator a wrong rate for the first tenth of a second."""
    estimator.update(FRAME_SAMPLES, monotonic_s=0.0)
    estimate = estimator.update(FRAME_SAMPLES, monotonic_s=FRAME_PERIOD_S)
    assert estimate.smoothed == pytest.approx(estimate.instantaneous)


def test_ema_converges_towards_a_steady_rate(estimator):
    """Feed a step change and assert the EMA moves monotonically to the new value."""
    t = 0.0
    for _ in range(20):  # settle at 2 Msps
        estimator.update(FRAME_SAMPLES, monotonic_s=t)
        t += FRAME_PERIOD_S
    settled = estimator.update(FRAME_SAMPLES, monotonic_s=t).smoothed
    assert settled == pytest.approx(2_000_000.0)

    # Halve the rate (the simulator's --rate-factor 0.5 case) and let it track.
    previous = settled
    for _ in range(60):
        t += FRAME_PERIOD_S * 2
        estimate = estimator.update(FRAME_SAMPLES, monotonic_s=t)
        assert estimate.instantaneous == pytest.approx(1_000_000.0)
        assert estimate.smoothed < previous  # strictly descending, no overshoot
        previous = estimate.smoothed
    assert previous == pytest.approx(1_000_000.0, rel=0.01)


def _settled(estimator, frames: int = 30) -> float:
    """Run an estimator to steady state at 2 Msps; return the *last* timestamp fed to it,
    so a caller can express the next arrival as an explicit offset from it."""
    t = 0.0
    for _ in range(frames):
        estimator.update(FRAME_SAMPLES, monotonic_s=t)
        t += FRAME_PERIOD_S
    return t - FRAME_PERIOD_S


def test_smoothed_lags_instantaneous_so_the_gauge_does_not_chase_jitter(estimator):
    """The point of the gauge's EMA: a one-frame scheduling hiccup must not swing it."""
    last = _settled(estimator)

    # one frame stalls: a 40 ms gap instead of 10 ms
    estimate = estimator.update(FRAME_SAMPLES, monotonic_s=last + FRAME_PERIOD_S * 4)
    assert estimate.instantaneous == pytest.approx(500_000.0)
    # The instantaneous reading collapsed by 4x; the gauge moved by under a quarter.
    assert estimate.smoothed > 1_500_000.0


def test_a_burst_and_a_stall_move_the_gauge_by_comparable_amounts():
    """Why the EMA runs on the interval rather than on the rate.

    Averaging ``1/delta_s`` is convex, so the two directions of the same jitter are not
    weighted alike. A 10 ms gap arriving as 40 ms is a 4x collapse in the reported rate;
    a 10 ms gap arriving as ~0 ms is an *unbounded* spike. Under the old rate-EMA the
    stall moved the gauge 7.5 % while a 6 us burst -- ordinary TCP coalescing -- moved it
    by four orders of magnitude and then owned it for the whole time constant. That
    asymmetry, not the jitter itself, is what put red on the screen during a stream the
    simulator was pacing at a flat 100.0 Hz.

    Smoothing the interval bounds both directions by construction: no single frame can
    drag the EMA more than alpha of the way towards itself, whichever way it errs.
    """
    stalled = SampleRateEstimator(alpha=0.1)
    stall = stalled.update(FRAME_SAMPLES, monotonic_s=_settled(stalled) + FRAME_PERIOD_S * 4)

    burst = SampleRateEstimator(alpha=0.1)
    spike = burst.update(FRAME_SAMPLES, monotonic_s=_settled(burst) + 0.000_006)

    assert spike.instantaneous > 3_000_000_000.0  # the raw estimate is still absurd...
    # ...but the gauge cannot follow it there. Neither excursion runs away, and the burst
    # -- the one that used to be unbounded -- is now the *smaller* of the two.
    assert 1_400_000.0 < stall.smoothed < 2_000_000.0
    assert 2_000_000.0 < spike.smoothed < 2_300_000.0


def test_rate_scales_with_sample_count_not_just_interval(estimator):
    """The estimate is samples-per-second, so a short frame over the same interval is
    a genuinely lower rate -- not a normalisation to be hidden."""
    estimator.update(FRAME_SAMPLES, monotonic_s=0.0)
    estimate = estimator.update(10_000, monotonic_s=FRAME_PERIOD_S)
    assert estimate.instantaneous == pytest.approx(1_000_000.0)


@pytest.mark.parametrize("delta_s", [0.0, -0.005])
def test_non_positive_interval_is_unmeasurable_and_does_not_poison_the_ema(estimator, delta_s):
    """Two frames on one clock tick would divide by zero. Report nothing and keep the
    last good smoothed value rather than writing infinity into the EMA for ever."""
    estimator.update(FRAME_SAMPLES, monotonic_s=0.0)
    good = estimator.update(FRAME_SAMPLES, monotonic_s=FRAME_PERIOD_S).smoothed

    estimate = estimator.update(FRAME_SAMPLES, monotonic_s=FRAME_PERIOD_S + delta_s)
    assert estimate.instantaneous is None
    assert estimate.smoothed == pytest.approx(good)

    # ...and the estimator recovers on the next well-ordered frame.
    next_t = FRAME_PERIOD_S + delta_s + FRAME_PERIOD_S
    recovered = estimator.update(FRAME_SAMPLES, monotonic_s=next_t)
    assert recovered.instantaneous == pytest.approx(2_000_000.0)


def test_reset_clears_history_so_a_reconnect_does_not_smooth_across_the_gap(estimator):
    """Assumption #6/#14: a reconnect is a new session. Carrying the old EMA across a
    gap of unknown length would blend two unrelated streams."""
    estimator.update(FRAME_SAMPLES, monotonic_s=0.0)
    estimator.update(FRAME_SAMPLES, monotonic_s=FRAME_PERIOD_S)

    estimator.reset()

    estimate = estimator.update(FRAME_SAMPLES, monotonic_s=900.0)
    assert estimate.instantaneous is None
    assert estimate.smoothed is None


@pytest.mark.parametrize("bad_alpha", [0.0, -0.1, 1.5])
def test_rejects_nonsense_alpha(bad_alpha):
    with pytest.raises(ValueError):
        SampleRateEstimator(alpha=bad_alpha)


def test_alpha_of_one_disables_smoothing(estimator):
    """alpha = 1 is the degenerate 'no smoothing' case and must stay well defined."""
    unsmoothed = SampleRateEstimator(alpha=1.0)
    unsmoothed.update(FRAME_SAMPLES, monotonic_s=0.0)
    unsmoothed.update(FRAME_SAMPLES, monotonic_s=FRAME_PERIOD_S)
    estimate = unsmoothed.update(FRAME_SAMPLES, monotonic_s=FRAME_PERIOD_S * 3)
    assert estimate.smoothed == pytest.approx(estimate.instantaneous)

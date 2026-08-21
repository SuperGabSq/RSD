from __future__ import annotations

from datetime import datetime

import pytest

from backend.domain.frame import FrameReport, RawFrame


def raw(number: int = 1, received_at: datetime | None = None) -> RawFrame:
    return RawFrame(
        number=number,
        payload=b"\x01\x02\x03\x04" * 20_000,
        received_at=received_at or datetime(2024, 1, 1, 12, 0, 0),
        monotonic_s=1234.5,
    )


def test_timestamp_matches_the_briefs_format_exactly():
    """The brief shows `[2024-01-01 12:00:00]`. Second precision, no timezone suffix,
    no fractional part -- matched character for character."""
    report = FrameReport.build(
        raw(received_at=datetime(2024, 1, 1, 12, 0, 0)),
        hash_hex="0" * 32,
        sample_count=20_000,
        is_valid=True,
        malformed=False,
        estimated_rate_hz=2_000_000.0,
    )
    assert report.timestamp == "2024-01-01 12:00:00"


def test_build_carries_the_analysis_results_through_unchanged():
    report = FrameReport.build(
        raw(number=42),
        hash_hex="deadbeef" * 4,
        sample_count=19_995,
        is_valid=False,
        malformed=True,
        estimated_rate_hz=None,
    )
    assert report.number == 42
    assert report.hash == "deadbeef" * 4
    assert report.sample_count == 19_995
    assert report.is_valid is False
    assert report.malformed is True
    assert report.estimated_rate_hz is None


def test_frames_are_immutable_across_the_thread_boundary():
    """Frames are produced on the 100 Hz acquisition thread and read on the ~30 Hz
    publisher thread. Immutability is what makes that hand-off safe without copying."""
    frame = raw()
    with pytest.raises(AttributeError):
        frame.number = 2  # type: ignore[misc]

    report = FrameReport.build(
        frame,
        hash_hex="0" * 32,
        sample_count=20_000,
        is_valid=True,
        malformed=False,
        estimated_rate_hz=None,
    )
    with pytest.raises(AttributeError):
        report.is_valid = False  # type: ignore[misc]


def test_frames_use_slots_so_the_100_hz_path_stays_cheap():
    """100 allocations/s forever: no per-instance __dict__."""
    assert not hasattr(raw(), "__dict__")

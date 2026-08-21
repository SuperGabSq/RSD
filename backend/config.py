"""Configuration, read from the environment once at startup.

Everything the brief left adjustable lives here as one frozen object rather than as
``os.environ`` lookups scattered through the code. A reviewer can see the whole
tunable surface of the application in twenty lines, and tests construct one directly
instead of mutating process state.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from backend.domain.decimation import DEFAULT_TARGET_COLUMNS
from backend.domain.rate import DEFAULT_ALPHA
from backend.domain.spectrum import DEFAULT_SAMPLE_RATE_HZ, DEFAULT_TARGET_BINS
from backend.domain.validation import DEFAULT_EXPECTED_SAMPLES


@dataclass(frozen=True, slots=True)
class Config:
    expected_samples: int = DEFAULT_EXPECTED_SAMPLES
    nominal_sample_rate_hz: float = DEFAULT_SAMPLE_RATE_HZ
    rate_ema_alpha: float = DEFAULT_ALPHA
    target_columns: int = DEFAULT_TARGET_COLUMNS
    spectrum_bins: int = DEFAULT_TARGET_BINS
    publish_hz: float = 30.0
    max_pending_reports: int = 5_000
    upstream_connect_timeout_s: float = 5.0
    # flask-sock caps inbound message size. Control messages are tiny, but leaving the
    # cap implicit is how a future larger message fails with an unexplained close.
    max_downstream_message_bytes: int = 1 << 20

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Config:
        source = os.environ if env is None else env

        def _int(name: str, default: int) -> int:
            return int(source.get(name, default))

        def _float(name: str, default: float) -> float:
            return float(source.get(name, default))

        return cls(
            expected_samples=_int("EXPECTED_SAMPLES", DEFAULT_EXPECTED_SAMPLES),
            nominal_sample_rate_hz=_float("SAMPLE_RATE_HZ", DEFAULT_SAMPLE_RATE_HZ),
            rate_ema_alpha=_float("RATE_EMA_ALPHA", DEFAULT_ALPHA),
            target_columns=_int("TARGET_COLUMNS", DEFAULT_TARGET_COLUMNS),
            spectrum_bins=_int("SPECTRUM_BINS", DEFAULT_TARGET_BINS),
            publish_hz=_float("PUBLISH_HZ", 30.0),
            max_pending_reports=_int("MAX_PENDING_REPORTS", 5_000),
            upstream_connect_timeout_s=_float("UPSTREAM_CONNECT_TIMEOUT_S", 5.0),
            max_downstream_message_bytes=_int("MAX_DOWNSTREAM_MESSAGE_BYTES", 1 << 20),
        )

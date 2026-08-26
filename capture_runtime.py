from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Mapping


MIN_ADAPTIVE_TIMEOUT_SECONDS = 0.30
MAX_ADAPTIVE_TIMEOUT_SECONDS = 2.50


@dataclass(frozen=True, slots=True)
class AdaptiveWaitState:
    timeout_seconds: float
    ewma_seconds: float = 0.0
    samples: int = 0
    timeout_streak: int = 0

    def to_mapping(self) -> dict[str, float | int]:
        return {
            "timeout_seconds": round(self.timeout_seconds, 3),
            "ewma_seconds": round(self.ewma_seconds, 3),
            "samples": self.samples,
            "timeout_streak": self.timeout_streak,
        }


def clamp_timeout(value: float) -> float:
    if not math.isfinite(value):
        return MIN_ADAPTIVE_TIMEOUT_SECONDS
    return min(MAX_ADAPTIVE_TIMEOUT_SECONDS, max(MIN_ADAPTIVE_TIMEOUT_SECONDS, value))


def adaptive_wait_from_mapping(
    value: object,
    base_timeout_seconds: float,
) -> AdaptiveWaitState:
    base = clamp_timeout(base_timeout_seconds)
    if not isinstance(value, Mapping):
        return AdaptiveWaitState(timeout_seconds=base)
    try:
        raw_timeout = float(value.get("timeout_seconds", base))
        raw_ewma = float(value.get("ewma_seconds", 0.0))
        if not math.isfinite(raw_timeout) or not math.isfinite(raw_ewma):
            raise ValueError("non-finite adaptive timing")
        # The base value is the application's safety window. A saved or learned
        # value may extend it, but must never shorten it: a late Ctrl+C can still
        # arrive after an early timeout and overwrite the user's clipboard.
        timeout = max(base, clamp_timeout(raw_timeout))
        ewma = max(0.0, min(raw_ewma, MAX_ADAPTIVE_TIMEOUT_SECONDS))
        samples = max(0, min(int(value.get("samples", 0)), 1_000_000))
        streak = max(0, min(int(value.get("timeout_streak", 0)), 100))
    except (TypeError, ValueError, OverflowError):
        return AdaptiveWaitState(timeout_seconds=base)
    return AdaptiveWaitState(timeout, ewma, samples, streak)


def learn_adaptive_wait(
    previous: AdaptiveWaitState,
    *,
    reason: str,
    elapsed_ms: int,
    minimum_timeout_seconds: float = MIN_ADAPTIVE_TIMEOUT_SECONDS,
) -> AdaptiveWaitState:
    """Learn only from trusted clipboard successes and real copy timeouts."""

    if reason == "captured" and elapsed_ms > 0:
        observed = min(MAX_ADAPTIVE_TIMEOUT_SECONDS, max(0.025, elapsed_ms / 1000.0))
        ewma = observed if previous.samples == 0 else previous.ewma_seconds * 0.65 + observed * 0.35
        target = max(
            clamp_timeout(minimum_timeout_seconds),
            clamp_timeout(ewma * 2.0 + 0.10),
        )
        return AdaptiveWaitState(target, ewma, previous.samples + 1, 0)
    if reason == "no_clipboard_change":
        streak = previous.timeout_streak + 1
        timeout = previous.timeout_seconds
        if streak >= 2:
            timeout = clamp_timeout(timeout + 0.10)
        return AdaptiveWaitState(timeout, previous.ewma_seconds, previous.samples, streak)
    return AdaptiveWaitState(
        previous.timeout_seconds,
        previous.ewma_seconds,
        previous.samples,
        previous.timeout_streak,
    )

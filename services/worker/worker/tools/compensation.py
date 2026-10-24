"""Deterministic compensation arithmetic. The LLM interprets rules; code does the maths.

Amounts follow CAR Sec 3 Series M Part IV Rev 4, para 3.3.2 (cancellation, by block time).
TODO: verify every figure against the ingested clause text before relying on it.
"""
from __future__ import annotations


def cancellation_compensation_inr(block_time_minutes: int, one_way_basic_fare_plus_fuel_inr: float) -> float:
    if block_time_minutes <= 60:
        cap = 5000
    elif block_time_minutes <= 120:
        cap = 7500
    else:
        cap = 10000
    return float(min(cap, one_way_basic_fare_plus_fuel_inr))

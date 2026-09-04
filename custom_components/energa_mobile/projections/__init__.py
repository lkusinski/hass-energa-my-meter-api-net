"""Projections read-model package (statistics and flow derivations from canonical store)."""

from .statistics import (
    build_cumulative_statistic_data,
    build_statistic_id,
    build_virtual_bank_flow_data,
)

__all__ = [
    "build_cumulative_statistic_data",
    "build_statistic_id",
    "build_virtual_bank_flow_data",
]

"""Legifrance medallion pipeline built from LEGI bulk, legacy texts and DILA."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import LegifrancePipelineConfig
    from .pipeline import LegifrancePipeline

__all__ = ["LegifrancePipeline", "LegifrancePipelineConfig"]


def __getattr__(name: str):
    if name == "LegifrancePipelineConfig":
        from .config import LegifrancePipelineConfig

        return LegifrancePipelineConfig
    if name == "LegifrancePipeline":
        from .pipeline import LegifrancePipeline

        return LegifrancePipeline
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

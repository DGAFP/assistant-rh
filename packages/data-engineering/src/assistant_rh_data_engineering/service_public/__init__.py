"""Service-Public medallion pipeline built from the official XML feed."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import ServicePublicPipelineConfig
    from .pipeline import ServicePublicPipeline

__all__ = ["ServicePublicPipeline", "ServicePublicPipelineConfig"]


def __getattr__(name: str):
    if name == "ServicePublicPipelineConfig":
        from .config import ServicePublicPipelineConfig

        return ServicePublicPipelineConfig
    if name == "ServicePublicPipeline":
        from .pipeline import ServicePublicPipeline

        return ServicePublicPipeline
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

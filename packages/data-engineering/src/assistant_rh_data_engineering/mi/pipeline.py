from __future__ import annotations

from typing import Any, Optional

from ..pdf_ministry.pipeline import MedallionPipeline
from .config import IDENTITY, MiPipelineConfig

__all__ = ["MiPipeline"]


class MiPipeline(MedallionPipeline):
    def __init__(self, config: Optional[MiPipelineConfig] = None, **kwargs: Any):
        super().__init__(IDENTITY, config or MiPipelineConfig(), **kwargs)

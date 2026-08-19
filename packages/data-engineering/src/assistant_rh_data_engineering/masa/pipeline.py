from __future__ import annotations

from typing import Any, Optional

from ..pdf_ministry.pipeline import MedallionPipeline
from .config import IDENTITY, MasaPipelineConfig

__all__ = ["MasaPipeline"]


class MasaPipeline(MedallionPipeline):
    def __init__(self, config: Optional[MasaPipelineConfig] = None, **kwargs: Any):
        super().__init__(IDENTITY, config or MasaPipelineConfig(), **kwargs)

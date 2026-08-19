from __future__ import annotations

from typing import Any, Optional

from ..pdf_ministry.pipeline import MedallionPipeline
from ..pdf_ministry.qna.gold import QnaGoldBuilder
from ..pdf_ministry.qna.silver import QnaSilverBuilder
from .config import IDENTITY, QNA_ENGINE_CONFIG, MattePipelineConfig

__all__ = ["MattePipeline"]


class MattePipeline(MedallionPipeline):
    """Pipeline MATTE: infra du socle, chunking QNA legacy (silver/gold)."""

    def __init__(self, config: Optional[MattePipelineConfig] = None, **kwargs: Any):
        super().__init__(IDENTITY, config or MattePipelineConfig(), **kwargs)

    def _make_silver_builder(self) -> Any:
        return QnaSilverBuilder(IDENTITY, QNA_ENGINE_CONFIG)

    def _make_gold_builder(self) -> Any:
        return QnaGoldBuilder(IDENTITY, self.config.embeddings, self.config.gold, QNA_ENGINE_CONFIG)

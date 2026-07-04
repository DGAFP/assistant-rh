from __future__ import annotations

from typing import Any, Optional

from ..pdf_ministry.pipeline import MedallionPipeline
from ..pdf_ministry.pipeline import plan_reconciliation as _plan_reconciliation
from ..utils.grist import ManifestRow
from .config import IDENTITY, MiPipelineConfig

__all__ = ["MiPipeline", "plan_reconciliation"]


def plan_reconciliation(
    expected: dict[str, ManifestRow],
    current: dict[str, dict[str, Any]],
    checksums: dict[str, str],
    *,
    force_reocr: bool = False,
    protected: frozenset[str] | set[str] = frozenset(),
) -> dict[str, list[str]]:
    """Règle MI: « zéro chunk => retraiter » (leçon de l\'audit MATTE)."""
    return _plan_reconciliation(
        expected,
        current,
        checksums,
        force_reocr=force_reocr,
        protected=protected,
        retry_zero_chunk=True,
    )


class MiPipeline(MedallionPipeline):
    def __init__(self, config: Optional[MiPipelineConfig] = None, **kwargs: Any):
        super().__init__(IDENTITY, config or MiPipelineConfig(), **kwargs)

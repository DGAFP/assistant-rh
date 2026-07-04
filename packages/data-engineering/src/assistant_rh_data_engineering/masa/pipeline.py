from __future__ import annotations

from typing import Any, Optional

from ..pdf_ministry.pipeline import MedallionPipeline
from ..pdf_ministry.pipeline import plan_reconciliation as _plan_reconciliation
from ..utils.grist import ManifestRow
from .config import IDENTITY, MasaPipelineConfig

__all__ = ["MasaPipeline", "plan_reconciliation"]


def plan_reconciliation(
    expected: dict[str, ManifestRow],
    current: dict[str, dict[str, Any]],
    checksums: dict[str, str],
    *,
    force_reocr: bool = False,
    protected: frozenset[str] | set[str] = frozenset(),
) -> dict[str, list[str]]:
    """Divergence MASA: un doc à zéro chunk avec checksum concordant converge
    (le filtre payload gold rend le zéro chunk légitime; l\'ingest est
    transactionnel, donc checksum en base = lot complet écrit)."""
    return _plan_reconciliation(
        expected,
        current,
        checksums,
        force_reocr=force_reocr,
        protected=protected,
        retry_zero_chunk=False,
    )


class MasaPipeline(MedallionPipeline):
    def __init__(self, config: Optional[MasaPipelineConfig] = None, **kwargs: Any):
        super().__init__(IDENTITY, config or MasaPipelineConfig(), **kwargs)

"""Surcharges locales de la config RAG pour l'éval, sans écrire dans ``rag_config``.

La config runtime (``rag_config``, ligne unique en base) est partagée : deux runs
parallèles avec des réglages différents doivent surcharger en mémoire, jamais
muter la base. Ce module applique des surcharges ``section.champ=valeur`` à la
config imbriquée (legacy mutable ou ``apps/api`` figée) en reconstruisant les
dataclasses avec :func:`dataclasses.replace`.

Le type cible est déduit de la valeur courante du champ : une surcharge mal
typée ou un champ absent de la config de la branche échoue immédiatement,
avant tout appel au pipeline (ex. ``selector.reranker_union_top_k`` tant que le
code de la PR #644 n'est pas mergé).
"""

from __future__ import annotations

import dataclasses
from enum import Enum
from typing import Any

_TRUE = {"1", "true", "yes", "on", "oui"}
_FALSE = {"0", "false", "no", "off", "non"}


def parse_override(spec: str) -> tuple[str, str]:
    """``"aggregation.rerank_input_k=40"`` -> ``("aggregation.rerank_input_k", "40")``."""
    path, sep, value = spec.partition("=")
    path = path.strip()
    if not sep or not path:
        raise ValueError(f"surcharge invalide {spec!r} : format attendu SECTION.CHAMP=VALEUR")
    return path, value.strip()


def coerce_value(current: Any, raw: Any, *, path: str) -> Any:
    """Convertit ``raw`` dans le type de ``current`` (valeur actuelle du champ)."""
    if current is None:
        raise ValueError(f"{path} : type cible inconnu (valeur courante None)")
    if not isinstance(raw, str):
        # Valeur déjà typée (flag CLI nommé) : on refuse seulement les types incompatibles.
        if isinstance(current, Enum):
            return type(current)(raw.value if isinstance(raw, Enum) else raw)
        if isinstance(current, bool) != isinstance(raw, bool):
            raise ValueError(f"{path} : {raw!r} n'est pas du type {type(current).__name__}")
        if isinstance(current, float) and isinstance(raw, int):
            return float(raw)
        if not isinstance(raw, type(current)):
            raise ValueError(f"{path} : {raw!r} n'est pas du type {type(current).__name__}")
        return raw
    text = raw.strip()
    try:
        if isinstance(current, bool):
            lowered = text.lower()
            if lowered in _TRUE:
                return True
            if lowered in _FALSE:
                return False
            raise ValueError(text)
        if isinstance(current, Enum):
            return type(current)(text)
        if isinstance(current, int):
            return int(text)
        if isinstance(current, float):
            return float(text)
        if isinstance(current, (tuple, list)):
            return type(current)(part.strip() for part in text.split(",") if part.strip())
        if isinstance(current, str):
            return text
    except ValueError as exc:
        raise ValueError(f"{path} : {raw!r} n'est pas une valeur {type(current).__name__} valide") from exc
    raise ValueError(f"{path} : type {type(current).__name__} non surchargeable")


def _field_names(obj: Any) -> list[str]:
    return [field.name for field in dataclasses.fields(obj)]


def apply_config_overrides(config: Any, overrides: list[tuple[str, Any]]) -> tuple[Any, list[str]]:
    """Applique ``[(\"section.champ\", valeur), ...]`` et renvoie ``(config, ajustements)``.

    ``config`` n'est jamais modifiée en place : la valeur renvoyée est une
    nouvelle instance. Un même chemin surchargé deux fois est refusé (deux
    flags contradictoires ne doivent pas se résoudre silencieusement par ordre).
    """
    seen: set[str] = set()
    adjustments: list[str] = []
    for path, raw in overrides:
        if path in seen:
            raise ValueError(f"{path} surchargé plusieurs fois")
        seen.add(path)
        section_name, dot, field_name = path.partition(".")
        if not dot or not field_name or "." in field_name:
            raise ValueError(f"surcharge invalide {path!r} : format attendu SECTION.CHAMP")
        if section_name not in _field_names(config):
            raise ValueError(f"section inconnue {section_name!r} ; sections : {', '.join(_field_names(config))}")
        section = getattr(config, section_name)
        if not dataclasses.is_dataclass(section):
            raise ValueError(f"{section_name!r} n'est pas une section de config")
        if field_name not in _field_names(section):
            raise ValueError(
                f"champ inconnu {path!r} dans la config de cette branche ; champs de {section_name} : {', '.join(_field_names(section))}"
            )
        value = coerce_value(getattr(section, field_name), raw, path=path)
        config = dataclasses.replace(config, **{section_name: dataclasses.replace(section, **{field_name: value})})
        adjustments.append(f"{path}={value.value if isinstance(value, Enum) else value}")
    return config, adjustments

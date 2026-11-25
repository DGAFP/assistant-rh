"""Évaluer le pipeline hexagonal servi en prod (``apps/api``) avec la CLI goldset.

La CLI d'éval historique construit le pipeline legacy figé
(``packages/rag-pipeline``) : une modification d'``apps/api`` y est invisible.
Ce module construit le ``Pipeline`` d'``apps/api`` exactement comme l'API
(``RAGConfigurationService`` + adaptateurs DB/providers via
``create_pipeline_factory``), puis expose l'interface synchrone attendue par
``src.goldset.eval.run_question`` : ``run_with_trace(question, retrieval_scope=…)``
renvoyant un résultat au format legacy (``answer``, ``context_items``,
``sources``, ``timing``, ``metadata``).

Les résultats intermédiaires sont capturés bruts à chaque étage (``EvalRunContext``)
plutôt que relus dans les traces persistées : celles-ci sont tronquées à 40
éléments et 64 Ko, ce qui fausserait les métriques par étage du funnel.

Dépendance : ``assistant_rh_api`` doit être installé dans l'environnement
(``uv sync --all-packages --group dev``, comme en CI).
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass, field
from datetime import UTC
from typing import Any
from zoneinfo import ZoneInfo

import psycopg

from src.goldset.config_overrides import apply_config_overrides

try:
    import httpx
    from assistant_rh_api.bootstrap import create_pipeline_factory
    from assistant_rh_api.core.errors import ApplicationError, DatabaseUnavailable, InferenceFailure
    from assistant_rh_api.core.ministry_policy import MINISTRIES
    from assistant_rh_api.core.models.chat import ChatInput, RunContext
    from assistant_rh_api.core.models.rag_configuration import RAGConfig
    from assistant_rh_api.core.pipeline.steps.context_builder import thaw
    from assistant_rh_api.core.pipeline.trace_projection import aggregation_trace, query_trace, selection_trace
    from assistant_rh_api.core.prompt_policy import DEFAULT_GENERATOR_PROMPT, load_prompt
    from assistant_rh_api.core.rag_configuration import RAGConfigurationService
    from assistant_rh_api.db.dsn import DatabaseSettings
    from assistant_rh_api.db.pool import Database
    from assistant_rh_api.db.settings_stores import ConfigStore, PromptStore
    from assistant_rh_api.gateways.auth import SystemClock
    from assistant_rh_api.gateways.ids import RunIds
    from assistant_rh_api.gateways.packaged_prompts import PackagedPromptStore
except ImportError as exc:  # pragma: no cover - dépend de l'environnement uv
    raise ImportError("--pipeline api exige le paquet assistant_rh_api : lancer `uv sync --all-packages --group dev` (comme la CI).") from exc

PIPELINE_NAME = "api"
DEFAULT_MINISTRY = "matte"
EVAL_MODEL = "assistant-rh-eval"
# Étages dont on capture le résultat brut (noms des ``context.stage`` du Pipeline).
_CAPTURED_STAGES = ("query-processor", "retriever", "section-aggregator", "context-selector", "context-builder", "generator")

# Flags CLI nommés -> chemin dans le RAGConfig d'apps/api. Même vocabulaire que
# les flags legacy, pour qu'un A/B se décrive pareil dans les deux modes.
NAMED_API_OVERRIDES: dict[str, str] = {
    "selector_model": "selector.model",
    "selector_provider": "selector.provider",
    "generator_model": "generation.model",
    "generator_provider": "generation.provider",
    "system_prompt_name": "generation.system_prompt_name",
    "section_rerank_top_k": "aggregation.section_rerank_top_k",
    "rerank_input_k": "aggregation.rerank_input_k",
    "initial_top_k": "retrieval.initial_top_k",
    "ivfflat_probes": "retrieval.ivfflat_probes",
    "min_kept_sections": "selector.min_kept_sections",
    "doc_entire_threshold_wide": "context.doc_entire_threshold_wide",
    "reranker_union_top_k": "selector.reranker_union_top_k",
}


class ApiPipelineError(RuntimeError):
    """Échec du pipeline API, avec un message non vide pour ``EvalItem.error``."""


# Variables libpq qu'apps/api refuse (validate_libpq_environment) : elles
# pourraient rediriger une connexion. Le `.env` racine en définit pour d'autres outils.
LIBPQ_TARGET_VARIABLES = ("PGHOST", "PGHOSTADDR", "PGPORT", "PGDATABASE", "PGUSER", "PGSERVICE", "PGSERVICEFILE")


def drop_ambient_libpq_targets(environ: Any) -> list[str]:
    """Retire les cibles libpq ambiantes du process d'éval et renvoie leurs noms.

    L'éval passe toujours un DSN explicite (host, dbname et user obligatoires) ;
    sans ce nettoyage, le pool d'apps/api refuse de démarrer dès que le `.env`
    racine est chargé. Les valeurs ne sont jamais affichées.
    """
    dropped = [key for key in LIBPQ_TARGET_VARIABLES if environ.get(key)]
    for key in dropped:
        environ.pop(key, None)
    return dropped


def ministry_for_scope(retrieval_scope: Any) -> str:
    """Ministère API d'une question, depuis le scope legacy ``per-question``.

    Le pipeline API route toujours par ministère (pas de scope « all » ni
    « none ») ; les questions non ministérielles suivent le parcours MATTE,
    comme ``resolve_question_scope``.
    """
    ministry = str(getattr(retrieval_scope, "selected_ministry", "") or "").strip().lower()
    return ministry if ministry in MINISTRIES else DEFAULT_MINISTRY


def named_overrides(args: Any) -> list[tuple[str, Any]]:
    """Flags nommés renseignés -> ``[(chemin api, valeur)]``, dans l'ordre de la table."""
    overrides: list[tuple[str, Any]] = []
    for attr, path in NAMED_API_OVERRIDES.items():
        value = getattr(args, attr, None)
        if value is None or value == "":
            continue
        overrides.append((path, value))
    return overrides


def _adjustment_label(path: str, adjustment: str) -> str:
    """Ré-étiquette ``section.champ=v`` avec le nom du flag nommé (continuité legacy)."""
    for attr, named_path in NAMED_API_OVERRIDES.items():
        if named_path == path:
            return f"{attr}={adjustment.partition('=')[2]}"
    return adjustment


@dataclass
class EvalRunContext(RunContext):
    """``RunContext`` qui conserve le résultat brut de chaque étage.

    Pas de ``slots`` ici : le ``super()`` sans argument ne fonctionne pas dans
    une dataclass ``slots=True`` en Python 3.12.
    """

    captured: list[tuple[str, str, Any]] = field(default_factory=list)

    async def stage(self, name, operation, *, project, measure=None, attempt=""):  # type: ignore[override]
        result = await super().stage(name, operation, project=project, measure=measure, attempt=attempt)
        if name in _CAPTURED_STAGES:
            self.captured.append((name, attempt, result))
        return result


@dataclass
class EvalContextItem:
    """Item de contexte au format attendu par ``context_payload`` (métadonnées en dict)."""

    section_id: str | None
    heading: str | None
    content: str
    score: float
    publisher: str | None
    document_title: str | None
    document_url: str | None
    token_estimate: int
    metadata: dict[str, Any]


@dataclass
class ApiEvalResult:
    answer: str
    context_items: list[EvalContextItem]
    sources: list[dict[str, Any]]
    timing: dict[str, Any]
    metadata: dict[str, Any]


# Même ordre que _DOC_ID_METADATA_KEYS (legacy et apps/api).
_DOC_ID_KEYS = ("doc_id", "doc_short_id", "document_id", "source_document_id", "short_id", "cid")


def _doc_id(metadata: dict[str, Any]) -> str:
    for key in _DOC_ID_KEYS:
        value = metadata.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _context_item(item: Any) -> EvalContextItem:
    return EvalContextItem(
        section_id=None if item.section_id is None else str(item.section_id),
        heading=item.heading,
        content=item.content,
        score=float(item.score),
        publisher=item.publisher,
        document_title=item.document_title,
        document_url=item.document_url,
        token_estimate=int(item.token_estimate or 0),
        metadata=thaw(item.metadata) or {},
    )


def _context_ref(item: EvalContextItem) -> dict[str, Any]:
    return {
        "doc_id": _doc_id(item.metadata),
        "section_id": item.section_id or "",
        "heading": item.heading,
        "publisher": item.publisher,
        "document_title": item.document_title,
        "score": item.score,
        "token_estimate": item.token_estimate,
        "metadata": item.metadata,
    }


def _sources(items: list[EvalContextItem]) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in items:
        key = (_doc_id(item.metadata), item.document_url or item.document_title or "")
        if key in seen:
            continue
        seen.add(key)
        sources.append(
            {
                "doc_id": key[0],
                "title": item.document_title or item.heading or "",
                "url": item.document_url or "",
                "publisher": item.publisher or "",
            }
        )
    return sources


def _attempts(captured: list[tuple[str, str, Any]]) -> list[dict[str, Any]]:
    """Tentatives de retrieval au format legacy lu par ``stage_retrieval_metrics``."""
    attempts: dict[str, dict[str, Any]] = {}
    for stage, attempt_name, result in captured:
        if not attempt_name:
            continue
        attempt = attempts.setdefault(attempt_name, {"name": attempt_name})
        if stage == "retriever":
            # Forme du ``chunks_raw`` legacy : un seul doc_id canonique par chunk.
            attempt["chunks_raw"] = [
                {
                    "doc_id": _doc_id(thaw(chunk.metadata) or {}),
                    "chunk_id": str(chunk.chunk_id),
                    "section_id": str(chunk.section_id or ""),
                    "table": chunk.table_source,
                    "score": chunk.score,
                }
                for chunk in result.chunks
            ]
        elif stage == "section-aggregator":
            trace = thaw(aggregation_trace(result))
            diagnostics = trace.get("diagnostics") or {}
            attempt["chunks_before_rerank"] = diagnostics.get("chunks_before_rerank") or []
            attempt["chunks_after_rerank"] = diagnostics.get("chunks_after_rerank") or []
            attempt["aggregated_sections"] = [
                {**section, "document_id": section.get("document_id") or _doc_id(section.get("metadata") or {})}
                for section in trace.get("sections") or []
            ]
            attempt["aggregation"] = {
                key: diagnostics.get(key) for key in ("sections_before_rerank", "sections_after_rerank", "reranker_status", "reranker_fallback")
            }
        elif stage == "context-selector":
            trace = thaw(selection_trace(result))
            diagnostics = trace.get("diagnostics") or {}
            attempt["selector"] = {
                "status": diagnostics.get("status"),
                "decisions": diagnostics.get("decisions") or {},
                "reason": diagnostics.get("reason"),
                "prompt_chars": len(result.diagnostics.user_prompt or "")
                + len(result.diagnostics.prompt.value.content if result.diagnostics.prompt else ""),
                "response_chars": len(result.diagnostics.raw_response or ""),
                "items_before": None,
                "items_after": len(result.sections),
                "all_rejected": result.all_rejected,
            }
        elif stage == "context-builder":
            attempt["context_items_ref"] = [_context_ref(_context_item(item)) for item in result.items]
    return list(attempts.values())


def _generator_metadata(captured: list[tuple[str, str, Any]], config: RAGConfig) -> tuple[dict[str, Any], str, str]:
    configured_provider = config.generation.provider.value
    configured_model = config.generation.model
    metadata: dict[str, Any] = {
        "generator_provider": configured_provider,
        "generator_model": configured_model,
        "generator_provider_used": None,
        "generator_model_used": None,
        "generator_used_fallback": False,
        "generator_fallback_count": 0,
    }
    system_prompt = user_prompt = ""
    generated = next((result for stage, _, result in reversed(captured) if stage == "generator"), None)
    if generated is None:
        return metadata, system_prompt, user_prompt
    diagnostics = generated.diagnostics
    outcome = diagnostics.outcome
    metadata["generator_status"] = diagnostics.status
    if outcome is not None:
        metadata["generator_provider_used"] = outcome.provider
        metadata["generator_model_used"] = outcome.model
        metadata["generator_used_fallback"] = outcome.provider != configured_provider or outcome.model != configured_model
        metadata["generator_fallback_count"] = diagnostics.fallback_count
        if outcome.usage is not None:
            metadata["generator_usage"] = {
                "prompt_tokens": outcome.usage.prompt_tokens,
                "completion_tokens": outcome.usage.completion_tokens,
                "total_tokens": outcome.usage.total_tokens,
            }
    if diagnostics.request is not None:
        for message in diagnostics.request.messages:
            if message.role == "system":
                system_prompt = message.content
            elif message.role == "user":
                user_prompt = message.content
    return metadata, system_prompt, user_prompt


def project_result(
    result: Any, context: EvalRunContext, *, config: RAGConfig, ministry: str, config_revision: str = ""
) -> tuple[ApiEvalResult, str, str]:
    """Projette un ``PipelineResult`` API + les étages capturés au format legacy de l'éval.

    Renvoie ``(résultat, system_prompt, user_prompt)`` du générateur.
    """
    items = [_context_item(item) for item in result.items]
    attempts = _attempts(context.captured)
    latest = attempts[-1] if attempts else {}
    generator, system_prompt, user_prompt = _generator_metadata(context.captured, config)
    processing = next((value for stage, _, value in context.captured if stage == "query-processor"), None)
    query = thaw(query_trace(processing)).get("result", {}) if processing is not None else {}
    metadata: dict[str, Any] = {
        "pipeline": PIPELINE_NAME,
        "config_revision": config_revision,
        "selected_ministry": ministry,
        "turn_id": context.turn_id,
        "trace_id": context.trace_id,
        "intent": query.get("intent"),
        "query_for_retrieval": query.get("enriched_query") or query.get("processed_query"),
        "direct_response": bool(query.get("direct_response")),
        "selector_enabled": config.selector.enabled,
        "retrieval_attempts": attempts,
        "aggregated_sections": latest.get("aggregated_sections", []),
        # Même sémantique que le legacy : retrieved_doc_ids lit le contexte servi,
        # chunks_after_rerank ET chunks_raw (pool brut) ; hit_rate/doc_recall
        # gardent ainsi la même définition dans les deux modes.
        "context_items_ref": [_context_ref(item) for item in items],
        "chunks_raw": latest.get("chunks_raw", []),
        "chunks_before_rerank": latest.get("chunks_before_rerank", []),
        "chunks_after_rerank": latest.get("chunks_after_rerank", []),
        "selector_decisions": (latest.get("selector") or {}).get("decisions", {}),
        "selector_prompt_chars": sum(int((attempt.get("selector") or {}).get("prompt_chars") or 0) for attempt in attempts),
        "selector_response_chars": sum(int((attempt.get("selector") or {}).get("response_chars") or 0) for attempt in attempts),
        **{key: thaw(context.diagnostics.get(key)) for key in ("selector_retry_triggered", "selector_retry_succeeded", "selector_all_rejected")},
        **generator,
    }
    timing: dict[str, Any] = {}
    for event in context.events:
        key = f"{event.stage}_{event.attempt_name}_ms" if event.attempt_name else f"{event.stage}_ms"
        timing[key] = timing.get(key, 0) + event.duration_ms
    timing["total_ms"] = context.metrics(stream=False).elapsed_ms
    usage = metadata.get("generator_usage") or {}
    timing["response_length_tokens"] = int(usage.get("completion_tokens") or 0) or len(result.answer) // 4
    return (
        ApiEvalResult(answer=result.answer, context_items=items, sources=_sources(items), timing=timing, metadata=metadata),
        system_prompt,
        user_prompt,
    )


def _describe_failure(exc: BaseException) -> str:
    if isinstance(exc, InferenceFailure):
        attempts = ", ".join(f"{a.provider}/{a.model}:{a.error or 'ok'}:{a.status or '-'}" for a in exc.attempts) or "aucune tentative"
        return f"InferenceFailure ({attempts})"
    if isinstance(exc, ApplicationError):
        return f"{type(exc).__name__} ({exc.code})"
    return f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__


class ApiEvalPipeline:
    """Pipeline ``apps/api`` piloté de façon synchrone par la boucle d'éval.

    Une seule boucle asyncio (``asyncio.Runner``) vit le temps du run : le pool
    Postgres et le client HTTP restent liés à cette boucle entre deux questions.
    """

    def __init__(self, *, dsn: str, environ: dict[str, str] | Any) -> None:
        self._dsn = dsn
        self._environ = dict(environ)
        self._runner: asyncio.Runner | None = None
        self._database: Database | None = None
        self._client: httpx.AsyncClient | None = None
        self._clock = SystemClock()
        self._ids = RunIds()
        self._base_config: RAGConfig | None = None
        self.config: RAGConfig | None = None
        self.config_revision = ""
        self._pipeline: Any = None
        self.last_full_prompt = ""
        self.last_system_prompt = ""

    # -- cycle de vie -------------------------------------------------------
    def __enter__(self) -> ApiEvalPipeline:
        self._runner = asyncio.Runner()
        try:
            self._runner.run(self._open())
        except BaseException:
            self.close()
            raise
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    async def _open(self) -> None:
        # Réglages du pool de l'API, connexion plus tolérante pour une base distante.
        self._database = Database(DatabaseSettings(dsn=self._dsn, connect_timeout_seconds=10, timeout_seconds=30))
        await self._database.open()
        self._client = httpx.AsyncClient(trust_env=False)
        snapshot = await RAGConfigurationService(ConfigStore(self._database)).load()
        if snapshot.fallback is not None:
            # L'API retombe sur les défauts quand rag_config est illisible ; une éval
            # mesurerait alors une config que personne ne sert. On refuse.
            raise ApiPipelineError(f"rag_config illisible sur le DSN cible ({snapshot.fallback}) : éval refusée sur les valeurs par défaut")
        self._base_config = snapshot.config.value
        self.config = self._base_config
        self.config_revision = snapshot.config.revision

    async def _close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
        if self._database is not None:
            await self._database.close()

    def close(self) -> None:
        if self._runner is None:
            return
        try:
            self._runner.run(self._close())
        finally:
            self._runner.close()
            self._runner = None

    def _run(self, coro: Any) -> Any:
        if self._runner is None:
            raise RuntimeError("ApiEvalPipeline utilisé hors de son bloc `with`")
        return self._runner.run(coro)

    # -- configuration ------------------------------------------------------
    def configure(self, overrides: list[tuple[str, Any]]) -> tuple[RAGConfig, list[str]]:
        """Applique les surcharges à la config runtime lue et construit le pipeline."""
        if self._base_config is None or self._database is None or self._client is None:
            raise RuntimeError("ApiEvalPipeline non ouvert")
        config, raw_adjustments = apply_config_overrides(self._base_config, overrides)
        adjustments = [_adjustment_label(path, adjustment) for (path, _), adjustment in zip(overrides, raw_adjustments)]
        factory = create_pipeline_factory(self._database, self._client, self._environ, clock=self._clock)
        self._pipeline = factory(config)
        self.config = config
        return config, adjustments

    def prompt_exists(self, name: str) -> bool:
        """Prompt présent en base (ou packagé) et non vide, comme ``load_prompt``."""

        async def check() -> bool:
            assert self._database is not None
            for store in (PromptStore(self._database), PackagedPromptStore()):
                snapshot = await store.get(name)
                if snapshot is not None and snapshot.value.content:
                    return True
            return False

        return bool(self._run(check()))

    def generator_prompt_content(self) -> str:
        """Prompt générateur effectivement résolu (même chaîne de repli que l'API)."""
        if self.config is None or self._database is None:
            raise RuntimeError("ApiEvalPipeline non ouvert")
        name = self.config.generation.system_prompt_name

        async def resolve() -> str:
            assert self._database is not None
            loaded = await load_prompt(PromptStore(self._database), PackagedPromptStore(), name, "generator.md", DEFAULT_GENERATOR_PROMPT)
            return loaded.snapshot.value.content

        return str(self._run(resolve()))

    def generator_prompt_sha(self) -> str:
        return hashlib.sha256(self.generator_prompt_content().encode("utf-8")).hexdigest()

    # -- exécution ----------------------------------------------------------
    def new_context(self) -> EvalRunContext:
        created = self._clock.now().astimezone(UTC)
        return EvalRunContext(
            turn_id=self._ids.new_id(),
            trace_id=self._ids.new_id(),
            created=created,
            today=created.astimezone(ZoneInfo("Europe/Paris")).date().isoformat(),
            clock=self._clock,
        )

    def run_with_trace(self, question: str, retrieval_scope: Any = None) -> ApiEvalResult:
        if self._pipeline is None or self.config is None:
            raise RuntimeError("ApiEvalPipeline.configure() doit précéder run_with_trace()")
        ministry = ministry_for_scope(retrieval_scope)
        context = self.new_context()
        self.last_full_prompt = self.last_system_prompt = ""
        try:
            result = self._run(self._pipeline.run(ChatInput(model=EVAL_MODEL, question=question), ministry, context))
        except DatabaseUnavailable as exc:
            # Coupure DB transitoire : la boucle de retry de l'éval ne rattrape que psycopg.
            raise psycopg.OperationalError(_describe_failure(exc)) from exc
        except Exception as exc:
            raise ApiPipelineError(_describe_failure(exc)) from exc
        projected, system_prompt, user_prompt = project_result(
            result, context, config=self.config, ministry=ministry, config_revision=self.config_revision
        )
        self.last_system_prompt = system_prompt
        self.last_full_prompt = user_prompt
        return projected

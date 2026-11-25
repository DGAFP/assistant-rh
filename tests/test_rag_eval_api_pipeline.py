"""Mode ``--pipeline api`` de la CLI d'éval : surcharges de config et construction du pipeline apps/api.

Aucun accès DB ni provider : le pool Postgres et le store de config sont
remplacés par des doublures, le pipeline est le vrai ``Pipeline`` d'apps/api
composé avec les ports synthétiques de ``apps/api/tests/chat_fakes.py``.
"""

from __future__ import annotations

import asyncio
import dataclasses
from types import SimpleNamespace

import httpx
import psycopg
import pytest
from assistant_rh_api.core.errors import DatabaseUnavailable, InferenceFailure
from assistant_rh_api.core.models.inference import Attempt
from assistant_rh_api.core.models.rag_configuration import LLMProvider
from assistant_rh_api.core.models.rag_configuration import RAGConfig as ApiRAGConfig
from assistant_rh_api.core.pipeline.pipeline import Pipeline
from assistant_rh_rag_pipeline.config import RAGConfig as LegacyRAGConfig

import src.goldset.api_pipeline as api_module
import src.goldset.eval as eval_module
from apps.api.tests.auth_fakes import Clock
from apps.api.tests.chat_fakes import Runtime
from src.goldset.config_overrides import apply_config_overrides, parse_override
from src.goldset.eval import GoldsetQuestion, build_eval_scope, build_parser, context_payload, retrieved_doc_ids, run_question

DSN = "postgresql://eval:eval@localhost:5432/assistant_rh"

# -- surcharges de config --------------------------------------------------


def test_parse_override_requires_path_and_value() -> None:
    assert parse_override(" aggregation.rerank_input_k = 40 ") == ("aggregation.rerank_input_k", "40")
    for invalid in ("aggregation.rerank_input_k", "=40", ""):
        with pytest.raises(ValueError, match="SECTION.CHAMP=VALEUR"):
            parse_override(invalid)


def test_api_overrides_are_typed_and_never_mutate_the_runtime_config() -> None:
    runtime = ApiRAGConfig()
    config, adjustments = apply_config_overrides(
        runtime,
        [
            ("aggregation.rerank_input_k", "40"),
            ("selector.min_kept_sections", 4),
            ("selector.provider", "mistral"),
            ("selector.enabled", "false"),
            ("generation.temperature", 1),
            ("retrieval.tables", "matte,dgafp"),
        ],
    )
    assert config.aggregation.rerank_input_k == 40
    assert config.selector.min_kept_sections == 4
    assert config.selector.provider is LLMProvider.MISTRAL
    assert config.selector.enabled is False
    assert config.generation.temperature == 1.0 and isinstance(config.generation.temperature, float)
    assert config.retrieval.tables == ("matte", "dgafp")
    assert adjustments == [
        "aggregation.rerank_input_k=40",
        "selector.min_kept_sections=4",
        "selector.provider=mistral",
        "selector.enabled=False",
        "generation.temperature=1.0",
        "retrieval.tables=('matte', 'dgafp')",
    ]
    # La config runtime lue (figée) reste celle de rag_config.
    assert runtime == ApiRAGConfig()


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ([("selector.not_a_field", 1)], "champ inconnu 'selector.not_a_field'"),
        ([("nope.rerank_input_k", 1)], "section inconnue 'nope'"),
        ([("aggregation.rerank_input_k", "quarante")], "n'est pas une valeur int valide"),
        ([("aggregation.rerank_input_k", True)], "n'est pas du type int"),
        ([("selector.enabled", "peut-être")], "n'est pas une valeur bool valide"),
        ([("aggregation.rerank_input_k", 40), ("aggregation.rerank_input_k", 20)], "surchargé plusieurs fois"),
        ([("rerank_input_k", 40)], "format attendu SECTION.CHAMP"),
    ],
)
def test_invalid_overrides_fail_before_any_pipeline_call(overrides, message) -> None:
    with pytest.raises(ValueError, match=message):
        apply_config_overrides(ApiRAGConfig(), overrides)


def test_reranker_union_override_requires_a_branch_that_has_the_field() -> None:
    has_field = "reranker_union_top_k" in {field.name for field in dataclasses.fields(ApiRAGConfig().selector)}
    if has_field:
        config, adjustments = apply_config_overrides(ApiRAGConfig(), [("selector.reranker_union_top_k", 0)])
        assert config.selector.reranker_union_top_k == 0
        assert adjustments == ["selector.reranker_union_top_k=0"]
    else:
        with pytest.raises(ValueError, match="champ inconnu 'selector.reranker_union_top_k'"):
            apply_config_overrides(ApiRAGConfig(), [("selector.reranker_union_top_k", 0)])
    # Le legacy figé ne porte pas ce champ : refus explicite plutôt qu'un run qui l'ignore.
    with pytest.raises(ValueError, match="champ inconnu 'selector.reranker_union_top_k'"):
        apply_config_overrides(LegacyRAGConfig(), [("selector.reranker_union_top_k", 0)])


def test_generic_overrides_also_apply_to_the_legacy_config() -> None:
    config, adjustments = apply_config_overrides(LegacyRAGConfig(), [("aggregation.rerank_input_k", "40")])
    assert config.aggregation.rerank_input_k == 40
    assert adjustments == ["aggregation.rerank_input_k=40"]


def test_cli_named_flags_map_to_api_config_paths() -> None:
    args = build_parser().parse_args(
        [
            "--goldset-name",
            "baseline_v1",
            "--pipeline",
            "api",
            "--rerank-input-k",
            "40",
            "--min-kept-sections",
            "4",
            "--reranker-union-top-k",
            "0",
            "--selector-model",
            "deepseek-v4-flash",
            "--config-override",
            "context.token_budget=9000",
        ]
    )
    assert args.pipeline == "api"
    assert args.config_override == ["context.token_budget=9000"]
    assert api_module.named_overrides(args) == [
        ("selector.model", "deepseek-v4-flash"),
        ("aggregation.rerank_input_k", 40),
        ("selector.min_kept_sections", 4),
        ("selector.reranker_union_top_k", 0),
    ]
    assert build_parser().parse_args(["--goldset-name", "g"]).pipeline == "legacy"


# -- périmètre de comparabilité ----------------------------------------------


def _scope_args(**overrides):
    base = dict(
        limit=5,
        skip_ragas=True,
        ragas_model="",
        skip_judge=True,
        judge_model="",
        judge_provider="scaleway",
        judge_base_url=None,
        judge_votes=1,
        ministry_scope="per-question",
    )
    return SimpleNamespace(**{**base, **overrides})


def test_eval_scope_separates_api_runs_and_keeps_legacy_scope_unchanged() -> None:
    questions = [GoldsetQuestion(id=1, question="q", gold_answer="a", gold_sources=[])]
    legacy = build_eval_scope(_scope_args(), questions)
    assert "pipeline" not in legacy  # baselines historiques toujours comparables
    assert build_eval_scope(_scope_args(pipeline="legacy"), questions) == legacy
    assert build_eval_scope(_scope_args(pipeline="api"), questions) == {**legacy, "pipeline": "api"}


def test_api_mode_rejects_non_ministry_scopes_before_touching_the_database(monkeypatch) -> None:
    monkeypatch.setattr(eval_module, "resolve_dsn", lambda *a, **k: pytest.fail("DSN résolu avant la validation"))
    args = build_parser().parse_args(["--goldset-name", "g", "--pipeline", "api", "--ministry-scope", "all"])
    with pytest.raises(ValueError, match="per-question"):
        eval_module.run_eval(args)


# -- construction du pipeline apps/api ---------------------------------------


class FakeDatabase:
    instances: list[FakeDatabase] = []

    def __init__(self, settings) -> None:
        self.settings = settings
        self.opened = self.closed = False
        FakeDatabase.instances.append(self)

    async def open(self) -> None:
        self.opened = True

    async def close(self) -> None:
        self.closed = True


class FailingConfigStore:
    async def load(self):
        raise DatabaseUnavailable()


@pytest.fixture
def runtime(monkeypatch):
    """Doublures DB + vraie fabrique de pipeline composée de ports synthétiques."""
    FakeDatabase.instances = []
    fake = Runtime(v3_rerank_input_k=30)
    calls = []

    def factory(database, client, environ, *, clock):
        calls.append({"database": database, "client": client, "environ": environ, "clock": clock})

        def build(config):
            pipeline = fake.service._pipeline_factory(config)
            calls[-1]["config"] = config
            return pipeline

        return build

    monkeypatch.setattr(api_module, "Database", FakeDatabase)
    monkeypatch.setattr(api_module, "ConfigStore", lambda database: fake.config)
    monkeypatch.setattr(api_module, "create_pipeline_factory", factory)
    return SimpleNamespace(fake=fake, calls=calls)


def test_api_pipeline_is_built_from_runtime_config_with_cli_overrides(runtime) -> None:
    with api_module.ApiEvalPipeline(dsn=DSN, environ={"ALBERT_API_KEY": "k"}) as pipe:
        database = FakeDatabase.instances[-1]
        assert database.opened and database.settings.dsn == DSN
        assert pipe.config_revision == "configuration-revision"
        assert pipe.config.aggregation.rerank_input_k == 30  # lu dans rag_config

        config, adjustments = pipe.configure(
            [("aggregation.rerank_input_k", 40), ("selector.min_kept_sections", 2), ("context.token_budget", "9000")]
        )

        call = runtime.calls[-1]
        assert call["database"] is database
        assert isinstance(call["client"], httpx.AsyncClient)
        assert call["environ"] == {"ALBERT_API_KEY": "k"}
        assert call["config"] is config is pipe.config
        assert config.aggregation.rerank_input_k == 40 and config.selector.min_kept_sections == 2 and config.context.token_budget == 9000
        assert adjustments == ["rerank_input_k=40", "min_kept_sections=2", "context.token_budget=9000"]
        assert isinstance(pipe._pipeline, Pipeline)
    assert database.closed
    assert pipe._runner is None


def test_api_pipeline_refuses_default_config_when_rag_config_is_unreadable(monkeypatch, runtime) -> None:
    monkeypatch.setattr(api_module, "ConfigStore", lambda database: FailingConfigStore())
    with pytest.raises(api_module.ApiPipelineError, match="database_unavailable"):
        with api_module.ApiEvalPipeline(dsn=DSN, environ={}):
            pass
    assert FakeDatabase.instances[-1].closed


def test_run_with_trace_projects_api_result_into_the_legacy_eval_shape(runtime) -> None:
    with api_module.ApiEvalPipeline(dsn=DSN, environ={}) as pipe:
        pipe.configure([])
        result = pipe.run_with_trace("Quels congés ?", retrieval_scope=SimpleNamespace(selected_ministry="mso"))

    assert result.answer.startswith("Réponse")
    assert result.metadata["pipeline"] == "api"
    assert result.metadata["selected_ministry"] == "mso"
    assert result.metadata["config_revision"] == "configuration-revision"
    assert result.metadata["generator_provider_used"] == "albert"
    assert result.metadata["generator_used_fallback"] is True  # modèle servi ≠ modèle configuré
    assert pipe.last_system_prompt.startswith("GENERATE")
    assert pipe.last_full_prompt

    contexts = context_payload(result)
    assert contexts and contexts[0]["doc_id"] == "guide-mso"
    assert isinstance(contexts[0]["metadata"], dict)
    assert "guide-mso" in retrieved_doc_ids(result, contexts)

    (attempt,) = result.metadata["retrieval_attempts"]
    assert attempt["name"] == "initial"
    assert {chunk["doc_id"] for chunk in attempt["chunks_raw"]} == {"guide-mso"}
    stages = eval_module.stage_retrieval_metrics(result.metadata, ["guide-mso"])
    assert set(stages["initial"]) >= {"pool", "selector_kept", "context_builder_output"}
    assert any(stage.startswith("sections_top") for stage in stages["initial"])
    assert all(stage["hit_rate"] == 1.0 for stage in stages["initial"].values())
    assert result.timing["total_ms"] >= 0 and "generator_ms" in result.timing


def test_non_ministry_questions_follow_the_matte_route() -> None:
    assert api_module.ministry_for_scope(None) == "matte"
    assert api_module.ministry_for_scope(SimpleNamespace(selected_ministry="eval_all_ministries")) == "matte"
    assert api_module.ministry_for_scope(SimpleNamespace(selected_ministry="MASA")) == "masa"


def test_run_question_scores_the_api_pipeline_like_the_legacy_one(runtime) -> None:
    question = GoldsetQuestion(id=7, question="Quels congés ?", gold_answer="a", gold_sources=["guide-matte"], source="manual")
    with api_module.ApiEvalPipeline(dsn=DSN, environ={}) as pipe:
        pipe.configure([])
        item = run_question(
            pipe=pipe,
            question=question,
            run_ragas=False,
            run_judge=False,
            judge_model="",
            judge_base_url="",
            judge_api_key="",
            ragas_model="",
            scaleway_base_url="",
            scaleway_api_key="",
            retrieval_scope=eval_module.resolve_question_scope(question, "per-question"),
        )
    assert item.error == ""
    assert item.metadata["selected_ministry"] == "matte"
    assert item.metadata["generator_prompt_chars"] > 0
    assert item.deterministic_metrics["hit_rate"] == 1.0
    assert item.deterministic_metrics["stages"]["initial"]["context_builder_output"]["hit_rate"] == 1.0


def test_pipeline_failures_become_non_empty_item_errors_and_db_cuts_stay_retryable(runtime) -> None:
    with api_module.ApiEvalPipeline(dsn=DSN, environ={}) as pipe:
        pipe.configure([])
        runtime.fake.llm.failure = InferenceFailure((Attempt("albert", "openweight-large", "timeout", None),))
        with pytest.raises(api_module.ApiPipelineError, match="InferenceFailure \\(albert/openweight-large:timeout"):
            pipe.run_with_trace("q")
        runtime.fake.llm.failure = DatabaseUnavailable()
        with pytest.raises(psycopg.OperationalError, match="DatabaseUnavailable"):
            pipe.run_with_trace("q")


def test_create_pipeline_factory_composes_the_served_pipeline() -> None:
    from assistant_rh_api.bootstrap import create_pipeline_factory

    async def build() -> Pipeline:
        async with httpx.AsyncClient(trust_env=False) as client:
            return create_pipeline_factory(object(), client, {"ALBERT_API_KEY": "a", "SCALEWAY_API_KEY": "k"}, clock=Clock())(ApiRAGConfig())

    assert isinstance(asyncio.run(build()), Pipeline)


def test_unconfigured_api_pipeline_cannot_run() -> None:
    pipe = api_module.ApiEvalPipeline(dsn=DSN, environ={})
    with pytest.raises(RuntimeError, match="configure"):
        pipe.run_with_trace("q")


def test_ambient_libpq_targets_are_dropped_because_the_eval_dsn_is_explicit() -> None:
    environ = {"PGHOST": "elsewhere", "PGUSER": "someone", "PGPASSWORD": "kept", "PGPORT": "", "ALBERT_API_KEY": "k"}
    assert api_module.drop_ambient_libpq_targets(environ) == ["PGHOST", "PGUSER"]
    assert environ == {"PGPASSWORD": "kept", "PGPORT": "", "ALBERT_API_KEY": "k"}


def _legacy_runtime(monkeypatch) -> None:
    monkeypatch.setattr(eval_module, "get_rag_config", lambda: object())
    monkeypatch.setattr(eval_module, "runtime_config_to_rag_config", lambda runtime: LegacyRAGConfig())
    monkeypatch.setattr(eval_module, "get_prompt_content", lambda name, render_today=False: "PROMPT")


def test_legacy_mode_applies_generic_overrides_after_named_flags(monkeypatch) -> None:
    _legacy_runtime(monkeypatch)
    args = build_parser().parse_args(["--goldset-name", "g", "--min-kept-sections", "4", "--config-override", "context.token_budget=9000"])
    config, adjustments, prompt = eval_module._configure_legacy(args)
    assert config.selector.min_kept_sections == 4 and config.context.token_budget == 9000
    assert adjustments == ["min_kept_sections=4", "context.token_budget=9000"]
    assert prompt == "PROMPT"


def test_legacy_mode_refuses_the_api_only_reranker_union(monkeypatch) -> None:
    _legacy_runtime(monkeypatch)
    args = build_parser().parse_args(["--goldset-name", "g", "--reranker-union-top-k", "0"])
    with pytest.raises(ValueError, match="champ inconnu 'selector.reranker_union_top_k'"):
        eval_module._configure_legacy(args)

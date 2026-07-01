from types import SimpleNamespace

import pytest
from assistant_rh_rag_pipeline.config import SearchMode
from assistant_rh_rag_pipeline.ministry_scope import MinistryScopeError, build_retrieval_scope
from assistant_rh_rag_pipeline.pipeline import Pipeline, _RetrievalAttempt, _RunState
from assistant_rh_rag_pipeline.retriever import Retriever

from src.ui.user_groups_store import resolve_group_retrieval_scope, validate_ministry_policy


def test_mso_scope_resolves_to_ministry_plus_shared_tables() -> None:
    scope = build_retrieval_scope("mso")

    assert scope.selected_ministry == "mso"
    assert scope.table_keys == ("mso", "service_public", "dgafp")
    assert scope.include_chunks_test is False


def test_unknown_ministry_fails_closed() -> None:
    with pytest.raises(MinistryScopeError):
        build_retrieval_scope("old-ministry")


@pytest.mark.parametrize(
    ("allowed", "default", "is_valid"),
    [
        (["matte", "mso"], "mso", True),
        (["matte", "old-ministry"], "matte", False),
        (["matte"], "mso", False),
        ([], "matte", False),
    ],
)
def test_group_ministry_policy_validation(allowed: list[str], default: str, is_valid: bool) -> None:
    ok, _error, normalized_allowed, normalized_default = validate_ministry_policy(allowed, default)

    assert ok is is_valid
    if is_valid:
        assert normalized_allowed == allowed
        assert normalized_default == default


def test_group_scope_resolver_rejects_unallowed_selected_ministry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "src.ui.user_groups_store.get_group_policy",
        lambda _slug: {
            "valid": True,
            "error": "",
            "allowed_ministries": ["matte"],
            "default_ministry": "matte",
        },
    )

    scope, error = resolve_group_retrieval_scope("group-a", "mso")

    assert scope is None
    assert "pas autorisé" in error


def test_pipeline_scoped_retrieval_uses_scope_tables_and_disables_chunks_test(monkeypatch: pytest.MonkeyPatch) -> None:
    pipe = Pipeline.__new__(Pipeline)
    pipe.config = SimpleNamespace(
        retrieval=SimpleNamespace(
            search_mode=SearchMode.SEMANTIC,
            initial_top_k=3,
            enable_selector_retry=True,
            selector_retry_search_mode=SearchMode.HYBRID,
            selector_retry_top_k=5,
        )
    )
    pipe._retriever = SimpleNamespace(config=SimpleNamespace(tables=["matte", "service_public", "dgafp", "rgrh"], enable_chunks_test=True))
    calls: list[dict] = []

    def fake_attempt(**kwargs):
        calls.append(kwargs)
        return _RetrievalAttempt(
            name=kwargs["name"],
            search_mode=kwargs["search_mode"],
            top_k=kwargs["top_k"],
            tables_searched=list(kwargs["active_tables"]),
            context_items_ref=[{"section_id": "s1"}],
            selector_all_rejected=False,
        )

    def fake_set_latest(state: _RunState, latest: _RetrievalAttempt, attempts: list[_RetrievalAttempt]) -> None:
        state.stage_refs["_latest_context_items"] = ["context"]
        state.stage_refs["tables_searched"] = latest.tables_searched

    monkeypatch.setattr(pipe, "_run_retrieval_attempt", fake_attempt)
    monkeypatch.setattr(pipe, "_set_latest_attempt_state", fake_set_latest)

    scope = build_retrieval_scope("mso")
    result = pipe._retrieve_and_build(
        SimpleNamespace(query_for_retrieval="question"),
        _RunState(),
        retrieval_scope=scope,
    )

    assert result == ["context"]
    assert calls[0]["active_tables"] == ["mso", "service_public", "dgafp"]
    assert calls[0]["force_hybrid_tables"] == {"dgafp"}
    assert calls[0]["include_chunks_test"] is False
    assert calls[0]["strict_table_errors"] is True


def test_retriever_strict_unknown_table_key_fails() -> None:
    retriever = Retriever.__new__(Retriever)
    retriever.config = SimpleNamespace(
        tables=["unknown"],
        enable_chunks_test=False,
        search_mode=SearchMode.SEMANTIC,
        initial_top_k=1,
    )

    with pytest.raises(ValueError):
        retriever.retrieve("question", strict_table_errors=True)

"""The M1 bridge must feed the same evaluation metrics with real core outputs."""

import asyncio

import pytest
from assistant_rh_rag_pipeline.ministry_scope import build_retrieval_scope

from apps.api.tests.chat_fakes import Runtime
from scripts.conformance.m1_runtime import CoreEvaluator
from src.goldset.eval import context_payload, retrieved_doc_ids, stage_retrieval_metrics


@pytest.mark.anyio
async def test_core_eval_bridge_preserves_document_ids_and_request_isolation():
    runtime = Runtime()
    loop = asyncio.get_running_loop()

    async def run(ministry):
        adapter = CoreEvaluator(runtime.service, loop, runtime.clock, "local-test")
        return await asyncio.to_thread(adapter.run_with_trace, "Question " + ministry, retrieval_scope=build_retrieval_scope(ministry))

    matte, mi = await asyncio.gather(run("matte"), run("mi"))
    for result, ministry, other in ((matte, "matte", "mi"), (mi, "mi", "matte")):
        contexts = context_payload(result)
        ids = retrieved_doc_ids(result, contexts)
        assert "guide-" + ministry in ids and "guide-" + other not in ids
        assert stage_retrieval_metrics(result.metadata, ["guide-" + ministry])
        saved = runtime.runs.rows[result.metadata["turn_id"]]
        assert saved.selected_ministry == ministry and saved.group_slug == "m1-core-" + ministry
        assert result.metadata["generator_model_used"] == "synthetic"
        assert result.metadata["generator_used_fallback"] is False

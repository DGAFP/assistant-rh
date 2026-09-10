"""Compare unchanged M0b stage outputs using documented reconstructed LLM inputs.

M0b did not record raw LLM replies, acronym snapshots or DB prompt content.
The separate port fixture reconstructs only the observable response fields;
this verifies stage parity, not provenance of the original model invocation.
Full parsing and prompt parity are additionally covered by differential tests.
"""

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from assistant_rh_api.core.inference import Completion
from assistant_rh_api.core.models.configuration import Snapshot
from assistant_rh_api.core.models.rag_configuration import QueryProcessorConfig
from assistant_rh_api.core.pipeline.steps.query_processor import QueryProcessor
from assistant_rh_api.gateways.packaged_prompts import PackagedPromptStore

ROOT = Path(__file__).resolve().parents[4]
BASELINE = ROOT / "tests/conformance/baselines/m0-api-parity-dev-9bf1cf0"
RESPONSES = json.loads((ROOT / "apps/api/tests/fixtures/query_processor_m0b_ports.json").read_text())
MANIFEST = json.loads((BASELINE / "manifest.json").read_text())


@pytest.mark.anyio
@pytest.mark.parametrize("name", sorted(RESPONSES))
async def test_m0b_query_processor_exact_stage_output(name):
    fixture = json.loads((BASELINE / name / "00_input.json").read_text())
    stage = json.loads((BASELINE / name / "01_query_processor.json").read_text())
    pipeline = json.loads((BASELINE / name / "07_pipeline_result.json").read_text())
    acronyms = AsyncMock()
    acronyms.load.return_value = Snapshot((), "reconstructed-empty-acronyms", "default")
    prompts = AsyncMock()
    prompts.get.return_value = None
    llm = AsyncMock()
    llm.complete.return_value = Completion(json.dumps(RESPONSES[name]), "albert", "openweight-medium", ())
    processor = QueryProcessor(QueryProcessorConfig(**MANIFEST["pipeline_config"]["query_processor"]), acronyms, prompts, PackagedPromptStore(), llm)
    outcome = await processor.process(fixture["query"], fixture["conversation_history"], fixture["ministry"], today="2026-09-01")
    result = outcome.result
    actual = {
        "input": {"query": fixture["query"], "conversation_history": fixture["conversation_history"]},
        "output": {
            "intent": result.intent.value,
            "theme": result.theme,
            "needs_legal_search": result.needs_legal_search,
            "needs_legal_search_llm": result.needs_legal_search_llm,
            "should_proceed": result.should_proceed,
            "processed_query": result.processed_query,
            "enriched_query": result.enriched_query,
            "query_for_retrieval": result.query_for_retrieval,
            "search_terms": [],
        },
    }
    # Compare serialized JSON as well: False must not accidentally equal 0.
    assert json.dumps(actual, sort_keys=True, ensure_ascii=False) == json.dumps(stage, sort_keys=True, ensure_ascii=False)
    if not result.should_proceed:
        assert result.direct_response == pipeline["answer"]
    else:
        assert result.intent_confidence == pipeline["metadata"]["intent_confidence"]
        assert list(result.expanded_acronyms) == pipeline["metadata"]["expanded_acronyms"]
    assert llm.complete.await_count == 1

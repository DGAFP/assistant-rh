from __future__ import annotations

from scripts.backfill_ragas import build_run_aggregate_updates


def test_build_run_aggregate_updates_persists_usage_from_all_components() -> None:
    rows = [
        {
            "answer": "Réponse",
            "deterministic_metrics": {},
            "ragas_metrics": {
                "status": "completed",
                "faithfulness": 0.9,
                "context_precision": 0.8,
                "context_recall": 0.7,
                "usage": {
                    "prompt_tokens": 1_000,
                    "completion_tokens": 100,
                    "calls": 3,
                    "model": "llama-3.3-70b-instruct",
                    "provider": "scaleway",
                    "cost_eur": 0.00099,
                    "capture_complete": True,
                },
            },
            "judge_result": {"status": "skipped", "reason": "disabled"},
            "timing": {"response_length_tokens": 40},
            "metadata": {
                "generator_prompt_chars": 400,
                "selector_prompt_chars": 200,
                "selector_response_chars": 20,
            },
            "error": "",
        }
    ]

    updates, status = build_run_aggregate_updates(rows)

    assert status == "completed"
    assert updates["ragas_status_counts"]["completed"] == 1
    assert updates["token_usage"]["ragas"]["prompt_tokens"] == 1_000
    assert updates["token_usage"]["ragas"]["calls"] == 3
    assert updates["token_usage"]["generator_albert_est"]["prompt_tokens"] == 100
    assert updates["token_usage"]["selector_albert_est"] == {
        "prompt_tokens": 50,
        "completion_tokens": 5,
        "cost_eur": 0.0,
    }
    assert updates["token_usage"]["billable_cost_eur"] == 0.001

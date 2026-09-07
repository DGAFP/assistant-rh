#!/usr/bin/env python3
"""Generate human-review CSV answers from Python and/or an API candidate.

The input may be a plain text file with one question per line, a CSV file, or a
JSONL file. The output is a CSV designed for reviewers: original question first,
then Python answer/metadata columns, then candidate answer/metadata columns.

Examples:

  # Python only
  uv run python scripts/generate_human_review_answers.py \
    --input "~/Downloads/assistant_rh_questions.txt" \
    --output tmp/assistant_rh_answers_python.csv \
    --pipeline python

  # Python + local OpenAI-compatible candidate endpoint
  uv run python scripts/generate_human_review_answers.py \
    --input "~/Downloads/assistant_rh_questions.txt" \
    --output tmp/assistant_rh_answers_python_candidate.csv \
    --pipeline both \
    --candidate-base-url http://localhost:8000 \
    --candidate-model assistant-rh
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import requests
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(REPO_ROOT / ".env")

PipelineChoice = Literal["python", "candidate", "both"]


@dataclass(frozen=True)
class Question:
    id: str
    question: str


@dataclass(frozen=True)
class PipelineRun:
    answer: str
    metadata: dict[str, Any]
    sources: list[dict[str, Any]]
    timing: dict[str, Any]
    error: str = ""


CSV_COLUMNS = [
    "id",
    "question",
    "python_answer",
    "python_metadata_json",
    "python_sources_json",
    "python_timing_json",
    "python_error",
    "candidate_answer",
    "candidate_metadata_json",
    "candidate_sources_json",
    "candidate_timing_json",
    "candidate_error",
]


def _json_dumps(value: Any) -> str:
    if value in (None, ""):
        return ""
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _expand_path(value: str | Path) -> Path:
    return Path(value).expanduser().resolve()


def read_questions(path: Path, question_column: str = "question", id_column: str = "id") -> list[Question]:
    suffix = path.suffix.lower()
    if suffix == ".jsonl":
        return _read_questions_jsonl(path, question_column=question_column, id_column=id_column)
    if suffix == ".csv":
        return _read_questions_csv(path, question_column=question_column, id_column=id_column)
    return _read_questions_text(path)


def _read_questions_text(path: Path) -> list[Question]:
    questions: list[Question] = []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        for line in handle:
            question = line.strip()
            if not question:
                continue
            questions.append(Question(id=f"q{len(questions) + 1:03d}", question=question))
    return questions


def _read_questions_jsonl(path: Path, *, question_column: str, id_column: str) -> list[Question]:
    questions: list[Question] = []
    with path.open("r", encoding="utf-8-sig") as handle:
        for line_number, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            raw = json.loads(stripped)
            if not isinstance(raw, dict):
                raise ValueError(f"Expected JSON object at {path}:{line_number}")
            question = str(raw.get(question_column) or raw.get("query") or "").strip()
            if not question:
                raise ValueError(f"Missing question at {path}:{line_number}")
            question_id = str(raw.get(id_column) or f"q{len(questions) + 1:03d}")
            questions.append(Question(id=question_id, question=question))
    return questions


def _read_questions_csv(path: Path, *, question_column: str, id_column: str) -> list[Question]:
    questions: list[Question] = []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            return []
        fallback_column = reader.fieldnames[0]
        for row_number, row in enumerate(reader, start=2):
            question = str(row.get(question_column) or row.get("query") or row.get(fallback_column) or "").strip()
            if not question:
                continue
            question_id = str(row.get(id_column) or f"q{len(questions) + 1:03d}")
            questions.append(Question(id=question_id, question=question))
    return questions


def validate_python_provider_environment() -> None:
    """Fail fast when provider config would fall through to api.openai.com.

    The pipeline LLM client only passes ``base_url`` to the OpenAI SDK when the
    provider-specific URL env var is set. Without these URLs, Albert/Scaleway
    API keys are sent to OpenAI and fail with misleading 401 errors. Keep the
    URLs in `.env` or the shell environment rather than hardcoding
    environment-specific infrastructure details in this script.
    """

    required = ["ALBERT_API_KEY", "ALBERT_BASE_URL", "SCALEWAY_API_KEY", "SCALEWAY_BASE_URL"]
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        formatted = ", ".join(missing)
        raise RuntimeError(
            f"Python pipeline provider configuration is incomplete. Missing: {formatted}. Add these variables to .env or the shell environment."
        )


def create_python_pipeline() -> Any:
    validate_python_provider_environment()

    from assistant_rh_rag_pipeline import create_pipeline

    return create_pipeline()


def run_python_pipeline(pipe: Any, question: str) -> PipelineRun:
    started = time.perf_counter()
    result = pipe.run(question)
    elapsed_ms = (time.perf_counter() - started) * 1000
    timing = dict(result.timing)
    timing.setdefault("pipeline_total_ms", elapsed_ms)
    return PipelineRun(
        answer=result.answer,
        metadata=dict(result.metadata),
        sources=list(result.sources),
        timing=timing,
    )


def run_candidate_pipeline(*, question: str, base_url: str, model: str, api_key: str | None, timeout_s: int) -> PipelineRun:
    url = f"{base_url.rstrip('/')}/v1/chat/completions"
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": question}],
        "stream": False,
    }

    started = time.perf_counter()
    response = requests.post(url, headers=headers, json=payload, timeout=timeout_s)
    elapsed_ms = (time.perf_counter() - started) * 1000
    response.raise_for_status()
    body = response.json()

    choice = _first_choice(body)
    message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
    answer = str(message.get("content") or "")

    metadata = {}
    if isinstance(body.get("metadata"), dict):
        metadata.update(body["metadata"])
    if isinstance(message.get("metadata"), dict):
        metadata.update(message["metadata"])

    sources: list[dict[str, Any]] = []
    for key in ("sources", "context_items", "contextItems"):
        candidate_sources = metadata.get(key) or body.get(key)
        if isinstance(candidate_sources, list):
            sources = [item for item in candidate_sources if isinstance(item, dict)]
            break

    return PipelineRun(
        answer=answer,
        metadata=metadata,
        sources=sources,
        timing={"pipeline_total_ms": elapsed_ms},
    )


def _first_choice(body: dict[str, Any]) -> dict[str, Any]:
    choices = body.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        return choices[0]
    return {}


def error_run(exc: BaseException) -> PipelineRun:
    return PipelineRun(answer="", metadata={}, sources=[], timing={}, error=str(exc))


def build_row(question: Question, python_run: PipelineRun | None, candidate_run: PipelineRun | None) -> dict[str, str]:
    return {
        "id": question.id,
        "question": question.question,
        "python_answer": python_run.answer if python_run else "",
        "python_metadata_json": _json_dumps(python_run.metadata if python_run else None),
        "python_sources_json": _json_dumps(python_run.sources if python_run else None),
        "python_timing_json": _json_dumps(python_run.timing if python_run else None),
        "python_error": python_run.error if python_run else "",
        "candidate_answer": candidate_run.answer if candidate_run else "",
        "candidate_metadata_json": _json_dumps(candidate_run.metadata if candidate_run else None),
        "candidate_sources_json": _json_dumps(candidate_run.sources if candidate_run else None),
        "candidate_timing_json": _json_dumps(candidate_run.timing if candidate_run else None),
        "candidate_error": candidate_run.error if candidate_run else "",
    }


def write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate human-review CSV answers from Assistant RH pipelines.")
    parser.add_argument("--input", type=Path, required=True, help="Text, CSV, or JSONL questions file.")
    parser.add_argument("--output", type=Path, required=True, help="Output CSV path.")
    parser.add_argument("--pipeline", choices=["python", "candidate", "both"], default="both", help="Pipeline(s) to run.")
    parser.add_argument("--question-column", default="question", help="Question column for CSV/JSONL input. Also accepts 'query'.")
    parser.add_argument("--id-column", default="id", help="ID column for CSV/JSONL input.")
    parser.add_argument("--limit", type=int, default=None, help="Maximum number of questions to process.")
    parser.add_argument("--candidate-base-url", default="http://localhost:8000", help="OpenAI-compatible candidate base URL.")
    parser.add_argument("--candidate-model", default="assistant-rh", help="Model name sent to the candidate endpoint.")
    parser.add_argument("--candidate-api-key", default=None, help="Optional candidate API key. Defaults to OPENAI_API_KEY if set.")
    parser.add_argument("--timeout-s", type=int, default=180, help="HTTP timeout for candidate requests.")
    parser.add_argument("--continue-on-error", action="store_true", help="Keep writing rows when one pipeline call fails.")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    input_path = _expand_path(args.input)
    output_path = _expand_path(args.output)

    questions = read_questions(input_path, question_column=args.question_column, id_column=args.id_column)
    if args.limit is not None:
        questions = questions[: args.limit]
    if not questions:
        raise SystemExit("No questions found")

    should_run_python = args.pipeline in {"python", "both"}
    should_run_candidate = args.pipeline in {"candidate", "both"}
    candidate_api_key = args.candidate_api_key or os.getenv("OPENAI_API_KEY")

    python_pipe = create_python_pipeline() if should_run_python else None

    failed = False
    total = len(questions)
    print(f"Generating {total} row(s) at {output_path}", file=sys.stderr)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()

        for index, question in enumerate(questions, start=1):
            print(f"[{index}/{total}] {question.id}: {question.question[:90]}", file=sys.stderr)
            python_run = None
            candidate_run = None

            if should_run_python:
                try:
                    assert python_pipe is not None
                    python_run = run_python_pipeline(python_pipe, question.question)
                except Exception as exc:  # pragma: no cover - environment dependent
                    failed = True
                    python_run = error_run(exc)
                    if not args.continue_on_error:
                        raise

            if should_run_candidate:
                try:
                    candidate_run = run_candidate_pipeline(
                        question=question.question,
                        base_url=args.candidate_base_url,
                        model=args.candidate_model,
                        api_key=candidate_api_key,
                        timeout_s=args.timeout_s,
                    )
                except Exception as exc:  # pragma: no cover - environment dependent
                    failed = True
                    candidate_run = error_run(exc)
                    if not args.continue_on_error:
                        raise

            writer.writerow(build_row(question, python_run, candidate_run))
            handle.flush()

    print(
        json.dumps(
            {
                "generated_at": datetime.now(tz=UTC).isoformat(),
                "output": str(output_path),
                "question_count": len(questions),
                "pipeline": args.pipeline,
                "had_errors": failed,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

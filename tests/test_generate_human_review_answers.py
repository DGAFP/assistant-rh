from __future__ import annotations

import csv
import importlib.util
import json
import sys
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "generate_human_review_answers.py"
spec = importlib.util.spec_from_file_location("generate_human_review_answers", SCRIPT_PATH)
assert spec is not None and spec.loader is not None
generate_human_review_answers = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = generate_human_review_answers
spec.loader.exec_module(generate_human_review_answers)

PipelineRun = generate_human_review_answers.PipelineRun
Question = generate_human_review_answers.Question
build_row = generate_human_review_answers.build_row
read_questions = generate_human_review_answers.read_questions
write_csv = generate_human_review_answers.write_csv


def test_read_questions_from_plain_text_skips_blank_lines(tmp_path: Path):
    input_path = tmp_path / "questions.txt"
    input_path.write_text("Première question ?\n\nDeuxième question ?\n", encoding="utf-8")

    assert read_questions(input_path) == [
        Question(id="q001", question="Première question ?"),
        Question(id="q002", question="Deuxième question ?"),
    ]


def test_read_questions_from_csv_uses_question_and_id_columns(tmp_path: Path):
    input_path = tmp_path / "questions.csv"
    input_path.write_text("id,question\ncustom-1,Question CSV ?\n", encoding="utf-8")

    assert read_questions(input_path) == [Question(id="custom-1", question="Question CSV ?")]


def test_read_questions_from_jsonl_accepts_query_key(tmp_path: Path):
    input_path = tmp_path / "questions.jsonl"
    input_path.write_text(json.dumps({"id": "j1", "query": "Question JSONL ?"}, ensure_ascii=False) + "\n", encoding="utf-8")

    assert read_questions(input_path) == [Question(id="j1", question="Question JSONL ?")]


def test_build_row_serializes_review_columns_as_json():
    question = Question(id="q001", question="Question ?")
    python_run = PipelineRun(
        answer="Réponse Python",
        metadata={"intent": "rag_query"},
        sources=[{"title": "Source A"}],
        timing={"pipeline_total_ms": 12.3},
    )
    mastra_run = PipelineRun(
        answer="Réponse Mastra",
        metadata={"model": "openweight-medium"},
        sources=[],
        timing={"pipeline_total_ms": 45.6},
    )

    row = build_row(question, python_run, mastra_run)

    assert row["question"] == "Question ?"
    assert row["python_answer"] == "Réponse Python"
    assert json.loads(row["python_metadata_json"]) == {"intent": "rag_query"}
    assert json.loads(row["python_sources_json"]) == [{"title": "Source A"}]
    assert row["mastra_answer"] == "Réponse Mastra"
    assert json.loads(row["mastra_metadata_json"]) == {"model": "openweight-medium"}


def test_write_csv_outputs_human_review_columns(tmp_path: Path):
    output_path = tmp_path / "answers.csv"
    row = build_row(
        Question(id="q001", question="Question ?"),
        PipelineRun(answer="Python", metadata={}, sources=[], timing={}),
        None,
    )

    write_csv(output_path, [row])

    with output_path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))

    assert rows[0]["id"] == "q001"
    assert rows[0]["question"] == "Question ?"
    assert rows[0]["python_answer"] == "Python"
    assert "mastra_answer" in rows[0]

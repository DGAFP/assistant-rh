#!/usr/bin/env python3
"""Rejeu et diagnostic de la campagne Grist « Suivi-Tests » (issue #298).

Deux modes :

  run     Rejoue les questions de la table Grist ``Manuel_testing`` sur la
          config runtime lue en base (comme l'app), logge chaque tour dans
          ``chat_runs``/``rag_trace_events`` avec les tags de campagne, et
          affiche le diagnostic au fil de l'eau.

  report  Diagnostique une campagne DÉJÀ loggée dans ``chat_runs`` (aucun
          appel LLM) : pour chaque question, indique l'étape du pipeline où
          le document attendu disparaît (retrieval → rerank → agrégation →
          selector → sources finales).

Exemples :

  # Diagnostiquer la campagne du 08/07 sur staging
  uv run python scripts/suivi_tests_replay.py report \
      --session-id suivi-tests-20260708 --dsn-env SCW_POSTGRES_DSN_STAGING

  # Rejouer 3 questions sur staging sans logger
  uv run python scripts/suivi_tests_replay.py run \
      --dsn-env SCW_POSTGRES_DSN_STAGING --ids 61 63 47 --dry-run

Le mapping question → documents attendus vient de la colonne Grist
``expected_docs`` si elle existe, sinon de ``config/suivi_tests_expected_docs.json``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.suivi_tests.campaign import (  # noqa: E402
    DEFAULT_EXPECTED_CONFIG,
    CampaignQuestion,
    fetch_campaign_questions,
    load_expected_docs,
)
from src.suivi_tests.diagnose import diagnose_run, format_diagnosis_line  # noqa: E402

DEFAULT_DSN_ENV = "SCW_POSTGRES_DSN_STAGING"
CAMPAIGN_USER_GROUP = "suivi-tests"


def _resolve_dsn(dsn_env: str) -> str:
    dsn = os.getenv(dsn_env, "").strip()
    if not dsn:
        raise SystemExit(f"Variable d'environnement {dsn_env} absente ou vide (voir .env).")
    return dsn


def _print_summary(verdicts: dict[str, int]) -> None:
    total = sum(verdicts.values())
    print(f"\n== Synthèse ({total} questions) ==")
    for verdict, count in sorted(verdicts.items(), key=lambda item: -item[1]):
        print(f"  {verdict:<45} {count}")


# ─────────────────────────────────────────────────────────────────────────────
# report — diagnostic d'une campagne existante dans chat_runs
# ─────────────────────────────────────────────────────────────────────────────


def cmd_report(args: argparse.Namespace) -> int:
    import sqlalchemy as sa

    engine = sa.create_engine(_resolve_dsn(args.dsn_env))
    expected = load_expected_docs(Path(args.expected_config))

    with engine.connect() as conn:
        rows = (
            conn.execute(
                sa.text(
                    """
                    SELECT conversation_id, question, v3_chunks_raw, v3_chunks_after_rerank,
                           v3_context_items_full, v3_selector_kept_indices, retrieved
                    FROM chat_runs WHERE session_id = :sid ORDER BY ts
                    """
                ),
                {"sid": args.session_id},
            )
            .mappings()
            .all()
        )
    if not rows:
        raise SystemExit(f"Aucun run pour session_id={args.session_id!r} sur {args.dsn_env}.")

    verdicts: dict[str, int] = {}
    payload: list[dict[str, object]] = []
    print(f"== Diagnostic campagne {args.session_id} ({len(rows)} runs) ==\n")
    for row in rows:
        conversation_id = str(row["conversation_id"] or "")
        try:
            record_id = int(conversation_id.rsplit("-", 1)[-1])
        except ValueError:
            record_id = -1
        patterns = expected.get(record_id, [])
        if not patterns:
            verdicts["non évalué (pas de doc attendu défini)"] = verdicts.get("non évalué (pas de doc attendu défini)", 0) + 1
            if args.verbose:
                print(f"· {conversation_id:<12} (pas de doc attendu défini)          {str(row['question'])[:70]}")
            continue
        diagnosis = diagnose_run(row, patterns)
        verdicts[diagnosis.overall_label] = verdicts.get(diagnosis.overall_label, 0) + 1
        print(format_diagnosis_line(conversation_id, str(row["question"] or ""), diagnosis))
        payload.append(
            {
                "conversation_id": conversation_id,
                "question": row["question"],
                "overall": diagnosis.overall,
                "patterns": [vars(diag) for diag in diagnosis.patterns],
            }
        )

    _print_summary(verdicts)
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nDétail JSON écrit dans {args.json_out}")
    return 0


# ─────────────────────────────────────────────────────────────────────────────
# run — rejeu des questions Grist sur la config runtime, avec logging chat_runs
# ─────────────────────────────────────────────────────────────────────────────


def _replay_one(
    *,
    pipe,
    question: CampaignQuestion,
    session_id: str,
    runtime_config,
    rag_config,
    engine,
    env_label: str,
) -> dict | None:
    """Rejoue une question en répliquant le flux de logging de l'UI
    (process_query → run_stream → build_log_row → log_run/log_trace_events)."""
    from assistant_rh_rag_pipeline.chat_logger import build_log_row, log_run, log_trace_events
    from assistant_rh_rag_pipeline.ministry_scope import build_retrieval_scope
    from assistant_rh_rag_pipeline.models import Chunk

    from src.ui.chatbot_sources import context_items_to_v1_chunks, extract_legal_refs_for_display

    scope = build_retrieval_scope(question.ministry_id)
    turn_id = uuid.uuid4().hex[:8]
    trace_id = uuid.uuid4().hex

    started = time.perf_counter()
    qr = pipe.process_query(question.question, [], retrieval_scope=scope)
    if not qr.should_proceed:
        print(f"· {question.conversation_id:<12} intent gating: pas de RAG ({qr.intent_reason}) — non loggé")
        return None
    answer = "".join(pipe.run_stream(qr, [], turn_id=turn_id, trace_id=trace_id, retrieval_scope=scope))
    total_time_ms = (time.perf_counter() - started) * 1000

    result = pipe.last_result
    context_items = result.context_items if result else []
    _, legal_refs_v3 = extract_legal_refs_for_display(answer, context_items)
    v1_chunks = context_items_to_v1_chunks(context_items, Chunk)

    session_state = {
        "session_id": session_id,
        "conversation_id": question.conversation_id,
        "turns": [],
        "user_group": CAMPAIGN_USER_GROUP,
    }
    row = build_log_row(
        turn_id=turn_id,
        query=question.question,
        response=answer,
        pipeline=pipe,
        qr=qr,
        config=rag_config,
        runtime_config=runtime_config,
        session_state=session_state,
        total_time_ms=total_time_ms,
        context_items=context_items,
        v1_chunks_for_display=v1_chunks,
        legal_refs_v3=legal_refs_v3,
        trace_id=trace_id,
    )
    if engine is not None:
        log_run(row, engine=engine)
        trace_events = (result.metadata if result else {}).get("rag_trace_events", [])
        log_trace_events(trace_events, turn_id=turn_id, trace_id=trace_id, engine=engine, env_label=env_label)
    return row


def cmd_run(args: argparse.Namespace) -> int:
    dsn = _resolve_dsn(args.dsn_env)
    # get_rag_config / create_engine_from_env lisent SCW_POSTGRES_DSN : on le
    # pointe sur l'environnement demandé AVANT de créer le pipeline, pour que
    # config runtime, prompts DB et corpus viennent du même endroit.
    os.environ["SCW_POSTGRES_DSN"] = dsn

    from assistant_rh_rag_pipeline import create_pipeline
    from assistant_rh_rag_pipeline.admin import get_rag_config, runtime_config_to_rag_config
    from assistant_rh_rag_pipeline.db_helpers import create_engine_from_env

    only_ids = {int(value) for value in args.ids} if args.ids else None
    questions = fetch_campaign_questions(
        grist_doc_id=args.grist_doc,
        grist_table_id=args.grist_table,
        expected_config=Path(args.expected_config),
        only_ids=only_ids,
    )
    if args.limit:
        questions = questions[: args.limit]
    if not questions:
        raise SystemExit("Aucune question à rejouer (filtre --ids trop restrictif ?).")

    runtime_config = get_rag_config()
    rag_config = runtime_config_to_rag_config(runtime_config)
    pipe = create_pipeline(config=rag_config, dsn=dsn)
    engine = None if args.dry_run else create_engine_from_env()

    session_id = args.session_id or f"suivi-tests-{date.today():%Y%m%d}"
    print(f"== Rejeu {len(questions)} questions → session_id={session_id} ({args.dsn_env}{', dry-run' if args.dry_run else ''}) ==\n")

    verdicts: dict[str, int] = {}
    for question in questions:
        try:
            row = _replay_one(
                pipe=pipe,
                question=question,
                session_id=session_id,
                runtime_config=runtime_config,
                rag_config=rag_config,
                engine=engine,
                env_label=args.env_label,
            )
        except Exception as exc:  # un échec ne doit pas stopper la campagne
            print(f"✗ {question.conversation_id:<12} ERREUR: {type(exc).__name__}: {exc}")
            verdicts["erreur"] = verdicts.get("erreur", 0) + 1
            continue
        if row is None:
            verdicts["intent gating"] = verdicts.get("intent gating", 0) + 1
            continue
        if question.expected_patterns:
            diagnosis = diagnose_run(row, question.expected_patterns)
            verdicts[diagnosis.overall_label] = verdicts.get(diagnosis.overall_label, 0) + 1
            print(format_diagnosis_line(question.conversation_id, question.question, diagnosis))
        else:
            verdicts["non évalué (pas de doc attendu défini)"] = verdicts.get("non évalué (pas de doc attendu défini)", 0) + 1
            print(f"· {question.conversation_id:<12} rejoué, pas de doc attendu défini")

    _print_summary(verdicts)
    if not args.dry_run:
        print(f"\nRapport rejouable hors-ligne : scripts/suivi_tests_replay.py report --session-id {session_id} --dsn-env {args.dsn_env}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    subparsers = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--dsn-env", default=DEFAULT_DSN_ENV, help=f"Variable d'env portant le DSN Postgres (défaut: {DEFAULT_DSN_ENV})")
    common.add_argument("--expected-config", default=str(DEFAULT_EXPECTED_CONFIG), help="Fichier JSON des documents attendus par question")

    run_parser = subparsers.add_parser("run", parents=[common], help="Rejouer les questions Grist et logger dans chat_runs")
    run_parser.add_argument("--session-id", default=None, help="Défaut: suivi-tests-YYYYMMDD (date du jour)")
    run_parser.add_argument("--grist-doc", default=None, help="Doc Grist Suivi-Tests (défaut: SUIVI_TESTS_GRIST_DOC_ID ou doc connu)")
    run_parser.add_argument("--grist-table", default=None, help="Table Grist (défaut: Manuel_testing)")
    run_parser.add_argument("--ids", nargs="*", default=None, help="Ne rejouer que ces record_id Grist")
    run_parser.add_argument("--limit", type=int, default=None, help="Limiter le nombre de questions")
    run_parser.add_argument("--dry-run", action="store_true", help="Ne pas écrire dans chat_runs/rag_trace_events")
    run_parser.add_argument("--env-label", default="staging", help="Étiquette env des trace events (défaut: staging)")
    run_parser.set_defaults(func=cmd_run)

    report_parser = subparsers.add_parser("report", parents=[common], help="Diagnostiquer une campagne déjà loggée (sans LLM)")
    report_parser.add_argument("--session-id", required=True, help="session_id chat_runs de la campagne")
    report_parser.add_argument("--json-out", default=None, help="Écrire le détail JSON dans ce fichier")
    report_parser.add_argument("--verbose", action="store_true", help="Afficher aussi les questions sans doc attendu")
    report_parser.set_defaults(func=cmd_report)
    return parser


def main() -> int:
    from dotenv import load_dotenv

    load_dotenv(REPO_ROOT / ".env")
    args = build_parser().parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

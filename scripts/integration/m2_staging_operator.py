"""Exercise the individual staging API from Conversations and clean isolated fixtures.

Run from the checkout with python -m scripts.integration.m2_staging_operator.
The two temporary SQL logins use native Scaleway readonly permissions plus their
existing runtime role. Their privileges and logins are removed after the recipe.
A fault trigger matches only the synthetic feedback and is always removed.
No production target, existing runtime privilege, or application config is changed.
Private session state and answers must remain outside every Git checkout.
"""

import argparse
import asyncio
import base64
import hashlib
import json
import os
import secrets
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import httpx
import psycopg
from assistant_rh_api.db.dsn import DatabaseSettings, validate_libpq_environment
from assistant_rh_api.db.feedback_store import FeedbackStore, feedback
from assistant_rh_api.db.pool import Database
from dotenv import dotenv_values
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from scripts.conformance.m0b_private import private_directory


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--operator-env-file", type=Path, required=True)
    parser.add_argument("--cloud-env-file", type=Path, required=True)
    parser.add_argument("--kubeconfig", type=Path, required=True)
    parser.add_argument("--kubectl", default="kubectl")
    parser.add_argument("--private-dir", type=Path, required=True)
    args = parser.parse_args()
    ROOT = Path(__file__).resolve().parents[2]
    OUT = private_directory(args.private_dir)
    KUBE = [args.kubectl, "--kubeconfig", str(args.kubeconfig), "--request-timeout=15s"]
    operator = dotenv_values(args.operator_env_file)
    validate_libpq_environment(os.environ)
    DSN = operator["SCW_POSTGRES_DSN"]
    target = conninfo_to_dict(DSN)
    if (
        operator.get("APP_ENV") != "staging"
        or target.get("user") != "assistant_rh_migrator"
        or target.get("dbname") != "assistant_rh"
        or target.get("sslmode") not in ("verify-ca", "verify-full")
    ):
        raise ValueError("Expected the verified TLS staging migrator DSN")
    CLOUD = dotenv_values(args.cloud_env_file)
    RDB = "https://api.scaleway.com/rdb/v1/regions/fr-par/instances/c3d3f676-4c9f-47ea-87d4-e6b300ec7ebf"
    suffix = secrets.token_hex(6)
    remote_dir = "/tmp/m2-individual-" + suffix
    report = {
        "schema": "assistant-rh-m2-individual-api-acceptance-v1",
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "phases": [],
        "sql_checks": [],
    }
    fixtures, created_logins, cleanup_jtis = {}, {}, []
    fault = "m2_fault_" + suffix
    legacy_id, legacy_doc, missing_doc = (uuid4() for _ in range(3))
    legacy = b"%PDF-1.4\n% synthetic transfer fixture\n" + b"x" * (32 * 1024 * 1024 - 64)
    legacy += b"\n%%EOF\n"
    legacy_expected = {"bytes": len(legacy), "sha256": hashlib.sha256(legacy).hexdigest()}

    def progress(message):
        print(message, flush=True)
        (OUT / "individual-report.json").write_text(json.dumps(report, indent=2) + "\n")

    def kube(args, source=None):
        value = subprocess.run(KUBE + args, input=source, text=True, capture_output=True, timeout=300)
        if value.returncode:
            raise RuntimeError("Kubernetes operation failed: " + value.stderr[-1200:])
        return value.stdout

    def remote(phase, data=None):
        command = KUBE + ["exec", "-i", "-n", "conversations", pod, "--", "python", remote_dir + "/recipe.py", phase, remote_dir + "/state.json"]
        result = subprocess.run(command, input=json.dumps(data or {}), text=True, capture_output=True, timeout=300)
        lines = result.stdout.splitlines()
        value = json.loads(lines[-1]) if lines else {}
        if phase == "cleanup":
            cleanup_jtis.extend(value.get("measurements", {}).pop("private_jtis", []))
        report["phases"].append(value)
        progress("HTTP phase " + phase + ": " + str(len(value.get("checks", []))) + " checks; exit " + str(result.returncode))
        if result.returncode:
            (OUT / ("private-" + phase + "-stderr.log")).write_text(result.stderr)
            raise RuntimeError("HTTP phase failed: " + phase)
        return value

    def check(name, condition, **details):
        report["sql_checks"].append({"name": name, "passed": bool(condition), **details})
        if not condition:
            raise RuntimeError("SQL check failed: " + name)

    def row(c):
        return c.execute(
            "SELECT *,xmin::text AS row_xmin,ctid::text AS row_ctid FROM public.chat_feedbacks WHERE turn_id=%s", (fixtures["turns"]["feedback"],)
        ).fetchone()

    def audit_count(c):
        return c.execute("SELECT count(*) AS count FROM public.chat_feedback_audit WHERE turn_id=%s", (fixtures["turns"]["feedback"],)).fetchone()[
            "count"
        ]

    def roles(c, api):
        for role, end in [("arh_streamlit", "streamlit"), ("arh_analysis", "analysis")]:
            login = "m2_" + suffix + "_" + end
            password = "M2a-" + secrets.token_urlsafe(32)
            r = api.post(RDB + "/users", json={"name": login, "password": password, "is_admin": False})
            r.raise_for_status()
            created_logins[login] = role
            r = api.put(RDB + "/privileges", json={"database_name": "assistant_rh", "user_name": login, "permission": "readonly"})
            r.raise_for_status()
            c.execute("SELECT public.api_attach_runtime_user(%s,%s)", (login, role))
            with psycopg.connect(make_conninfo(DSN, user=login, password=password), autocommit=True, row_factory=dict_row) as connection:
                identity = connection.execute(
                    """SELECT current_user AS user,pg_has_role(current_user,'arh_api','USAGE') AS api,
                    has_schema_privilege(current_user,'public','USAGE') AS schema_usage,
                    has_table_privilege(current_user,'chat_feedbacks','TRUNCATE') AS truncate"""
                ).fetchone()
                check(
                    role + "_non_api_login",
                    identity["user"] == login and not identity["api"] and identity["schema_usage"] and not identity["truncate"],
                )
                with connection.transaction(force_rollback=True):
                    connection.execute("SET LOCAL lock_timeout='1s'")

                    def attempt(label, statement, parameters, expected):
                        state, message, allowed = None, "", False
                        try:
                            with connection.transaction(force_rollback=True):
                                connection.execute(statement, parameters)
                                allowed = True
                        except psycopg.Error as error:
                            state, message = error.sqlstate, error.diag.message_primary
                        passed = allowed if expected else not allowed and state in ("42501", "23514")
                        if role == "arh_streamlit" and label.startswith("content_"):
                            passed = passed and message == "Individual feedback is written only by the API"
                        check(role + "_" + label, passed, sqlstate=state)

                    for column in ("comment", "stars", "reasons_positive", "reasons_negative"):
                        value = 0 if column == "stars" else "forged"
                        attempt(
                            "content_" + column,
                            sql.SQL("UPDATE public.chat_feedbacks SET {}=%s WHERE turn_id=%s").format(sql.Identifier(column)),
                            (value, fixtures["turns"]["feedback"]),
                            False,
                        )
                    attempt(
                        "annotation",
                        "UPDATE public.chat_feedbacks SET theme='Synthetic annotation' WHERE turn_id=%s",
                        (fixtures["turns"]["feedback"],),
                        role == "arh_streamlit",
                    )
                    attempt(
                        "analysis",
                        """UPDATE public.chat_feedbacks SET error_category='synthetic',
                        ai_reason='Synthetic analysis',ai_analyzed_at=now() WHERE turn_id=%s""",
                        (fixtures["turns"]["feedback"],),
                        True,
                    )
                    attempt("delete_individual", "DELETE FROM public.chat_feedbacks WHERE turn_id=%s", (fixtures["turns"]["feedback"],), False)
                    attempt("disable_guard", "ALTER TABLE public.chat_feedbacks DISABLE TRIGGER api_feedback_individual_guard", None, False)
                    attempt("drop_guard", "DROP TRIGGER api_feedback_individual_guard ON public.chat_feedbacks", None, False)
                    if role == "arh_streamlit":
                        attempt(
                            "rename_individual",
                            "UPDATE public.chat_runs SET turn_id=%s WHERE turn_id=%s",
                            (uuid4().hex, fixtures["turns"]["feedback"]),
                            False,
                        )
                        for comment in ("First", "Second"):
                            connection.execute(
                                "INSERT INTO public.chat_feedbacks(turn_id,ts,stars,comment) VALUES (%s,clock_timestamp(),3,%s)",
                                (fixtures["turns"]["collective"], comment),
                            )
                        current = connection.execute(
                            "SELECT comment FROM chat_feedbacks WHERE turn_id=%s", (fixtures["turns"]["collective"],)
                        ).fetchone()
                        archived = connection.execute(
                            "SELECT count(*) AS count FROM chat_feedback_audit WHERE turn_id=%s", (fixtures["turns"]["collective"],)
                        ).fetchone()
                        check("collective_history_replacement", current == {"comment": "Second"} and archived == {"count": 1})
            progress("SQL role " + role + " exercised through native Scaleway readonly login")

    with (
        psycopg.connect(DSN, autocommit=True, row_factory=dict_row) as op,
        httpx.Client(headers={"X-Auth-Token": CLOUD["SCW_SECRET_KEY"]}, timeout=30) as api,
    ):
        server = kube(["config", "view", "--minify", "-o", "jsonpath={.clusters[0].cluster.server}"])
        check("staging_cluster", server == "https://fce3ddcb-b9ff-49fb-8b7d-aca605272b0a.api.k8s.fr-par.scw.cloud:6443")
        check("private_staging_database", str(op.execute("SELECT inet_server_addr() AS address").fetchone()["address"]) == "10.10.1.2")
        check("operator_tls", op.execute("SELECT ssl FROM pg_stat_ssl WHERE pid=pg_backend_pid()").fetchone()["ssl"])
        check(
            "operator_target",
            op.execute("SELECT current_user AS user,current_database() AS database").fetchone()
            == {"user": "assistant_rh_migrator", "database": "assistant_rh"},
        )
        pods = json.loads(kube(["get", "pods", "-n", "conversations", "-l", "app.kubernetes.io/component=backend", "-o", "json"]))["items"]
        pod = next(value["metadata"]["name"] for value in pods if all(x["ready"] for x in value["status"]["containerStatuses"]))
        copy_code = "import pathlib,sys; p=pathlib.Path(" + repr(remote_dir) + ');p.mkdir(mode=0o700);(p/"recipe.py").write_text(sys.stdin.read())'
        kube(
            ["exec", "-i", "-n", "conversations", pod, "--", "python", "-c", copy_code],
            (ROOT / "scripts/integration/m2_individual_staging.py").read_text(),
        )
        try:
            password = "Synthetic-M2-" + secrets.token_urlsafe(24)
            fixtures = remote("setup", {"b4_password": password})["measurements"]["fixtures"]
            (OUT / "private-fixtures.json").write_text(json.dumps(fixtures))
            (OUT / "private-fixtures.json").chmod(0o600)
            salt = secrets.token_bytes(16)
            hashed = (
                "pbkdf2_sha256$200000$"
                + base64.b64encode(salt).decode()
                + "$"
                + base64.b64encode(hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200000)).decode()
            )
            with op.transaction():
                op.execute(
                    """INSERT INTO public.user_groups(slug,label,password_hash,allowed_ministries,default_ministry)
                    VALUES (%s,'Synthetic M2',%s,%s,'matte')""",
                    (fixtures["b4_slug"], hashed, Jsonb(["matte"])),
                )
                s3 = op.execute(
                    """SELECT doc_id,source FROM rag_documents WHERE source='matte'
                    AND storage_path LIKE 'matte/%.pdf' AND page_count>0 ORDER BY page_count,doc_id LIMIT 1"""
                ).fetchone()
                foreign = op.execute(
                    "SELECT doc_id FROM rag_documents WHERE source='mso' AND storage_path LIKE 'mso/%.pdf' ORDER BY doc_id LIMIT 1"
                ).fetchone()
                candidate = op.execute(
                    """SELECT doc_id FROM rag_documents WHERE source='matte' AND doc_id<>%s
                    AND storage_path LIKE 'matte/%%.pdf' ORDER BY doc_id LIMIT 1""",
                    (s3["doc_id"],),
                ).fetchone()
                op.execute("INSERT INTO public.documents(id,filename,data) VALUES (%s,'synthetic-m2.pdf',%s)", (legacy_id, legacy))
                op.execute(
                    "INSERT INTO public.rag_documents(doc_id,source,title,legacy_doc_id) VALUES (%s,'matte','Synthetic M2 transfer',%s)",
                    (legacy_doc, legacy_id),
                )
                op.execute(
                    "INSERT INTO public.rag_documents(doc_id,source,title,storage_path) VALUES (%s,'matte','Synthetic M2 absent storage',%s)",
                    (missing_doc, "matte/m2-absent-" + suffix + ".pdf"),
                )
                for label, turn in fixtures["turns"].items():
                    actor = None if label == "collective" else fixtures["peer"] if label == "peer" else fixtures["author"]
                    op.execute(
                        """INSERT INTO public.chat_runs(turn_id,ts,question,answer,user_group,selected_ministry,author_user_id,api_record)
                        VALUES (%s,now(),'Synthetic M2 question','Synthetic answer',%s,'matte',%s,%s)""",
                        (
                            turn,
                            fixtures["b4_slug"] if actor is None else "@conversations",
                            actor,
                            Jsonb({"status": "completed", "diagnostics": {"unserved_candidate": str(candidate["doc_id"])}}),
                        ),
                    )
                    if label != "feedback":
                        for i, (ref, doc) in enumerate(
                            [("s3", s3["doc_id"]), ("legacy", legacy_doc), ("foreign", foreign["doc_id"]), ("missing", missing_doc)]
                        ):
                            op.execute(
                                """INSERT INTO public.chat_run_sources(turn_id,ordinal,doc_ref,document_id,title,url)
                                VALUES (%s,%s,%s,%s,'Synthetic selected source','')""",
                                (turn, i, ref, str(doc)),
                            )
            progress("Isolated RAG, B4 and 32 MiB legacy fixtures committed")
            remote("feedback_initial")
            initial = row(op)
            check("initial_unique_revision", initial["api_revision"] == 0 and initial["comment"] == "Initial" and audit_count(op) == 0)
            remote("feedback_retry")
            check("retry_has_no_write", row(op) == initial)
            remote("feedback_denials")
            check("denials_have_no_write", row(op) == initial)
            roles(op, api)
            op.execute(
                """UPDATE public.chat_feedbacks SET theme='Synthetic human',beta_scope='human',
                error_category='retrieval',ai_reason='Obsolete',ai_analyzed_at=now() WHERE turn_id=%s""",
                (fixtures["turns"]["feedback"],),
            )
            remote("feedback_edit")
            edited = row(op)
            check("annotations_preserved", edited["theme"] == "Synthetic human" and edited["beta_scope"] == "human")
            check(
                "analysis_invalidated_once",
                edited["error_category"] is None and edited["ai_reason"] is None and edited["ai_analyzed_at"] is None and edited["api_revision"] == 1,
            )
            check(
                "one_pending_analysis",
                op.execute(
                    "SELECT count(*) AS count FROM chat_feedbacks WHERE turn_id=%s AND stars<=2 AND error_category IS NULL",
                    (fixtures["turns"]["feedback"],),
                ).fetchone()["count"]
                == 1,
            )

            async def stale_analysis():
                database = Database(DatabaseSettings(dsn=DSN, max_size=1))
                await database.open()
                try:
                    previous = feedback(initial)
                    return await FeedbackStore(database).save_analysis(
                        previous.id, previous.revision, "stale", "Obsolete", datetime.now(timezone.utc)
                    )
                finally:
                    await database.close()

            check("stale_analysis_cannot_overwrite_edit", not asyncio.run(stale_analysis()))
            audit = op.execute(
                "SELECT actor_user_id,audit_session_hash,record FROM public.chat_feedback_audit WHERE turn_id=%s", (fixtures["turns"]["feedback"],)
            ).fetchone()
            check(
                "individual_audit",
                str(audit["actor_user_id"]) == fixtures["author"]
                and audit["record"]["ai_reason"] == "Obsolete"
                and len(audit["audit_session_hash"]) == 64,
            )
            check(
                "reconnection_changes_audit_session",
                audit["audit_session_hash"] != initial["api_session_hash"] and str(edited["api_actor_user_id"]) == fixtures["author"],
            )
            before_audit = audit_count(op)
            remote("feedback_concurrency")
            check("concurrent_updates_serialized", row(op)["api_revision"] == edited["api_revision"] + 8 and audit_count(op) == before_audit + 8)
            before = row(op)
            before_audit = audit_count(op)
            with op.transaction():
                op.execute(
                    sql.SQL(
                        """CREATE FUNCTION public.{}() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog AS $$
                        BEGIN RAISE EXCEPTION 'Synthetic M2 fault' USING ERRCODE='23514'; END $$"""
                    ).format(sql.Identifier(fault))
                )
                op.execute(
                    sql.SQL(
                        """CREATE TRIGGER {} BEFORE UPDATE ON public.chat_feedbacks FOR EACH ROW
                        WHEN (NEW.turn_id={} AND NEW.stars=2) EXECUTE FUNCTION public.{}()"""
                    ).format(sql.Identifier(fault), sql.Literal(fixtures["turns"]["feedback"]), sql.Identifier(fault))
                )
            try:
                remote("feedback_rollback")
                check("http_failure_rolls_back_current_and_audit", row(op) == before and audit_count(op) == before_audit)
            finally:
                with op.transaction():
                    op.execute(sql.SQL("DROP TRIGGER IF EXISTS {} ON public.chat_feedbacks").format(sql.Identifier(fault)))
                    op.execute(sql.SQL("DROP FUNCTION IF EXISTS public.{}()").format(sql.Identifier(fault)))
            remote("documents", {"legacy": legacy_expected, "unserved_ref": str(candidate["doc_id"])})
            remote("clients")
            real = op.execute(
                "SELECT api_record->>'status' AS status,count(*) AS count FROM chat_runs WHERE author_user_id=%s AND turn_id<>ALL(%s) GROUP BY 1",
                (fixtures["author"], list(fixtures["turns"].values())),
            ).fetchall()
            check("sdk_provider_runs_completed", real == [{"status": "completed", "count": 2}])
            remote("streaming")
            for _ in range(50):
                statuses = op.execute(
                    """SELECT api_record->>'status' AS status,count(*) AS count FROM chat_runs
                    WHERE author_user_id=%s AND turn_id<>ALL(%s) GROUP BY 1 ORDER BY 1""",
                    (fixtures["author"], list(fixtures["turns"].values())),
                ).fetchall()
                if statuses == [{"status": "cancelled", "count": 1}, {"status": "completed", "count": 3}]:
                    break
                time.sleep(0.1)
            check(
                "streaming_completion_and_disconnect_persisted",
                statuses == [{"status": "cancelled", "count": 1}, {"status": "completed", "count": 3}],
            )
            report["passed"] = True
        except Exception as error:
            report["passed"] = False
            report["failure"] = type(error).__name__ + ": " + str(error)
            raise
        finally:
            try:
                remote("cleanup")
            except Exception as error:
                report["backend_cleanup_error"] = type(error).__name__
            if fixtures:
                with op.transaction():
                    op.execute(sql.SQL("DROP TRIGGER IF EXISTS {} ON public.chat_feedbacks").format(sql.Identifier(fault)))
                    op.execute(sql.SQL("DROP FUNCTION IF EXISTS public.{}()").format(sql.Identifier(fault)))
                    ids = [
                        r["turn_id"]
                        for r in op.execute(
                            "SELECT turn_id FROM chat_runs WHERE author_user_id=ANY(%s::uuid[]) OR turn_id=ANY(%s)",
                            ([fixtures["author"], fixtures["peer"]], list(fixtures["turns"].values())),
                        )
                    ]
                    for table in ("chat_feedback_audit", "chat_feedbacks", "chat_run_sources", "rag_trace_events", "chat_runs"):
                        op.execute(sql.SQL("DELETE FROM public.{} WHERE turn_id=ANY(%s)").format(sql.Identifier(table)), (ids,))
                    op.execute("DELETE FROM user_groups WHERE slug=%s", (fixtures["b4_slug"],))
                    op.execute("DELETE FROM rag_documents WHERE doc_id=ANY(%s)", ([legacy_doc, missing_doc],))
                    op.execute("DELETE FROM documents WHERE id=%s", (legacy_id,))
                    op.execute("DELETE FROM api_delegation_replays WHERE jti=ANY(%s)", (cleanup_jtis,))
                report["cleanup"] = {
                    "rag_runs_remaining": op.execute(
                        "SELECT count(*) AS count FROM chat_runs WHERE author_user_id=ANY(%s::uuid[]) OR turn_id=ANY(%s)",
                        ([fixtures["author"], fixtures["peer"]], list(fixtures["turns"].values())),
                    ).fetchone()["count"],
                    "synthetic_documents_remaining": op.execute(
                        "SELECT count(*) AS count FROM rag_documents WHERE doc_id=ANY(%s)", ([legacy_doc, missing_doc],)
                    ).fetchone()["count"],
                    "replays_remaining": op.execute(
                        "SELECT count(*) AS count FROM api_delegation_replays WHERE jti=ANY(%s)", (cleanup_jtis,)
                    ).fetchone()["count"],
                }
            for login in created_logins:
                api.put(RDB + "/privileges", json={"database_name": "assistant_rh", "user_name": login, "permission": "none"}).raise_for_status()
                r = api.delete(RDB + "/users/" + login)
                report.setdefault("sql_login_cleanup", {})[login] = r.status_code in (200, 204, 404)
            kube(
                [
                    "exec",
                    "-n",
                    "conversations",
                    pod,
                    "--",
                    "python",
                    "-c",
                    "import pathlib; p=pathlib.Path("
                    + repr(remote_dir)
                    + ');[(p/n).unlink(missing_ok=True) for n in ("recipe.py","state.json")];p.rmdir()',
                ]
            )
            if "backend_cleanup_error" in report or not all(report.get("sql_login_cleanup", {}).values()) or any(report.get("cleanup", {}).values()):
                report["passed"] = False
            report["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            progress("Individual staging recipe finished; passed=" + str(report.get("passed")))
            if not report.get("passed"):
                raise SystemExit(1)


if __name__ == "__main__":
    main()

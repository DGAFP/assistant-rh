"""Inspect staging runtime grants and exercise synthetic rows in a rolled-back transaction.

Run inside the API staging image. No dotenv loading, grants, DDL, or
committed fixtures. Non-API roles are exercised only if the existing operator
connection may SET ROLE; missing authority is reported as incomplete.
"""

import json
import os
from datetime import datetime, timezone
from uuid import uuid4

import psycopg
from assistant_rh_api.db.dsn import validate_libpq_environment
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row

ROLES = ("arh_api", "arh_streamlit", "arh_analysis")


def probe(connection):
    checks = []
    report = {
        "schema": "assistant-rh-m2-runtime-roles-probe-v1",
        "at": datetime.now(timezone.utc).isoformat(),
        "checks": checks,
        "role_exercises": {},
    }
    turn_id, actor = uuid4().hex, uuid4()
    try:
        connection.execute("SET LOCAL statement_timeout = '5s'")
        connection.execute("SET LOCAL lock_timeout = '1s'")
        current = connection.execute("SELECT current_user AS name").fetchone()["name"]
        report["connection_user"] = current
        report["grants"] = []
        for role in ROLES:
            row = connection.execute(
                """SELECT rolname, rolcanlogin, rolsuper,
                       has_column_privilege(rolname,'public.chat_feedbacks','comment','UPDATE') AS content_update,
                       has_column_privilege(rolname,'public.chat_feedbacks','theme','UPDATE') AS annotation_update,
                       has_column_privilege(rolname,'public.chat_feedbacks','ai_reason','UPDATE') AS analysis_update,
                       has_table_privilege(rolname,'public.chat_feedbacks','TRUNCATE') AS truncate,
                       has_table_privilege(rolname,'public.chat_feedbacks','TRIGGER') AS trigger,
                       pg_has_role(rolname,(SELECT relowner FROM pg_class WHERE oid='public.chat_feedbacks'::regclass),'USAGE') AS owner
                FROM pg_roles WHERE rolname=%s""",
                (role,),
            ).fetchone()
            report["grants"].append(row)
            checks.append(
                {
                    "name": role + "_cannot_disable_guards",
                    "passed": row is not None and not any(row[k] for k in ("rolcanlogin", "rolsuper", "truncate", "trigger", "owner")),
                }
            )
        report["triggers"] = connection.execute(
            "SELECT tgname,tgenabled FROM pg_trigger WHERE tgname IN ('api_feedback_individual_guard','api_run_author_immutable') ORDER BY tgname"
        ).fetchall()
        checks.append(
            {"name": "both_guards_enabled", "passed": len(report["triggers"]) == 2 and all(row["tgenabled"] == "O" for row in report["triggers"])}
        )
        connection.execute(
            """INSERT INTO public.chat_runs(turn_id,ts,question,answer,user_group,selected_ministry,author_user_id)
            VALUES (%s,now(),'Synthetic M2 role probe','Synthetic answer','@conversations','matte',%s)""",
            (turn_id, actor),
        )
        # Avoid consuming a feedback ID for the rolled-back fixture.
        connection.execute(
            """INSERT INTO public.chat_feedbacks(id,turn_id,ts,stars,comment,api_group_slug,api_session_hash,api_actor_user_id)
            VALUES (%s,%s,now(),3,'Original','@conversations',%s,%s)""",
            (-int(uuid4().hex[:7], 16) - 1, turn_id, "a" * 64, actor),
        )

        def exercise(role, login, column, expected):
            allowed, sqlstate = False, None
            try:
                with connection.transaction(force_rollback=True):
                    connection.execute(psycopg.sql.SQL("SET LOCAL ROLE {}").format(psycopg.sql.Identifier(login)))
                    cursor = connection.execute(
                        psycopg.sql.SQL("UPDATE public.chat_feedbacks SET {}='Probe' WHERE turn_id=%s").format(psycopg.sql.Identifier(column)),
                        (turn_id,),
                    )
                    allowed = cursor.rowcount == 1
            except psycopg.Error as error:
                sqlstate = error.sqlstate
            checks.append({"name": role + "_" + column, "passed": allowed if expected else not allowed and sqlstate == "42501", "sqlstate": sqlstate})

        for role in ROLES:
            # NOLOGIN roles carry write grants only. Scaleway grants schema USAGE
            # directly to the actual login, so SET ROLE arh_* would test a different
            # identity and incorrectly report a schema-permission failure.
            logins = connection.execute(
                """SELECT rolname FROM pg_roles
                WHERE rolcanlogin AND NOT rolsuper AND pg_has_role(oid,%s,'USAGE')
                  AND has_schema_privilege(oid,'public','USAGE')
                  AND NOT pg_has_role(oid,(SELECT relowner FROM pg_class WHERE oid='public.chat_feedbacks'::regclass),'USAGE')
                  AND pg_has_role(current_user,oid,'MEMBER') ORDER BY rolname""",
                (role,),
            ).fetchall()
            report["role_exercises"][role] = "exercised" if logins else "not_exercised_missing_runtime_login_or_operator_authority"
            if not logins:
                continue
            login = logins[0]["rolname"]
            exercise(role, login, "comment", role == "arh_api")
            exercise(role, login, "theme", role in ("arh_api", "arh_streamlit"))
            exercise(role, login, "ai_reason", True)
            if role == "arh_streamlit":
                collective = uuid4().hex
                with connection.transaction(force_rollback=True):
                    connection.execute(psycopg.sql.SQL("SET LOCAL ROLE {}").format(psycopg.sql.Identifier(login)))
                    connection.execute(
                        """INSERT INTO public.chat_runs(turn_id,ts,question,answer,user_group,selected_ministry)
                        VALUES (%s,now(),'Synthetic collective','Answer','synthetic','matte')""",
                        (collective,),
                    )
                    for comment in ("First", "Second"):
                        connection.execute(
                            "INSERT INTO public.chat_feedbacks(turn_id,ts,stars,comment) VALUES (%s,now(),3,%s)", (collective, comment)
                        )
                    row = connection.execute("SELECT comment FROM public.chat_feedbacks WHERE turn_id=%s", (collective,)).fetchone()
                    checks.append({"name": "collective_legacy_replacement", "passed": row == {"comment": "Second"}})
    finally:
        connection.rollback()
    report["cleanup"] = {
        "fixture_rows_remaining": connection.execute("SELECT count(*) AS count FROM public.chat_runs WHERE turn_id=%s", (turn_id,)).fetchone()[
            "count"
        ],
        "transaction_rolled_back": True,
    }
    connection.rollback()
    report["complete"] = all(value == "exercised" for value in report["role_exercises"].values())
    report["passed"] = bool(checks) and all(check["passed"] for check in checks) and report["cleanup"]["fixture_rows_remaining"] == 0
    report["gate"] = "pass" if report["complete"] and report["passed"] else "incomplete" if report["passed"] else "fail"
    return report


def main():
    validate_libpq_environment(os.environ)
    dsn = os.environ["SCW_POSTGRES_DSN"]
    target = conninfo_to_dict(dsn)
    if os.environ.get("APP_ENV") != "staging" or target.get("host") != "10.10.1.2" or target.get("dbname") != "assistant_rh":
        raise SystemExit("Refusing a target other than Kapsule staging PostgreSQL")
    try:
        with psycopg.connect(dsn, row_factory=dict_row, connect_timeout=5) as connection:
            report = probe(connection)
        print(json.dumps(report, indent=2))
        raise SystemExit(0 if report["gate"] == "pass" else 2 if report["gate"] == "incomplete" else 1)
    except psycopg.Error as error:
        raise SystemExit(f"Staging role probe failed ({type(error).__name__})") from None


if __name__ == "__main__":
    main()

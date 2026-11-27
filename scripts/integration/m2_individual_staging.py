"""Staging HTTP recipe, run in one Conversations pod with operator-owned fixtures.

The operator creates RAG fixtures separately; this backend never accesses the RAG
database. Private session and replay identifiers stay in a mode-0600 state file.
Run setup, seed fixtures, execute phases, then cleanup even after a failed phase.
"""

import argparse
import asyncio
import base64
import hashlib
import json
import secrets
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import django
import httpx


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase")
    parser.add_argument("state", type=Path)
    args = parser.parse_args()
    django.setup()
    from chat import assistant_rh_delegation as delegation
    from core.models import MinistryGrant
    from django.conf import settings
    from django.contrib.auth import get_user_model
    from django.contrib.auth.models import Group
    from django.contrib.sessions.backends.cache import SessionStore
    from django.test import Client

    base = "http://assistant-rh-api.assistant-rh-api.svc.cluster.local:8000/v1/"
    if settings.ASSISTANT_RH_API_URL.rstrip("/") + "/" != base:
        raise RuntimeError("Refusing a target other than the private staging API")
    payload = json.loads(input())
    state = json.loads(args.state.read_text()) if args.state.exists() else {"users": [], "sessions": [], "jtis": []}
    checks, measurements = [], {}

    def save():
        args.state.write_text(json.dumps(state))
        args.state.chmod(0o600)

    def check(name, passed):
        checks.append({"name": name, "passed": bool(passed)})
        if not passed:
            raise RuntimeError("Staging check failed: " + name)

    def login(user):
        client = Client()
        client.force_login(user, backend="django.contrib.auth.backends.ModelBackend")
        key = client.session.session_key
        state["sessions"].append(key)
        save()
        return key

    def remember(signed):
        part = signed.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
        state["jtis"].append(claims["jti"])
        save()
        return claims

    def headers(who="author", ministry="matte", expired=False):
        signed = delegation.sign_assertion(state[who], state[who + "_session"], ministry)
        claims = remember(signed)
        if expired:
            import jwt

            claims.update(iat=int(time.time()) - 300, exp=int(time.time()) - 1)
            signed = jwt.encode(claims, delegation._delegation_keys()[0], algorithm="EdDSA", headers={"kid": settings.ASSISTANT_RH_SIGNING_KID})
        return {"Authorization": "Bearer " + signed}

    def feedback_body(**changes):
        return {"completion_id": state["turns"]["feedback"], "stars": 4, "comment": "Initial", "reasons_positive": ["Clair", "Utile"], **changes}

    def post_many(client, bodies):
        requests = [(body, headers()) for body in bodies]
        with ThreadPoolExecutor(max_workers=4) as executor:
            return list(executor.map(lambda entry: client.post("feedback", json=entry[0], headers=entry[1]), requests))

    try:
        if args.phase == "setup":
            state.update(marker="m2-individual-" + secrets.token_hex(8), b4_password=payload["b4_password"])
            group = Group.objects.create(name=state["marker"])
            state["group"] = group.pk
            save()
            for ministry in ("matte", "mi", "mso"):
                MinistryGrant.objects.create(group=group, ministry=ministry)
            for name in ("author", "peer"):
                user = get_user_model()(
                    email=f"{state['marker']}-{name}@example.invalid", sub=f"{state['marker']}-{name}", assistant_rh_admitted=True
                )
                user.set_unusable_password()
                user.save()
                state["users"].append(str(user.pk))
                state[name] = str(user.pk)
                save()
                user.groups.add(group)
                state[name + "_session"] = login(user)
            state["turns"] = {name: uuid4().hex for name in ("feedback", "peer", "collective", "documents")}
            state["b4_slug"] = state["marker"]
            save()
            measurements["fixtures"] = {key: state[key] for key in ("author", "peer", "turns", "marker", "b4_slug")}
        elif args.phase == "cleanup":
            for key in state["sessions"]:
                SessionStore(session_key=key).delete()
            get_user_model().objects.filter(pk__in=state["users"]).delete()
            Group.objects.filter(pk=state.get("group")).delete()
            check("users_removed", not get_user_model().objects.filter(pk__in=state["users"]).exists())
            check("group_removed", not Group.objects.filter(pk=state.get("group")).exists())
            check("sessions_removed", all(not SessionStore().exists(key) for key in state["sessions"]))
            measurements["private_jtis"] = state["jtis"]
        else:
            with httpx.Client(base_url=base, timeout=180, follow_redirects=False, trust_env=False) as client:
                turn = state["turns"]["feedback"]
                if args.phase in ("feedback_initial", "feedback_retry", "feedback_concurrency"):
                    if args.phase == "feedback_initial":
                        bodies = [feedback_body(comment="  Initial  ", reasons_positive=["Utile", "Clair", "Utile"])] * 4
                    elif args.phase == "feedback_retry":
                        bodies = [feedback_body(completion_id="chatcmpl-" + turn)] * 4
                    else:
                        bodies = [feedback_body(comment=f"Concurrent {index}") for index in range(8)]
                    responses = post_many(client, bodies)
                    check(args.phase + "_all_204", all(response.status_code == 204 and not response.content for response in responses))
                    check(args.phase + "_no_store", all(response.headers.get("cache-control") == "no-store" for response in responses))
                    result = client.get("feedback/" + turn, headers=headers())
                    check("author_reads_feedback", result.status_code == 200 and result.json()["helpful"] is True)
                    check(
                        "annotations_not_exposed", not any(key in result.json() for key in ("theme", "beta_scope", "ai_reason", "api_actor_user_id"))
                    )
                elif args.phase == "feedback_denials":
                    for label, who, ministry, target in (
                        ("peer", "peer", "matte", turn),
                        ("other_ministry", "author", "mi", turn),
                        ("collective", "author", "matte", state["turns"]["collective"]),
                        ("missing", "author", "matte", uuid4().hex),
                    ):
                        read = client.get("feedback/" + target, headers=headers(who, ministry))
                        write = client.post("feedback", json=feedback_body(completion_id=target), headers=headers(who, ministry))
                        check(label + "_feedback_refused", read.status_code == write.status_code == 404 and read.json() == write.json())
                    MinistryGrant.objects.filter(group_id=state["group"], ministry="matte").delete()
                    try:
                        read = client.get("feedback/" + turn, headers=headers(ministry=None))
                        write = client.post("feedback", json=feedback_body(), headers=headers(ministry=None))
                        check("withdrawn_feedback_refused", read.status_code == write.status_code == 404)
                    finally:
                        MinistryGrant.objects.get_or_create(group_id=state["group"], ministry="matte")
                    check("expired_feedback_refused", client.get("feedback/" + turn, headers=headers(expired=True)).status_code == 401)
                    issued = client.post("auth/session", json={"slug": state["b4_slug"], "password": state["b4_password"]})
                    check("collective_session_issued", issued.status_code == 200)
                    b4 = {"Authorization": "Bearer " + issued.json()["access_token"]}
                    try:
                        check("b4_cannot_read_individual_feedback", client.get("feedback/" + turn, headers=b4).status_code == 403)
                        check("b4_cannot_write_individual_feedback", client.post("feedback", json=feedback_body(), headers=b4).status_code == 403)
                        path = "documents/s3/content?completion_id=" + state["turns"]["documents"]
                        check("b4_cannot_open_individual_document", client.get(path, headers=b4).status_code == 403)
                    finally:
                        check("collective_session_revoked", client.delete("auth/session", headers=b4).status_code == 204)
                elif args.phase == "feedback_edit":
                    state["author_session"] = login(get_user_model().objects.get(pk=state["author"]))
                    body = feedback_body(stars=1, comment="Edited after reconnection", reasons_positive=[], reasons_negative=["Sources manquantes"])
                    check("reconnected_author_edits", client.post("feedback", json=body, headers=headers()).status_code == 204)
                    read = client.get("feedback/" + turn, headers=headers())
                    check("edit_persists", read.status_code == 200 and read.json()["comment"] == body["comment"] and read.json()["helpful"] is False)
                elif args.phase == "feedback_rollback":
                    response = client.post("feedback", json=feedback_body(stars=3, comment="Rollback probe", reasons_positive=[]), headers=headers())
                    check("failed_update_returns_500", response.status_code == 500)
                    check("failure_detail_hidden", all(word not in response.text for word in ("m2_fault", "Rollback probe", "23514", "postgres")))
                elif args.phase == "documents":
                    document_turn = state["turns"]["documents"]
                    path = "documents/{}/content?completion_id=" + document_turn

                    def read_pdf(ref, extra=None):
                        digest, size, signature = hashlib.sha256(), 0, b""
                        with client.stream("GET", path.format(ref), headers=headers() | (extra or {})) as response:
                            check(ref + "_200", response.status_code == 200)
                            check(
                                ref + "_headers",
                                all(
                                    response.headers.get(key) == value
                                    for key, value in {
                                        "content-type": "application/pdf",
                                        "cache-control": "private, no-store",
                                        "x-content-type-options": "nosniff",
                                        "accept-ranges": "none",
                                        "content-disposition": 'inline; filename="document.pdf"',
                                    }.items()
                                )
                                and "location" not in response.headers,
                            )
                            expected_size = int(response.headers["content-length"])
                            for chunk in response.iter_bytes(65536):
                                if not signature:
                                    signature = chunk[:5]
                                digest.update(chunk)
                                size += len(chunk)
                            check(ref + "_complete_pdf", signature == b"%PDF-" and size == expected_size)
                        return {"bytes": size, "sha256": digest.hexdigest()}

                    measurements["s3"] = read_pdf("s3")
                    for value in ("bytes=0-10", "bytes=999999999999-", "bytes=0-1,4-5", "malformed"):
                        check("range_is_full_response", read_pdf("s3", {"Range": value, "If-Range": '"unknown"'}) == measurements["s3"])
                    measurements["legacy"] = read_pdf("legacy")
                    check("legacy_bytes_match", measurements["legacy"] == payload["legacy"])
                    check("legacy_range_is_full_response", read_pdf("legacy", {"Range": "bytes=0-10"}) == measurements["legacy"])
                    for index in range(2):
                        with client.stream("GET", path.format("legacy"), headers=headers()) as response:
                            check("abort_legacy_started_" + str(index), response.status_code == 200 and bool(next(response.iter_bytes(65536))))
                    check("legacy_pool_reusable_after_aborts", read_pdf("legacy") == measurements["legacy"])
                    for label, ref, who, ministry, target in (
                        ("peer", "s3", "peer", "matte", document_turn),
                        ("wrong_ministry", "s3", "author", "mi", document_turn),
                        ("foreign_document", "foreign", "author", "matte", document_turn),
                        ("unserved_candidate", payload["unserved_ref"], "author", "matte", document_turn),
                        ("missing_storage", "missing", "author", "matte", document_turn),
                        ("collective", "s3", "author", "matte", state["turns"]["collective"]),
                        ("other_run", "s3", "author", "matte", state["turns"]["peer"]),
                    ):
                        response = client.get("documents/" + ref + "/content", params={"completion_id": target}, headers=headers(who, ministry))
                        check(label + "_document_refused", response.status_code == 404 and response.json()["error"]["code"] == "document_not_found")
                    MinistryGrant.objects.filter(group_id=state["group"], ministry="matte").delete()
                    try:
                        check(
                            "withdrawn_document_refused",
                            client.get(path.format("s3"), headers=headers(ministry=None) | {"Range": "bytes=0-10"}).status_code == 404,
                        )
                    finally:
                        MinistryGrant.objects.get_or_create(group_id=state["group"], ministry="matte")
                    check("expired_document_refused", client.get(path.format("s3"), headers=headers(expired=True)).status_code == 401)
                    check("restored_document_access", read_pdf("s3") == measurements["s3"])
                elif args.phase == "clients":
                    from openai import AsyncOpenAI
                    from pydantic_ai import Agent

                    class Capture:
                        def attach(self, response):
                            remember(response.request.headers["Authorization"].removeprefix("Bearer "))

                    async def clients():
                        async with AsyncOpenAI(
                            api_key="delegated-at-send-time",
                            base_url=base,
                            max_retries=0,
                            http_client=httpx.AsyncClient(
                                auth=delegation.DelegationAuth(state["author"], state["author_session"], "matte", Capture()), trust_env=False
                            ),
                        ) as sdk:
                            result = await sdk.chat.completions.create(model="assistant-rh-matte", messages=[{"role": "user", "content": "Bonjour"}])
                            check("openai_sdk_completion", bool(result.choices[0].message.content) and result.id.startswith("chatcmpl-"))
                        model = delegation.delegated_model(
                            settings.LLM_CONFIGURATIONS["assistant-rh-matte"], (state["author"], state["author_session"], "matte", Capture())
                        )
                        result = await Agent(model).run("Bonjour")
                        check("conversations_provider_completion", bool(result.output))

                    asyncio.run(clients())
                elif args.phase == "streaming":
                    streams = []
                    for cancel in (False, True):
                        began, pings, texts, done = time.monotonic(), 0, 0, False
                        first_text = None
                        with client.stream(
                            "POST",
                            "chat/completions",
                            headers=headers(),
                            json={
                                "model": "assistant-rh-matte",
                                "stream": True,
                                "messages": [
                                    {
                                        "role": "user",
                                        "content": "Quelles sont les règles du congé de formation professionnelle pour un agent contractuel ?",
                                    }
                                ],
                            },
                        ) as response:
                            check("stream_http_200", response.status_code == 200)
                            check("stream_event_type", response.headers.get("content-type", "").startswith("text/event-stream"))
                            for line in response.iter_lines():
                                if line.startswith(":"):
                                    pings += 1
                                elif line == "data: [DONE]":
                                    done = True
                                elif line.startswith("data: "):
                                    event = json.loads(line[6:])
                                    if any(choice.get("delta", {}).get("content") for choice in event.get("choices", [])):
                                        texts += 1
                                        if first_text is None:
                                            first_text = round(time.monotonic() - began, 3)
                                if cancel and (pings or texts):
                                    break
                        streams.append(
                            {
                                "cancelled_by_client": cancel,
                                "duration_s": round(time.monotonic() - began, 3),
                                "first_text_s": first_text,
                                "pings": pings,
                                "text_frames": texts,
                                "done": done,
                            }
                        )
                        check("stream_interrupted" if cancel else "stream_completed", not done if cancel else done and texts > 0)
                    measurements["streams"] = streams
                elif args.phase == "goldset":
                    capture = args.state.parent / "answers.jsonl"
                    started = time.monotonic()
                    transports = []
                    with capture.open("x") as output, ThreadPoolExecutor(max_workers=2) as executor:
                        capture.chmod(0o600)

                        def complete(row, authorization):
                            began = time.monotonic()
                            response = client.post(
                                "chat/completions",
                                headers=authorization,
                                json={"model": row["model"], "stream": False, "messages": [{"role": "user", "content": row["question"]}]},
                            )
                            return response, round(time.monotonic() - began, 3)

                        for index in range(0, len(payload["panel"]), 2):
                            batch = payload["panel"][index : index + 2]
                            futures = [executor.submit(complete, row, headers(ministry=row["model"].removeprefix("assistant-rh-"))) for row in batch]
                            for row, future in zip(batch, futures, strict=True):
                                response, elapsed = future.result()
                                output.write(
                                    json.dumps({"id": row["id"], "model": row["model"], "status": response.status_code, "payload": response.json()})
                                    + "\n"
                                )
                                output.flush()
                                transports.append({"id": row["id"], "model": row["model"], "http_status": response.status_code, "elapsed_s": elapsed})
                                check("goldset_http_" + str(row["id"]), response.status_code == 200)
                            print("Goldset completed " + str(len(transports)), file=sys.stderr, flush=True)
                    measurements.update(transport=transports, elapsed_s=round(time.monotonic() - started, 3))
                else:
                    raise ValueError("Unknown recipe phase")
    finally:
        save()
        print(json.dumps({"phase": args.phase, "checks": checks, "measurements": measurements}))


if __name__ == "__main__":
    main()

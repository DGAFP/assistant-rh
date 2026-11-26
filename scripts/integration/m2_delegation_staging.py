"""Probe deployed delegation from Conversations, without creating RAG runs.

Execute with the staging backend's Python via kubectl exec -i. Synthetic Django
users, grants and cache sessions are removed even on failure. Assertions and
session keys never enter the report. This does not qualify feedback writes or
document delivery: those require separately owned RAG fixtures.
"""

import base64
import importlib
import importlib.metadata
import json
import secrets
import uuid
from datetime import datetime, timezone

import django
import httpx


def main():
    django.setup()
    from chat.assistant_rh_delegation import sign_assertion
    from core.models import MinistryGrant
    from django.conf import settings
    from django.contrib.auth import get_user_model
    from django.contrib.auth.models import Group
    from django.test import Client
    from rest_framework.exceptions import PermissionDenied

    expected = "http://assistant-rh-api.assistant-rh-api.svc.cluster.local:8000/v1"
    if settings.ASSISTANT_RH_API_URL.rstrip("/") != expected:
        raise RuntimeError("Refusing a target other than the private staging API")
    marker = "m2-probe-" + secrets.token_hex(8)
    users, sessions, replay_jtis, checks = [], [], [], []
    group = None
    report = {
        "schema": "assistant-rh-m2-delegation-probe-v1",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "scope": "deployed delegation and missing-resource refusals; no RAG fixtures or inference",
        "versions": {name: importlib.metadata.version(name) for name in ("openai", "pydantic-ai-slim", "django", "httpx")},
        "checks": checks,
    }

    def check(name, condition):
        checks.append({"name": name, "passed": bool(condition)})
        if not condition:
            raise RuntimeError("Staging check failed: " + name)

    def login(user):
        client = Client()
        client.force_login(user, backend="django.contrib.auth.backends.ModelBackend")
        session = client.session
        sessions.append((session, session.session_key))
        return session.session_key

    def token(user, session_key, ministry="matte"):
        signed = sign_assertion(user.pk, session_key, ministry)
        payload = signed.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        # Keep cleanup identifiers in the operator's private capture, outside Git.
        replay_jtis.append(claims["jti"])
        return signed

    def refused(user, session_key, ministry="matte"):
        try:
            sign_assertion(user.pk, session_key, ministry)
        except PermissionDenied:
            return True
        return False

    try:
        group = Group.objects.create(name=marker)
        grant = MinistryGrant.objects.create(group=group, ministry="matte")
        for index in range(2):
            user = get_user_model()(email=f"{marker}-{index}@example.invalid", sub=f"{marker}-{index}", assistant_rh_admitted=True, is_active=True)
            user.set_unusable_password()
            user.save()
            users.append(user)
            user.groups.add(group)
        author, other = users
        first_session, other_session = (login(user) for user in users)
        unknown = uuid.uuid4().hex
        with httpx.Client(base_url=expected + "/", timeout=15, follow_redirects=False, trust_env=False) as client:
            check("health", client.get(expected.removesuffix("/v1") + "/healthz").status_code == 200)
            check("unauthenticated_catalog", client.get("models").status_code == 401)
            check("invalid_assertion", client.get("models", headers={"Authorization": "Bearer invalid"}).status_code == 401)
            signed = token(author, first_session)
            response = client.get("models", headers={"Authorization": "Bearer " + signed})
            check("current_catalog", response.status_code == 200 and [row["id"] for row in response.json()["data"]] == ["assistant-rh-matte"])
            check("assertion_replay", client.get("models", headers={"Authorization": "Bearer " + signed}).status_code == 401)
            for label, user, session_key in (("author", author, first_session), ("same_group_peer", other, other_session)):
                for method, path, body in (
                    ("GET", "feedback/" + unknown, None),
                    ("POST", "feedback", {"completion_id": unknown, "stars": 3, "comment": "Synthetic M2 probe"}),
                    ("GET", "documents/missing/content?completion_id=" + unknown, None),
                ):
                    response = client.request(method, path, json=body, headers={"Authorization": "Bearer " + token(user, session_key)})
                    check(label + "_missing_" + method.lower() + "_" + path.split("/")[0], response.status_code == 404)
                    check(label + "_safe_headers_" + method.lower() + "_" + path.split("/")[0], response.headers.get("cache-control") == "no-store")
            reconnect = login(author)
            check("reconnected_identity", author.pk == get_user_model().objects.get(pk=author.pk).pk)
            response = client.get("models", headers={"Authorization": "Bearer " + token(author, reconnect)})
            check("reconnected_delegation", response.status_code == 200)
            grant.delete()
            check("withdrawal_blocks_signing", refused(author, reconnect))
            response = client.get("models", headers={"Authorization": "Bearer " + token(author, reconnect, None)})
            check("withdrawal_empties_catalog", response.status_code == 200 and response.json()["data"] == [])
            MinistryGrant.objects.create(group=group, ministry="matte")
            check("rights_restored", client.get("models", headers={"Authorization": "Bearer " + token(author, reconnect)}).status_code == 200)
            author.is_active = False
            author.save(update_fields=["is_active"])
            check("deactivation_blocks_signing", refused(author, reconnect))
            author.is_active = True
            author.save(update_fields=["is_active"])
            sessions[-1][0].delete()
            check("logout_blocks_signing", refused(author, reconnect))
    finally:
        for session, key in sessions:
            session.delete(key)
        for user in users:
            user.delete()
        if group is not None:
            group.delete()
        report["cleanup"] = {
            "users_removed": not get_user_model().objects.filter(email__startswith=marker).exists(),
            "group_removed": not Group.objects.filter(name=marker).exists(),
            "sessions_removed": all(not session.exists(key) for session, key in sessions),
            "rag_runs_created": 0,
        }
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        print(json.dumps({"report": report, "private_replay_jtis": replay_jtis}))


if __name__ == "__main__":
    main()

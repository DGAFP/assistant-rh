"""Local HTTP recipe: real Conversations/API/Postgres; synthetic OIDC and AI only.

Run with the API virtualenv and --conversations pointing at the identity fork.
Creates and removes its own Docker database; never reads a developer .env.
"""

from __future__ import annotations

import argparse
import base64
import ipaddress
import json
import os
import secrets
import socket
import ssl
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import psycopg
from assistant_rh_api.db.dsn import validate_libpq_environment
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa
from cryptography.x509.oid import NameOID
from psycopg.types.json import Jsonb

ROOT = Path(__file__).resolve().parents[2]


def b64(value):
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--conversations", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    validate_libpq_environment(os.environ)
    fork = args.conversations.resolve()
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    os.chmod(out, 0o700)
    fork_python = fork / "src/backend/.venv/bin/python"
    api_python = ROOT / ".venv/bin/python"
    db_port, api_port, replica_port, conv_port = [free_port() for _ in range(4)]
    conv = f"http://127.0.0.1:{conv_port}"
    api = f"http://127.0.0.1:{api_port}"
    results, processes, logs = [], [], []
    succeeded = False
    started = datetime.now(timezone.utc).isoformat()
    codes, tokens, provider_calls = {}, {}, []
    oidc_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    numbers = oidc_key.public_key().public_numbers()
    fail_primary = False

    class ExternalServices(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass  # Authorization codes and tokens must never enter request logs.

        def reply(self, data, status=200, content_type="application/json"):
            body = data.encode() if isinstance(data, str) else json.dumps(data).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            parsed = urlsplit(self.path)
            query = parse_qs(parsed.query)
            if parsed.path == "/authorize":
                code = secrets.token_urlsafe(24)
                codes[code] = {"subject": query["subject"][0], "nonce": query["nonce"][0]}
                self.send_response(302)
                self.send_header("Location", query["redirect_uri"][0] + "?" + urlencode({"code": code, "state": query["state"][0]}))
                self.end_headers()
            elif parsed.path == "/jwks":
                self.reply(
                    {
                        "keys": [
                            {
                                "kty": "RSA",
                                "kid": "local-oidc",
                                "alg": "RS256",
                                "use": "sig",
                                "n": b64(numbers.n.to_bytes(256, "big")),
                                "e": b64(numbers.e.to_bytes(3, "big")),
                            }
                        ]
                    }
                )
            elif parsed.path == "/userinfo":
                subject = tokens[self.headers["Authorization"].removeprefix("Bearer ")]
                self.reply(
                    {"sub": subject, "email": subject + "@example.invalid", "email_verified": True, "first_name": subject, "last_name": "Local"}
                )
            elif parsed.path == "/logout":
                self.send_response(302)
                self.send_header("Location", query["post_logout_redirect_uri"][0] + "?" + urlencode({"state": query["state"][0]}))
                self.end_headers()
            else:
                self.reply({"error": "not_found"}, 404)

        def do_POST(self):
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            if self.path == "/token":
                grant = codes.pop(parse_qs(raw.decode())["code"][0])
                access = secrets.token_urlsafe(24)
                tokens[access] = grant["subject"]
                claims = {
                    "iss": external,
                    "aud": "conversations",
                    "sub": grant["subject"],
                    "nonce": grant["nonce"],
                    "iat": int(time.time()),
                    "exp": int(time.time()) + 600,
                }
                unsigned = b64(json.dumps({"alg": "RS256", "kid": "local-oidc"}).encode()) + "." + b64(json.dumps(claims).encode())
                signed = unsigned + "." + b64(oidc_key.sign(unsigned.encode(), padding.PKCS1v15(), hashes.SHA256()))
                self.reply({"access_token": access, "id_token": signed, "token_type": "Bearer", "expires_in": 600})
                return
            payload = json.loads(raw)
            provider_calls.append({"path": self.path, "model": payload.get("model"), "stream": payload.get("stream", False)})
            if self.path.endswith("/embeddings"):
                self.reply({"data": [{"embedding": [1.0] + [0.0] * 1023}]})
            elif self.path.endswith("/rerank"):
                self.reply({"results": [{"index": i, "relevance_score": 0.9} for i in range(len(payload["documents"]))]})
            elif self.path.endswith("/chat/completions"):
                if payload["model"] == "openweight-medium":
                    answer = '{"intent":"rag_query","confidence":0.99}'
                elif payload["model"] == "synthetic-selector":
                    answer = '{"selected_ids":[0]}'
                else:
                    if fail_primary and self.path.startswith("/albert/"):
                        self.reply({"error": "synthetic primary outage"}, 503)
                        return
                    answer = "Réponse synthétique selon le guide des congés."
                common = {"id": "synthetic-completion", "created": int(time.time()), "model": payload["model"]}
                if payload.get("stream"):
                    chunks = [
                        dict(common, object="chat.completion.chunk", choices=[{"index": 0, "delta": {"content": answer}, "finish_reason": None}]),
                        dict(common, object="chat.completion.chunk", choices=[{"index": 0, "delta": {}, "finish_reason": "stop"}]),
                    ]
                    self.reply(
                        "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n", content_type="text/event-stream"
                    )
                else:
                    self.reply(
                        dict(
                            common,
                            object="chat.completion",
                            choices=[{"index": 0, "message": {"role": "assistant", "content": answer}, "finish_reason": "stop"}],
                            usage={"prompt_tokens": 8, "completion_tokens": 6, "total_tokens": 14},
                        )
                    )
            else:
                self.reply({"error": "not_found"}, 404)

    server = ThreadingHTTPServer(("127.0.0.1", 0), ExternalServices)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "local recipe")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(oidc_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(timezone.utc) - timedelta(minutes=1))
        .not_valid_after(datetime.now(timezone.utc) + timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(oidc_key, hashes.SHA256())
    )
    (out / "local-ca.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (out / "tls-key.pem").write_bytes(
        oidc_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(out / "local-ca.pem", out / "tls-key.pem")
    server.socket = tls.wrap_socket(server.socket, server_side=True)
    trusted_tls = ssl.create_default_context(cafile=str(out / "local-ca.pem"))
    external = f"https://127.0.0.1:{server.server_port}"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    container = "rh596-recipe-" + secrets.token_hex(4)
    env = {"PATH": os.environ["PATH"], "HOME": str(Path.home()), "LANG": "C.UTF-8", "PYTHONUNBUFFERED": "1"}

    def record(name):
        results.append(name)
        print("PASS " + name, flush=True)

    def command(argv, *, environment=None, cwd=None):
        return subprocess.run(list(map(str, argv)), env=environment or env, cwd=cwd, text=True, capture_output=True, check=True).stdout.strip()

    def launch(name, argv, environment, cwd):
        log = (out / (name + ".log")).open("w")
        logs.append(log)
        process = subprocess.Popen(list(map(str, argv)), env=environment, cwd=cwd, stdout=log, stderr=subprocess.STDOUT)
        processes.append(process)
        return process

    def ready(url, process):
        for _ in range(180):
            if process.poll() is not None:
                raise RuntimeError("Server exited; inspect local service logs")
            try:
                if httpx.get(url, timeout=1).status_code < 500:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.3)
        raise RuntimeError("Server readiness timeout: " + url)

    try:
        command(
            [
                "docker",
                "run",
                "--rm",
                "-d",
                "--name",
                container,
                "-p",
                f"127.0.0.1:{db_port}:5432",
                "-e",
                "POSTGRES_DB=assistant_rh_api_test",
                "-e",
                "POSTGRES_USER=recipe_owner",
                "-e",
                "POSTGRES_PASSWORD=local-recipe-only",
                "pgvector/pgvector:pg17",
            ]
        )
        owner_dsn = f"postgresql://recipe_owner:local-recipe-only@127.0.0.1:{db_port}/assistant_rh_api_test"
        for _ in range(100):
            try:
                with psycopg.connect(owner_dsn, connect_timeout=1):
                    break
            except psycopg.OperationalError:
                time.sleep(0.2)
        with psycopg.connect(owner_dsn, autocommit=True) as db:
            db.execute("CREATE DATABASE conversations")
            for file in (ROOT / "docker/api/bootstrap-local.txt").read_text().splitlines():
                db.execute((ROOT / file).read_text())
            db.execute("CREATE ROLE recipe_api LOGIN PASSWORD 'local-api-only'")
            db.execute("SELECT public.api_attach_runtime_user('recipe_api', 'arh_api')")
            db.execute("GRANT USAGE ON SCHEMA public TO recipe_api")
            db.execute(
                "UPDATE rag_config SET config = %s WHERE id = 1",
                (
                    Jsonb(
                        {
                            "enable_intent_gating": True,
                            "v3_search_mode": "lexical",
                            "v3_enable_selector": True,
                            "v3_selector_model": "synthetic-selector",
                            "v3_generator_model": "synthetic-generator",
                        }
                    ),
                ),
            )
            for index, ministry in enumerate(("matte", "mso"), start=1):
                doc, section = f"00000000-0000-0000-0000-{index:012}", f"00000000-0000-0000-0001-{index:012}"
                db.execute(
                    "INSERT INTO rag_documents VALUES (%s, %s, %s, NULL, %s, %s, 20)",
                    (doc, ministry, "Guide " + ministry, ministry.upper(), "Congés synthétiques " + ministry),
                )
                db.execute(
                    "INSERT INTO rag_sections VALUES (%s, %s, %s, %s, %s, NULL)",
                    (section, doc, "Congés", "Guide > Congés", "Congés synthétiques " + ministry),
                )
                db.execute(
                    f"INSERT INTO rag_chunks_{ministry} (hash_id, chunk_text, section_id) VALUES (%s, 'Congés annuels', %s)",
                    ("chunk-" + ministry, section),
                )
        key = ed25519.Ed25519PrivateKey.generate()
        (out / "signing.pem").write_bytes(
            key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
        )
        (out / "audit.key").write_bytes(secrets.token_bytes(32))
        jwks = json.dumps({"keys": [{"kty": "OKP", "crv": "Ed25519", "kid": "local-recipe", "x": b64(key.public_key().public_bytes_raw())}]})
        api_env = dict(
            env,
            SCW_POSTGRES_DSN=owner_dsn.replace("recipe_owner:local-recipe-only", "recipe_api:local-api-only"),
            CONVERSATIONS_DELEGATION_JWKS=jwks,
            CONVERSATIONS_DELEGATION_ISSUER="local-conversations",
            ASSISTANT_RH_API_AUDIENCE="local-rh-api",
            ALBERT_BASE_URL=external + "/albert/v1",
            ALBERT_API_KEY="synthetic",
            SCALEWAY_BASE_URL=external + "/scaleway/v1",
            SCALEWAY_API_KEY="synthetic",
            APP_ENV="local",
        )
        # Only customize the TLS trust anchor; use the normal API composition and entry point.
        (out / "launch_api.py").write_text(
            "import certifi, os\ncertifi.where = lambda: os.environ['RECIPE_CA']\nfrom assistant_rh_api.__main__ import main\nmain()\n"
        )
        api_env["RECIPE_CA"] = str(out / "local-ca.pem")
        api_env["API_HOST"] = "127.0.0.1"
        for name, port in [("api", api_port), ("api-replica", replica_port)]:
            api_env["API_PORT"] = str(port)
            proc = launch(
                name,
                [api_python, out / "launch_api.py"],
                api_env,
                ROOT,
            )
            ready(f"http://127.0.0.1:{port}/healthz", proc)
        record("two real API replicas with non-owner runtime role and migrated PostgreSQL")
        settings = """import os
from conversations.settings import Test
from chat.llm_configuration import LLModel, LLMProvider
class Recipe(Test):
    ENVIRONMENT = "test"
    DEBUG = False
    ALLOWED_HOSTS = ["127.0.0.1"]
    DATABASES = {"default": {"ENGINE": "django.db.backends.postgresql", "NAME": "conversations",
        "USER": "recipe_owner", "PASSWORD": "local-recipe-only", "HOST": "127.0.0.1", "PORT": DBPORT}}
    CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
    SESSION_ENGINE = "django.contrib.sessions.backends.db"
    SESSION_COOKIE_SECURE = False
    CSRF_COOKIE_SECURE = False
    @property
    def LLM_CONFIGURATIONS(self):
        provider = LLMProvider(hrid="rh", kind="assistant-rh", api_key="unused", base_url=os.environ["ASSISTANT_RH_API_URL"])
        return {"assistant-rh-"+m: LLModel(hrid="assistant-rh-"+m, model_name="assistant-rh-"+m, human_readable_name=m, 
            provider=provider, is_active=True, tools=[], system_prompt="", supports_streaming=(m == "mso")) for m in ("matte", "mso")}
""".replace("DBPORT", str(db_port))
        (out / "recipe_settings.py").write_text(settings)
        conv_env = dict(env)
        for line in (fork / "env.d/test").read_text().splitlines():
            if line and not line.startswith("#"):
                name, value = line.split("=", 1)
                conv_env[name] = value
        conv_env.update(
            REQUESTS_CA_BUNDLE=str(out / "local-ca.pem"),
            DJANGO_SETTINGS_MODULE="recipe_settings",
            DJANGO_CONFIGURATION="Recipe",
            PYTHONPATH=str(out) + ":" + str(fork / "src/backend"),
            ASSISTANT_RH_ENABLED="true",
            ASSISTANT_RH_API_URL=api + "/v1",
            ASSISTANT_RH_DELEGATION_ISSUER="local-conversations",
            ASSISTANT_RH_API_AUDIENCE="local-rh-api",
            ASSISTANT_RH_SIGNING_KID="local-recipe",
            ASSISTANT_RH_SIGNING_KEY_FILE=str(out / "signing.pem"),
            ASSISTANT_RH_AUDIT_KEY_FILE=str(out / "audit.key"),
            OIDC_FALLBACK_TO_EMAIL_FOR_IDENTIFICATION="false",
            LOGIN_REDIRECT_URL=conv + "/api/v1.0/users/me/",
            LOGIN_REDIRECT_URL_FAILURE=conv,
            LOGOUT_REDIRECT_URL=conv,
            OIDC_OP_JWKS_ENDPOINT=external + "/jwks",
            OIDC_OP_AUTHORIZATION_ENDPOINT=external + "/authorize",
            OIDC_OP_TOKEN_ENDPOINT=external + "/token",
            OIDC_OP_USER_ENDPOINT=external + "/userinfo",
            OIDC_OP_LOGOUT_ENDPOINT=external + "/logout",
        )
        manage = [fork_python, fork / "src/backend/manage.py"]
        command([*manage, "migrate", "--noinput", "--skip-checks"], environment=conv_env, cwd=fork)

        def django(code):
            return command(
                [fork_python, "-c", "from configurations import importer; importer.install(); import django; django.setup(); " + code],
                environment=conv_env,
                cwd=fork,
            )

        proc = launch("conversations", [*manage, "runserver", f"127.0.0.1:{conv_port}", "--noreload", "--nothreading"], conv_env, fork)
        ready(conv + "/api/v1.0/config/", proc)
        base = conv + "/api/v1.0/"

        def login(subject):
            client = httpx.Client(timeout=120, verify=trusted_tls)
            response = client.get(base + "authenticate/")
            assert response.status_code == 302, response.text
            authorized = client.get(response.headers["location"] + "&" + urlencode({"subject": subject}))
            response = client.get(authorized.headers["location"])
            assert response.status_code == 302, response.text
            return client, response

        alice, pending = login("alice")
        assert "/unauthorized/" in pending.headers["location"]
        alice.close()
        django(
            "from core.models import User, MinistryGrant; from django.contrib.auth.models import Group; g=Group.objects.create(name='RH'); "
            "MinistryGrant.objects.create(group=g,ministry='matte'); MinistryGrant.objects.create(group=g,ministry='mso'); "
            "u=User.objects.get(sub='alice'); u.assistant_rh_admitted=True; u.save(); u.groups.add(g)"
        )
        alice, response = login("alice")
        assert "/users/me/" in response.headers["location"], response.headers["location"]
        alice_id = str(alice.get(base + "users/me/").json()["id"])
        bob, pending = login("bob")
        bob.close()
        django(
            "from core.models import User; from django.contrib.auth.models import Group; u=User.objects.get(sub='bob'); "
            "u.assistant_rh_admitted=True; u.save(); u.groups.add(Group.objects.get(name='RH'))"
        )
        bob, _ = login("bob")
        bob_id = str(bob.get(base + "users/me/").json()["id"])
        assert alice_id != bob_id
        record("OIDC authorization-code HTTP flow, pending admission then stable distinct UUIDs")

        def post(client, path, body):
            return client.post(base + path, json=body, headers={"X-CSRFToken": client.cookies["csrftoken"]})

        def catalogue(client):
            response = client.get(base + "llm-configuration/")
            assert response.status_code == 200, response.text
            return [model["hrid"] for model in response.json()["models"]]

        assert set(catalogue(alice)) == {"assistant-rh-matte", "assistant-rh-mso"}
        assert post(alice, "chats/", {"title": "forbidden", "model_hrid": "assistant-rh-mi"}).status_code == 400
        record("multi-corpus catalogue and forged out-of-scope model refused")

        def chat(client, ministry):
            created = post(client, "chats/", {"title": "Congés", "model_hrid": "assistant-rh-" + ministry})
            assert created.status_code == 201, created.text
            chat_id = created.json()["id"]
            answer = post(
                client,
                f"chats/{chat_id}/conversation/",
                {"messages": [{"id": secrets.token_hex(8), "role": "user", "parts": [{"type": "text", "text": "Congés annuels"}]}]},
            )
            assert answer.status_code == 200, answer.text
            events = [json.loads(line[6:]) for line in answer.text.splitlines() if line.startswith("data: {")]
            assert "Réponse synthétique" in "".join(item.get("delta", "") for item in events), answer.text
            assert "[DONE]" in answer.text
            return chat_id

        alice_chat = chat(alice, "matte")
        bob_chat = chat(bob, "mso")
        assert alice.get(base + f"chats/{bob_chat}/").status_code == 404
        assert bob.get(base + f"chats/{alice_chat}/").status_code == 404
        with psycopg.connect(owner_dsn) as db:
            runs = db.execute(
                "SELECT turn_id, author_user_id::text, selected_ministry, api_record->>'status', answer FROM chat_runs ORDER BY ts"
            ).fetchall()
            assert len(runs) == 2, runs
            assert {(row[1], row[2]) for row in runs} == {(alice_id, "matte"), (bob_id, "mso")}, runs
            assert all(row[3] == "completed" and "Réponse synthétique" in row[4] for row in runs), runs
            sources = db.execute(
                "SELECT r.selected_ministry, s.document_id::text FROM chat_run_sources s JOIN chat_runs r USING (turn_id)"
            ).fetchall()
            assert set(sources) == {("matte", "00000000-0000-0000-0000-000000000001"), ("mso", "00000000-0000-0000-0000-000000000002")}, sources
        record("Conversations -> real API JSON and SSE -> completed runs with distinct authors")
        fail_primary = True
        chat(alice, "mso")
        fail_primary = False
        with psycopg.connect(owner_dsn) as db:
            latest = db.execute("SELECT author_user_id::text, selected_ministry, provider FROM chat_runs ORDER BY ts DESC LIMIT 1").fetchone()
            assert latest == (alice_id, "mso", "scaleway"), latest
        record("Albert/Scaleway HTTP fallback preserves individual author and ministry")
        django("from core.models import MinistryGrant; MinistryGrant.objects.filter(ministry='matte').delete()")
        assert catalogue(alice) == ["assistant-rh-mso"]
        assert alice.get(base + f"chats/{alice_chat}/").status_code == 404
        assert post(alice, f"chats/{alice_chat}/conversation/", {"messages": []}).status_code == 404
        django(
            "from core.models import MinistryGrant; from django.contrib.auth.models import Group; "
            "MinistryGrant.objects.create(group=Group.objects.get(name='RH'),ministry='matte')"
        )
        assert alice.get(base + f"chats/{alice_chat}/").status_code == 200
        record("live grant withdrawal blocks chat and hides history; restoration recovers it")

        def signed(client, user_id, ministry=None):
            session = client.cookies["sessionid"]
            return django(f"from chat.assistant_rh_delegation import sign_assertion; print(sign_assertion({user_id!r}, {session!r}, {ministry!r}))")

        token = signed(alice, alice_id)
        first = httpx.get(api + "/v1/models", headers={"Authorization": "Bearer " + token})
        replay = httpx.get(f"http://127.0.0.1:{replica_port}/v1/models", headers={"Authorization": "Bearer " + token})
        assert first.status_code == 200 and replay.status_code == 401
        record("shared PostgreSQL anti-replay rejects reuse on a second API process")
        turn = next(row[0] for row in runs if row[1] == alice_id)

        def feedback(client, user_id, method, path, **kwargs):
            return httpx.request(method, api + path, headers={"Authorization": "Bearer " + signed(client, user_id, "matte")}, **kwargs)

        assert (
            feedback(
                alice, alice_id, "POST", "/v1/feedback", json={"completion_id": "chatcmpl-" + turn, "stars": 5, "comment": "Synthetic"}
            ).status_code
            == 204
        )
        assert feedback(bob, bob_id, "GET", "/v1/feedback/chatcmpl-" + turn).status_code == 404
        assert feedback(alice, alice_id, "GET", "/v1/feedback/chatcmpl-" + turn).status_code == 200
        record("real API feedback persistence and same-group cross-user isolation")
        old_session = alice.cookies["sessionid"]
        logout = alice.get(base + "logout/")
        oidc_logout = alice.get(logout.headers["location"])
        assert alice.get(oidc_logout.headers["location"]).status_code == 302
        assert alice.get(base + "llm-configuration/").status_code in (401, 403)
        alice.close()
        alice, _ = login("alice")
        assert str(alice.get(base + "users/me/").json()["id"]) == alice_id
        assert alice.cookies["sessionid"] != old_session
        renewed = signed(alice, alice_id)

        def audit(assertion):
            segment = assertion.split(".")[1]
            return json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))["audit_session"]

        assert audit(renewed) != audit(token)
        django("from core.models import User; u=User.objects.get(sub='alice'); u.is_active=False; u.save(); u.is_active=True; u.save()")
        assert alice.get(base + "llm-configuration/").status_code in (401, 403)
        alice.close()
        alice, _ = login("alice")
        assert catalogue(alice)
        session = alice.cookies["sessionid"]
        django(
            f"from django.contrib.sessions.models import Session; from django.utils import timezone; from datetime import timedelta; "
            f"Session.objects.filter(session_key={session!r}).update(expire_date=timezone.now()-timedelta(seconds=1))"
        )
        assert alice.get(base + "llm-configuration/").status_code in (401, 403)
        alice.close()
        bob.close()
        record("logout, stable identity on reconnection, disable/reactivate revocation and expiry")
        conv_log = (out / "conversations.log").read_text()
        api_log = (out / "api.log").read_text()
        import re

        ids = re.findall(r"assistant_rh_request request_id=([0-9a-f]{32})", conv_log)
        assert len(ids) >= 3 and all(f"request_id={value} status=200" in api_log for value in ids)
        assert token not in conv_log + api_log
        record("matching request correlation without signed assertion in service logs")
        succeeded = True
    finally:
        for proc in reversed(processes):
            proc.terminate()
        for proc in processes:
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        for log in logs:
            log.close()
        server.shutdown()
        subprocess.run(["docker", "stop", container], capture_output=True, check=False)
        report = {
            "success": succeeded,
            "started_at": started,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "api_commit": command(["git", "rev-parse", "HEAD"], cwd=ROOT),
            "fork_commit": command(["git", "rev-parse", "HEAD"], cwd=fork),
            "passed": results,
            "simulated": ["OIDC provider (not ProConnect)", "Albert and Scaleway inference endpoints"],
            "provider_calls": provider_calls,
        }
        (out / "results.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
        for file in ("signing.pem", "audit.key", "tls-key.pem"):
            (out / file).unlink(missing_ok=True)


if __name__ == "__main__":
    main()

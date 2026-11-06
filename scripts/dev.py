"""Local Conversations/API stack. Remote access is confined to the explicit dump source."""

from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import json
import os
import secrets
import shlex
import subprocess
import sys
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / ".dev"
FORK_URL = "https://github.com/DGAFP/conversations.git"
FORK_COMMIT = "b3d3018a93759515b0c828f58fb8d9c1d9182e78"
# Same image as the db service: it also provides an OpenSSL with Ed25519 (macOS ships LibreSSL).
PG_IMAGE = "pgvector/pgvector:pg17"
# Daemon selection is resolved once and passed explicitly; these must not reach Docker or Compose.
DOCKER_TARGET = ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH")
# The previous copy moves aside in the same transaction and is dropped only after the promotion.
SWAP_SQL = """
BEGIN;
DO $$
BEGIN
    IF EXISTS (SELECT FROM pg_database WHERE datname = 'assistant_rh_previous') THEN
        ALTER DATABASE assistant_rh_previous RENAME TO assistant_rh_retired;
    END IF;
END
$$;
ALTER DATABASE assistant_rh RENAME TO assistant_rh_previous;
ALTER DATABASE assistant_rh_import RENAME TO assistant_rh;
COMMIT;
"""
REOPEN_SQL = """
DO $$
DECLARE name TEXT;
BEGIN
    FOR name IN SELECT datname FROM pg_database WHERE datname IN ('assistant_rh', 'assistant_rh_previous') AND NOT datallowconn LOOP
        EXECUTE format('ALTER DATABASE %I WITH ALLOW_CONNECTIONS true', name);
    END LOOP;
END
$$;
"""
TABLES = (
    "rag_documents",
    "rag_sections",
    "rag_config",
    "system_prompts",
    "acronyms",
    "rag_chunks_matte",
    "rag_chunks_mso",
    "rag_chunks_mi",
    "rag_chunks_masa",
    "rag_chunks_service_public",
    "rag_chunks_dgafp",
    "rag_chunks_rgrh",
)
PROVIDER_KEYS = ("ALBERT_API_KEY", "ALBERT_BASE_URL", "ALBERT_EMBED_MODEL", "ALBERT_RERANK_MODEL", "SCALEWAY_API_KEY", "SCALEWAY_BASE_URL")


def read_env(path):
    """Read simple dotenv values without executing shell code or expanding variables."""
    values = {}
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip().removeprefix("export ")
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            value = value.strip()
            if value.startswith(("'", '"')):
                parts = shlex.split(value, comments=True)
                if len(parts) != 1:
                    raise ValueError(f"Valeur .env invalide pour {key}")
                value = parts[0]
            else:
                value = value.split(" #", 1)[0].rstrip()
            values[key.strip()] = value
    return values


def write_private(path, content):
    """Keys, provider credentials and database snapshots stay outside version control."""
    with open(path, "w", opener=lambda name, flags: os.open(name, flags, 0o600)) as file:
        file.write(content)
    path.chmod(0o600)


def write_env(path, values):
    if any("\n" in str(value) or "\r" in str(value) for value in values.values()):
        raise ValueError("Les variables locales doivent tenir sur une ligne.")
    write_private(path, "".join(f"{key}={value}\n" for key, value in values.items()))


def source_dsn(values):
    """Require an explicit staging URI; never fall back to the runtime/production DSN."""
    value = values.get("SCW_POSTGRES_DSN_STAGING", "")
    source = urlsplit(value)
    if source.scheme not in ("postgres", "postgresql") or not source.hostname or not source.username or not source.path.strip("/"):
        raise ValueError("Configurer SCW_POSTGRES_DSN_STAGING (URI PostgreSQL explicite) dans .env.")
    query = parse_qs(source.query)
    if set(query) - {"sslmode", "connect_timeout", "application_name"}:
        raise ValueError("Paramètres de DSN staging non supportés ; utiliser host/port/base dans l'URI.")
    production = values.get("SCW_POSTGRES_DSN_PROD", "").strip()
    if production:
        try:
            target = endpoint(production)
        except ValueError:
            raise ValueError("SCW_POSTGRES_DSN_PROD est illisible : import refusé par prudence.") from None
        if endpoint(value) == target:
            raise ValueError("La source staging désigne aussi la production : import refusé.")
    return value


def endpoint(dsn):
    """(host, port, database) of a libpq URI or key=value DSN, to compare servers."""
    if "://" in dsn:
        parts = urlsplit(dsn)
        return parts.hostname, parts.port or 5432, unquote(parts.path.lstrip("/"))
    fields = dict(item.split("=", 1) for item in shlex.split(dsn) if "=" in item)
    return (fields.get("host") or "").lower() or None, int(fields.get("port") or 5432), fields.get("dbname")


def run(args, **kwargs):
    return subprocess.run([str(arg) for arg in args], check=True, **kwargs)


def dump_environment(dsn):
    """Send libpq fields through the environment, never a password-bearing command argument."""
    source = urlsplit(dsn)
    query = parse_qs(source.query)
    return {
        "PGHOST": source.hostname,
        "PGPORT": str(source.port or 5432),
        "PGUSER": unquote(source.username),
        "PGPASSWORD": unquote(source.password or ""),
        "PGDATABASE": unquote(source.path.lstrip("/")),
        "PGSSLMODE": query.get("sslmode", ["require"])[0],
        "PGCONNECT_TIMEOUT": query.get("connect_timeout", ["10"])[0],
        "PGAPPNAME": query.get("application_name", ["assistant-rh-make-dump"])[0],
        "PGOPTIONS": "-c default_transaction_read_only=on",
    }


def restore_list(toc):
    """The API uses rag_documents; its obsolete link to legacy documents is out of scope."""
    legacy = " FK CONSTRAINT public rag_documents rag_documents_legacy_doc_id_fkey "
    return "\n".join(line for line in toc.splitlines() if legacy not in line) + "\n"


class Stack:
    def __init__(self, values):
        self.values = values
        self.fork = Path(values.get("CONVERSATIONS_DIR") or STATE / "conversations").expanduser().resolve()
        self.project = "rh-dev-" + hashlib.sha256(str(ROOT).encode()).hexdigest()[:10]
        # Compose must not discover a caller's project or files; the daemon is pinned by daemon().
        self.env = {key: value for key, value in os.environ.items() if not key.startswith(("COMPOSE_", "PG")) and key not in DOCKER_TARGET}
        self._daemon = None
        self.ports = {
            name: int(values.get("DEV_" + name + "_PORT", default))
            for name, default in (
                ("FRONTEND", 3000),
                ("BACKEND", 8071),
                ("API", 8000),
                ("DB", 55435),
            )
        }
        if len(set(self.ports.values())) != 4 or any(not 1024 <= port <= 65535 for port in self.ports.values()):
            raise ValueError("Choisir quatre ports DEV_*_PORT distincts entre 1024 et 65535.")

    def daemon(self):
        """Resolve the caller's Docker endpoint once; staging data may only reach a local socket."""
        if self._daemon is None:
            host = os.environ.get("DOCKER_HOST", "")
            if not host:
                # Honors DOCKER_CONTEXT or the current context (desktop-linux, colima, orbstack…).
                inspect = run(["docker", "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"], capture_output=True, text=True)
                host = inspect.stdout.strip()
            if not host.startswith(("unix://", "npipe://")):
                raise ValueError(f"Le démon Docker doit être local (socket unix) ; endpoint actuel : {host or 'inconnu'}.")
            self._daemon = host
        return self._daemon

    def docker(self, *args, **kwargs):
        env = {**self.env, **kwargs.pop("extra_env", {})}
        return run(["docker", "--host", self.daemon(), *args], env=env, **kwargs)

    def openssl(self, *args, data):
        return self.docker("run", "--rm", "-i", "--network", "none", "--entrypoint", "openssl", PG_IMAGE, *args, input=data, capture_output=True)

    def compose(self, *args, **kwargs):
        return self.docker(
            "compose",
            "--project-name",
            self.project,
            "--project-directory",
            ROOT,
            "--env-file",
            STATE / "compose.env",
            "-f",
            ROOT / "docker/dev/compose.yml",
            *args,
            **kwargs,
        )

    def prepare(self):
        """Generate persistent local identity material; pass only provider keys to the API."""
        (STATE / "dumps").mkdir(mode=0o700, exist_ok=True)
        key, public_key = STATE / "signing.pem", STATE / "signing.pub"
        if not key.exists():
            # Written 0600 from the start; the host OpenSSL is not required.
            write_private(key, self.openssl("genpkey", "-algorithm", "ED25519", data=b"").stdout.decode())
            public_key.unlink(missing_ok=True)
        if not public_key.exists():
            der = self.openssl("pkey", "-pubout", "-outform", "DER", data=key.read_bytes()).stdout
            write_private(public_key, base64.urlsafe_b64encode(der[-32:]).decode().rstrip("="))
        if not (STATE / "audit.key").exists():
            write_private(STATE / "audit.key", secrets.token_hex(32))
        for name in ("password", "django-secret"):
            if not (STATE / name).exists():
                write_private(STATE / name, secrets.token_urlsafe(32))
        jwks = {
            "keys": [
                {
                    "kty": "OKP",
                    "crv": "Ed25519",
                    "kid": "local-dev",
                    "alg": "EdDSA",
                    "use": "sig",
                    "x": public_key.read_text().strip(),
                }
            ]
        }
        write_env(
            STATE / "api.env",
            {
                **{key: self.values[key] for key in PROVIDER_KEYS if self.values.get(key)},
                "CONVERSATIONS_DELEGATION_JWKS": json.dumps(jwks),
                "CONVERSATIONS_DELEGATION_ISSUER": "conversations-local",
                "ASSISTANT_RH_API_AUDIENCE": "assistant-rh-local",
            },
        )
        write_env(
            STATE / "conversations.env",
            {
                "DJANGO_SECRET_KEY": (STATE / "django-secret").read_text(),
                "LOCAL_DEV_PASSWORD": (STATE / "password").read_text(),
                "LOGIN_REDIRECT_URL": "/",
                "LOGIN_REDIRECT_URL_FAILURE": "/",
                "LOGOUT_REDIRECT_URL": "/",
            },
        )
        # --env-file is interpolated by Compose; quote paths literally (not provider secrets).
        compose_values = {
            "CONVERSATIONS_DIR": str(self.fork),
            "DEV_UID": os.getuid(),
            **{"DEV_" + name + "_PORT": port for name, port in self.ports.items()},
        }
        if any("'" in str(value) or "\n" in str(value) for value in compose_values.values()):
            raise ValueError("Le chemin du fork ne doit pas contenir d'apostrophe ni de saut de ligne.")
        write_private(STATE / "compose.env", "".join(f"{key}='{value}'\n" for key, value in compose_values.items()))
        write_private(
            STATE / "models.json",
            json.dumps(
                {
                    "providers": [{"hrid": "rh", "kind": "assistant-rh", "base_url": "http://api:8000/v1", "api_key": "unused"}],
                    "models": [
                        {
                            "hrid": "assistant-rh-" + ministry,
                            "model_name": "assistant-rh-" + ministry,
                            "human_readable_name": ministry.upper(),
                            "provider_name": "rh",
                            "is_active": True,
                            "supports_streaming": True,
                            "system_prompt": "",
                            "tools": [],
                        }
                        for ministry in ("matte", "mso", "mi", "masa")
                    ],
                }
            ),
        )

    def checkout(self):
        """Clone the pinned fork when no working copy was supplied; never reset user work."""
        supplied = bool(self.values.get("CONVERSATIONS_DIR"))
        if not self.fork.exists():
            if supplied:
                raise ValueError("CONVERSATIONS_DIR n'existe pas.")
            run(["git", "clone", "--no-checkout", FORK_URL, self.fork])
        # A --no-checkout clone has no index yet: extract it even when its default branch already holds the pin.
        if not supplied and (not (self.fork / ".git/index").exists() or not self.contains_pin()):
            # The pin may be reachable only from refs/pull/*, which a clone does not fetch.
            # Also completes a managed clone interrupted before its checkout; local edits still block it.
            known = subprocess.run(["git", "-C", str(self.fork), "cat-file", "-e", FORK_COMMIT + "^{commit}"], check=False, capture_output=True)
            if known.returncode:
                run(["git", "-C", self.fork, "fetch", "--quiet", FORK_URL, FORK_COMMIT])
            run(["git", "-C", self.fork, "checkout", "--quiet", "--detach", FORK_COMMIT])
        if not self.contains_pin():
            raise ValueError(
                f"Le fork doit contenir {FORK_COMMIT} (connexion locale / Conversations #1) : "
                f"git -C {shlex.quote(str(self.fork))} fetch {FORK_URL} {FORK_COMMIT}, puis l'intégrer."
            )

    def contains_pin(self):
        result = subprocess.run(["git", "-C", str(self.fork), "merge-base", "--is-ancestor", FORK_COMMIT, "HEAD"], check=False, capture_output=True)
        return result.returncode == 0

    def sql(self, database, sql):
        return self.compose(
            "exec",
            "-T",
            "db",
            "psql",
            "--no-psqlrc",
            "-v",
            "ON_ERROR_STOP=1",
            "-h",
            "/var/run/postgresql",
            "-U",
            "dev_owner",
            "-d",
            database,
            "-At",
            input=sql,
            text=True,
            capture_output=True,
        ).stdout

    def dev(self, *, rebuild=False):
        self.checkout()
        flags = ["--build", "--renew-anon-volumes"] if rebuild else []
        self.compose("up", *flags, "--detach", "--wait", "--wait-timeout", "240")
        print(f"Conversations : http://localhost:{self.ports['FRONTEND']}\nAPI : http://localhost:{self.ports['API']}/healthz")
        print(f"Comptes locaux : dev@example.invalid / admin@example.invalid. Mot de passe initial : {STATE / 'password'}")
        print("Importer les corpus/config avec make dump. Journaux : make dev-logs. Arrêt : make dev-down.")

    def dump(self):
        """Restore into a candidate database; replace only this stack's local RAG database after validation."""
        source = source_dsn(self.values)
        environment = dump_environment(source)
        self.compose("up", "--detach", "--wait", "db")
        print("Export en lecture seule des corpus/config staging (12 tables autorisées)…", flush=True)
        self.compose(
            "run",
            "--rm",
            "--no-deps",
            "-T",
            "--entrypoint",
            "pg_dump",
            *[arg for key in environment for arg in ("-e", key)],
            "db",
            "--format=custom",
            "--no-owner",
            "--no-privileges",
            "--strict-names",
            *["--table=public." + table for table in TABLES],
            "--file=/state/staging.dump.next",
            extra_env=environment,
        )
        # The container writes as root. Move inside the container and restrict access there.
        self.compose(
            "run",
            "--rm",
            "--no-deps",
            "-T",
            "--entrypoint",
            "sh",
            "db",
            "-ec",
            f"mv /state/staging.dump.next /state/staging.dump && chown {os.getuid()} /state/staging.dump && chmod 600 /state/staging.dump",
        )
        print("Restauration dans une nouvelle base locale ; la base courante reste disponible…", flush=True)
        toc = self.compose("exec", "-T", "db", "pg_restore", "--list", "/state/staging.dump", text=True, capture_output=True).stdout
        write_private(STATE / "dumps/staging.list", restore_list(toc))
        self.sql("postgres", "DROP DATABASE IF EXISTS assistant_rh_import WITH (FORCE); CREATE DATABASE assistant_rh_import;")
        self.sql(
            "assistant_rh_import",
            "".join(f"CREATE EXTENSION IF NOT EXISTS {extension};" for extension in ("vector", "pgcrypto", "pg_trgm", "unaccent", "btree_gin")),
        )
        self.compose(
            "exec",
            "-T",
            "db",
            "pg_restore",
            "-h",
            "/var/run/postgresql",
            "-U",
            "dev_owner",
            "-d",
            "assistant_rh_import",
            "--exit-on-error",
            "--no-owner",
            "--no-privileges",
            "--use-list=/state/staging.list",
            "/state/staging.dump",
        )
        # Same bootstrap as a new volume, keeping the imported rag_config.
        self.compose(
            "exec", "-T", "db", "sh", "/workspace/docker/dev/bootstrap-api.sh", "assistant_rh_import", "imported", text=True, capture_output=True
        )
        counts = self.sql("assistant_rh_import", " UNION ALL ".join(f"SELECT '{table}', count(*) FROM public.{table}" for table in TABLES))
        if self.sql("assistant_rh_import", "SELECT count(*) FROM public.rag_config WHERE id=1").strip() != "1":
            raise ValueError("La copie ne contient pas de configuration RAG active ; base courante conservée.")
        running = self.compose("ps", "--status", "running", "--services", text=True, capture_output=True).stdout.splitlines()
        if "api" in running:
            self.compose("stop", "api")
        try:
            self.swap()
        finally:
            if "api" in running:
                self.compose("up", "--detach", "--wait", "api")
        write_private(STATE / "dump-counts.txt", counts)
        print(counts, end="")
        print("Import terminé. Comptes Conversations conservés ; ancien runtime API dans assistant_rh_previous.")

    def swap(self):
        """Promote the import; the last good copy is dropped only once the new one is in place."""
        self.sql("postgres", "DROP DATABASE IF EXISTS assistant_rh_retired WITH (FORCE);")
        # Committed before the terminate, so clients (IDE, psql) cannot reconnect before the rename.
        self.sql(
            "postgres",
            "ALTER DATABASE assistant_rh WITH ALLOW_CONNECTIONS false; SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname IN ('assistant_rh', 'assistant_rh_previous', 'assistant_rh_import');",
        )
        try:
            self.sql("postgres", SWAP_SQL)
        finally:
            self.sql("postgres", REOPEN_SQL)
        self.sql("postgres", "DROP DATABASE IF EXISTS assistant_rh_retired WITH (FORCE);")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("dev", "dump", "dev-down", "dev-logs", "dev-rebuild"))
    args = parser.parse_args()
    STATE.mkdir(mode=0o700, exist_ok=True)
    with (STATE / "lock").open("w") as lock:
        try:
            if args.command != "dev-logs":
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            stack = Stack({**read_env(ROOT / ".env"), **os.environ})
            if args.command in ("dev", "dump", "dev-rebuild"):
                stack.prepare()
                if args.command == "dump":
                    stack.dump()
                else:
                    stack.dev(rebuild=args.command == "dev-rebuild")
            elif not (STATE / "compose.env").exists():
                print("Aucune stack préparée dans ce worktree : lancer make dev.")
            else:
                stack.compose(*(["down"] if args.command == "dev-down" else ["logs", "--follow", "--tail", "100"]))
        except BlockingIOError:
            parser.exit(1, "Une autre commande make dev/dump est en cours dans ce worktree.\n")
        except (ValueError, OSError) as error:
            parser.exit(1, f"{error}\n")
        except subprocess.CalledProcessError as error:
            if error.stderr:
                # SQL errors contain object names, never the source URI (sent only to pg_dump's environment).
                stderr = error.stderr
                print(stderr if isinstance(stderr, str) else stderr.decode(errors="replace"), file=sys.stderr)
            parser.exit(1, f"Échec de {error.cmd[0]} (code {error.returncode}).\n")


if __name__ == "__main__":
    main()

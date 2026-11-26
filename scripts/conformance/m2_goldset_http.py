"""Collect live goldset answers through the deployed API with individual delegation.

The public report contains transport statistics and served-source recall. The
optional private capture supplies the answers and run references for judging
with the actual generation context under the M1 protocol.
"""

import argparse
import json
import os
import re
import subprocess
import time
from contextlib import nullcontext
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from assistant_rh_api.core.sources import public_source_url

from scripts.conformance.m0b_private import private_directory
from scripts.conformance.m0b_values import digest

MODELS = {f"assistant-rh-{ministry}": ministry for ministry in ("matte", "mso", "mi", "masa")}
SHA256 = re.compile(r"[a-f0-9]{64}")


def load_panel(path):
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    ids = set()
    for row in rows:
        case_id, model, question = row["id"], row["model"], row["question"]
        refs = row["expected_doc_refs"]
        if (
            type(case_id) is not int
            or case_id in ids
            or model not in MODELS
            or not isinstance(question, str)
            or not question.strip()
            or not isinstance(refs, list)
            or not all(isinstance(ref, str) and ref for ref in refs)
        ):
            raise ValueError("Invalid goldset row")
        ids.add(case_id)
    if not rows:
        raise ValueError("Empty goldset panel")
    return rows


def load_deployment(path):
    values = json.loads(path.read_text(encoding="utf-8"))
    required = {"environment", "git_sha", "image_digest", "config_sha256", "corpus_sha256", "model_config_sha256"}
    if set(values) != required or values["environment"] != "staging":
        raise ValueError("Expected a staging deployment manifest with Git, image, config, corpus and model fingerprints")
    git_sha = values["git_sha"]
    if not re.fullmatch(r"[a-f0-9]{40}", git_sha) or not re.fullmatch(r"sha256:[a-f0-9]{64}", values["image_digest"]):
        raise ValueError("Invalid Git revision or image digest")
    if not all(SHA256.fullmatch(values[key]) for key in ("config_sha256", "corpus_sha256", "model_config_sha256")):
        raise ValueError("Invalid deployment fingerprints")
    return values


def validate_endpoint(url):
    endpoint = urlsplit(url)
    loopback = endpoint.hostname in {"127.0.0.1", "::1", "localhost"}
    if (
        endpoint.path.rstrip("/") != "/v1"
        or not endpoint.hostname
        or endpoint.username is not None
        or endpoint.password is not None
        or endpoint.query
        or endpoint.fragment
        or (endpoint.scheme != "https" and not (endpoint.scheme == "http" and loopback))
    ):
        raise ValueError("Use a staging HTTPS /v1 URL or an explicit loopback /v1 tunnel")


def sign(signer, model):
    issued = subprocess.run([str(signer), model], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10, check=False)
    token = issued.stdout.strip()
    if issued.returncode or not re.fullmatch(r"[\w-]+\.[\w-]+\.[\w-]+", token, flags=re.ASCII):
        raise ValueError("Test signer failed to issue a delegation")
    return token


def valid_usage(usage):
    return isinstance(usage, dict) and all(
        type(usage.get(key)) is int and usage[key] >= 0 for key in ("prompt_tokens", "completion_tokens", "total_tokens")
    )


def valid_source(source):
    if not isinstance(source, dict):
        return False
    if not all(isinstance(source.get(key), str) for key in ("doc_ref", "title", "publisher")) or not source["doc_ref"].strip():
        return False
    access = source.get("access")
    return (access == "authenticated" and "url" in source and source["url"] is None) or (
        access == "public" and isinstance(source.get("url"), str) and bool(public_source_url(source["url"]))
    )


def complete(client, row, token):
    started = time.monotonic()

    def elapsed():
        return round((time.monotonic() - started) * 1000)

    transport = {"id": row["id"], "model": row["model"]}
    try:
        response = client.post(
            "chat/completions",
            headers={"Authorization": "Bearer " + token},
            json={"model": row["model"], "stream": False, "messages": [{"role": "user", "content": row["question"]}]},
        )
        transport |= {"status": response.status_code, "elapsed_ms": elapsed()}
        if response.status_code != 200:
            return {**transport, "outcome": "http_error"}, None
        payload = response.json()
        (choice,) = payload["choices"]
        answer = choice["message"]["content"]
        extension = payload["x_assistant_rh"]
        turn_id = extension["turn_id"]
        sources = extension["sources"]
        if (
            payload["object"] != "chat.completion"
            or payload["model"] != row["model"]
            or type(payload["created"]) is not int
            or payload["created"] < 0
            or not valid_usage(payload["usage"])
            or type(choice["index"]) is not int
            or choice["index"] != 0
            or choice["message"]["role"] != "assistant"
            or choice["finish_reason"] != "stop"
            or not isinstance(answer, str)
            or not answer.strip()
            or payload["id"] != f"chatcmpl-{turn_id}"
            or not re.fullmatch(r"[a-f0-9]{32}", turn_id)
            or extension["ministry"] != MODELS[row["model"]]
            or not isinstance(sources, list)
            or not all(valid_source(source) for source in sources)
        ):
            raise ValueError("Invalid completion envelope")
        expected = set(row["expected_doc_refs"])
        observed = {source["doc_ref"] for source in sources}
        quality = {"id": row["id"], "gold_sources": len(expected), "gold_sources_served": len(expected & observed)}
        return {**transport, "outcome": "completed"}, (quality, {"id": row["id"], "turn_id": turn_id, "answer": answer, "sources": sources})
    except httpx.HTTPError as exc:
        return {**transport, "outcome": type(exc).__name__, "elapsed_ms": elapsed()}, None
    except (KeyError, TypeError, ValueError, AttributeError):
        return {**transport, "outcome": "invalid_response", "elapsed_ms": elapsed()}, None


def run(args, capture=None):
    validate_endpoint(args.base_url)
    panel = load_panel(args.panel)
    report = {
        "kind": "live_http_goldset",
        "deployment": load_deployment(args.deployment),
        "panel_sha256": digest(args.panel),
        "transport": [],
        "quality": [],
    }
    with httpx.Client(base_url=args.base_url, timeout=300, follow_redirects=False, trust_env=False) as client:
        for row in panel:
            transport, result = complete(client, row, sign(args.signer.resolve(), row["model"]))
            report["transport"].append(transport)
            if result is not None:
                quality, private = result
                report["quality"].append(quality)
                if capture:
                    capture.write(json.dumps(private, ensure_ascii=False) + "\n")
                    capture.flush()
    report["quality_complete"] = len(report["quality"]) == len(panel)
    return report


def open_private_capture(path):
    if path is None:
        return nullcontext()
    directory = private_directory(path.parent)
    fd = os.open(directory / path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    return os.fdopen(fd, "w", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--panel", type=Path, required=True, help="JSONL with id, model, question and expected_doc_refs")
    parser.add_argument("--deployment", type=Path, required=True, help="Verified staging deployment manifest")
    parser.add_argument("--signer", type=Path, required=True, help="Executable issuing an isolated staging test assertion for each requested model")
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--private-capture", type=Path)
    args = parser.parse_args()
    # Reserve output paths before any request, so reruns cannot overwrite previous evidence.
    with args.report.open("x", encoding="utf-8") as output, open_private_capture(args.private_capture) as capture:
        report = run(args, capture)
        json.dump(report, output, ensure_ascii=False, indent=2)
        output.write("\n")
    return 0 if report["quality_complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

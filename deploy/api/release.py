#!/usr/bin/env python3
"""Trusted tag delivery coordinator. Cluster access is only needed by confirm."""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPOSITORY = "DGAFP/assistant-rh"
STAGING_REGISTRY = "rg.fr-par.scw.cloud/assistant-rh-staging"
TAG = re.compile(r"v((?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*))(?:-rc\.([1-9][0-9]*))?")
DIGEST = re.compile(r"sha256:[a-f0-9]{64}")
SHA = re.compile(r"[a-f0-9]{40}")
PLAN_ASSET = "reservation.json"
MANIFEST_ASSET = "delivery.json"
RECEIPT_ASSET = "receipt.json"


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlsplit(newurl).scheme != "https":
            raise RuntimeError("Refusing an insecure GitHub redirect")
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if urllib.parse.urlsplit(req.full_url).netloc != urllib.parse.urlsplit(newurl).netloc:
            redirected.remove_header("Authorization")
        return redirected


def api(path, data=None, method=None, binary=None):
    host = "https://uploads.github.com" if binary is not None else "https://api.github.com"
    url = host + (path if path.startswith("/user") else f"/repos/{REPOSITORY}/" + path)
    headers = {
        "Authorization": "Bearer " + os.environ["GH_TOKEN"],
        "Accept": "application/vnd.github+json",
    }
    body = json.dumps(data).encode() if data is not None else binary
    if body is not None:
        headers["Content-Type"] = "application/octet-stream" if binary is not None else "application/json"
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.build_opener(SafeRedirect()).open(request, timeout=60) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise RuntimeError(f"GitHub request failed: HTTP {error.code} ({path})") from error


def command(*args, cwd=None):
    result = subprocess.run(args, cwd=cwd, text=True, capture_output=True, check=False)
    if result.returncode:
        raise RuntimeError(f"{args[0]} failed; publication was not confirmed")
    return result.stdout.strip()


def classify(tag):
    match = TAG.fullmatch(tag)
    if not match:
        raise RuntimeError("Expected vX.Y.Z-rc.N or vX.Y.Z")
    return match.group(1), "staging" if match.group(2) else "production"


def output(**values):
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as stream:
        for key, value in values.items():
            stream.write(f"{key}={value}\n")


def delivery_assets(key="release"):
    if key != "release" and not re.fullmatch(r"[1-9][0-9]*", key):
        raise RuntimeError("Invalid redelivery identifier")
    prefix = "" if key == "release" else "redelivery-" + key + "-"
    return tuple(prefix + name for name in (PLAN_ASSET, MANIFEST_ASSET, RECEIPT_ASSET))


def resolve_tag(tag):
    ref = api("git/ref/tags/" + urllib.parse.quote(tag, safe=""))
    if not ref:
        raise RuntimeError("Deployment tag does not exist")
    obj = ref["object"]
    for _ in range(5):
        if obj["type"] == "commit" and SHA.fullmatch(obj["sha"]):
            return obj["sha"]
        if obj["type"] != "tag":
            break
        obj = api("git/tags/" + obj["sha"])["object"]
    raise RuntimeError("Tag does not resolve to a commit")


def plan():
    if os.environ.get("GITHUB_REPOSITORY") != REPOSITORY or os.environ.get("GITHUB_REF") != "refs/heads/main":
        raise RuntimeError("Privileged delivery must run from the protected default branch")
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    action = "publish"
    key = "release"
    if "workflow_run" in event:
        run = event["workflow_run"]
        if run["event"] != "push" or run["conclusion"] != "success" or run["head_repository"]["full_name"] != REPOSITORY:
            output(delivery="false")
            return
        tag = run["head_branch"] or ""
        if not TAG.fullmatch(tag):
            output(delivery="false")
            return
        expected_sha = run["head_sha"]
    else:
        inputs = event["inputs"]
        tag, action = inputs["release_tag"], inputs["action"]
        if action not in {"resume", "redeploy"}:
            raise RuntimeError("Unsupported delivery operation")
        expected_sha = resolve_tag(tag)
        key = os.environ["GITHUB_RUN_ID"] if action == "redeploy" else inputs.get("delivery_id") or "release"
        delivery_assets(key)
    _, target = classify(tag)
    sha = resolve_tag(tag)
    if sha != expected_sha:
        raise RuntimeError("Tag changed since the triggering validation")
    command("git", "merge-base", "--is-ancestor", sha, "origin/main")
    if target == "production" and os.environ.get("PRODUCTION_ENABLED") != "true":
        raise RuntimeError("Production target is not enabled; no publication was attempted")
    output(
        delivery="true",
        tag=tag,
        source_sha=sha,
        target=target,
        action=action,
        environment="kapsule-" + target,
        delivery_key=key,
    )


def release_for(tag, create=False):
    result = api("releases/tags/" + urllib.parse.quote(tag, safe=""))
    if result is None and create:
        _, target = classify(tag)
        result = api(
            "releases",
            {
                "tag_name": tag,
                "name": tag,
                "prerelease": target == "staging",
                "generate_release_notes": False,
            },
        )
    if result is None:
        raise RuntimeError("No release record exists for this tag")
    return result


def asset(release, name):
    for page in range(1, 101):
        assets = api(f"releases/{release['id']}/assets?per_page=100&page={page}")
        found = next((a for a in assets if a["name"] == name), None)
        if found or len(assets) < 100:
            return found
    raise RuntimeError("Too many release assets to inspect")


def download(release, name):
    found = asset(release, name)
    if found is None:
        return None
    # Published release files contain no secrets and can be downloaded without credentials.
    with urllib.request.urlopen(found["browser_download_url"], timeout=60) as response:
        return response.read()


def upload(release, name, content):
    existing = download(release, name)
    if existing is not None:
        if existing != content:
            raise RuntimeError("Refusing to replace an existing release artifact: " + name)
        return
    api(f"releases/{release['id']}/assets?name=" + urllib.parse.quote(name, safe=""), binary=content)


def serialized(value):
    return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()


def read_manifest(release, name=MANIFEST_ASSET):
    raw = download(release, name)
    if raw is None:
        raise RuntimeError("Release manifest is missing")
    result = json.loads(raw)
    if result.get("schema") != 1 or not SHA.fullmatch(result.get("source_sha", "")):
        raise RuntimeError("Invalid release manifest")
    if result["tag"] != release["tag_name"]:
        raise RuntimeError("Manifest belongs to another GitHub release")
    _, target = classify(result["tag"])
    if target != result["target"]:
        raise RuntimeError("Manifest environment differs from its tag")
    for name in ("api",):
        if not DIGEST.fullmatch(result[name]["digest"]):
            raise RuntimeError("Invalid image digest")
    return result


def receipt(release, manifest, name=RECEIPT_ASSET):
    if asset(release, name.replace("receipt.json", "revocation.json")):
        return None
    found = asset(release, name)
    validators = os.environ.get("RELEASE_VALIDATORS", "").split(",")
    if not found or found["uploader"]["login"] not in validators or found["uploader"]["type"] != "User":
        return None
    proof = json.loads(download(release, name))
    expected = hashlib.sha256(serialized(manifest)).hexdigest()
    if (
        proof.get("manifest_sha256") != expected
        or proof.get("validated") is not True
        or proof.get("source_sha") != manifest["source_sha"]
        or proof.get("target") != manifest["target"]
    ):
        raise RuntimeError("Recipe proof does not identify this exact delivery")
    return proof


def find_candidate(tag, sha):
    version, _ = classify(tag)
    for page in range(1, 11):
        releases = api(f"releases?per_page=100&page={page}")
        for release in releases:
            match = TAG.fullmatch(release["tag_name"])
            if not match or match.group(1) != version or not match.group(2):
                continue
            raw = download(release, MANIFEST_ASSET)
            if raw is None:
                continue
            manifest = read_manifest(release)
            if manifest["source_sha"] == sha and receipt(release, manifest):
                return manifest
        if len(releases) < 100:
            break
    raise RuntimeError("No validated RC of this version exists on the stable tag's exact commit")


def next_chart_version(tags):
    versions = [tuple(map(int, t.split("."))) for t in tags if re.fullmatch(r"0\.[0-9]+\.[0-9]+", t)]
    return f"0.{max((v[1] for v in versions), default=0) + 1}.0"


def registry_tags(registry):
    result = subprocess.run(
        [
            "oras",
            "repo",
            "tags",
            "--registry-config",
            os.environ["TARGET_REGISTRY_CONFIG"],
            registry + "/assistant-rh-api",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode and "404" not in result.stderr:
        raise RuntimeError("Could not enumerate existing chart versions")
    return result.stdout.splitlines()


def existing_digest(reference):
    result = subprocess.run(
        ["oras", "resolve", "--registry-config", os.environ["TARGET_REGISTRY_CONFIG"], reference],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        if "404" in result.stderr:
            return None
        raise RuntimeError("Registry lookup failed; artifact existence is unknown")
    if not DIGEST.fullmatch(result.stdout.strip()):
        raise RuntimeError("Registry returned an invalid digest")
    return result.stdout.strip()


def active_deployment(target, tag, key="release", recovery=False):
    deployments = api("deployments?per_page=100&environment=kapsule-" + target)
    for deployment in deployments:
        payload = deployment.get("payload") or {}
        if payload.get("coordinator") != "tagged-release-v1":
            continue
        statuses = api(f"deployments/{deployment['id']}/statuses?per_page=1")
        if not statuses or statuses[0]["state"] in {"queued", "pending", "in_progress"}:
            if (payload.get("tag") != tag or payload.get("delivery_key", "release") != key) and not recovery:
                raise RuntimeError("Another delivery is still active; reconcile it before publishing")
            return deployment
    return None


def publish():
    tag, sha = os.environ["RELEASE_TAG"], os.environ["SOURCE_SHA"]
    _, target = classify(tag)
    if resolve_tag(tag) != sha:
        raise RuntimeError("Deployment tag moved")
    registry = os.environ["RELEASE_REGISTRY"]
    if not re.fullmatch(r"rg\.fr-par\.scw\.cloud/[a-z0-9-]+", registry):
        raise RuntimeError("Invalid registry destination")
    if target == "production" and registry == STAGING_REGISTRY:
        raise RuntimeError("Production cannot publish into the staging registry")
    if target == "staging" and registry != STAGING_REGISTRY:
        raise RuntimeError("Unexpected staging registry")
    server = os.environ["RELEASE_CLUSTER_SERVER"]
    if not re.fullmatch(r"https://[a-z0-9.-]+:6443", server):
        raise RuntimeError("Invalid cluster API server")
    if command("git", "rev-parse", "HEAD", cwd="source") != sha:
        raise RuntimeError("Application checkout differs from the resolved tag")
    if not os.environ.get("RELEASE_VALIDATORS"):
        raise RuntimeError("Recipe validators must be configured before publishing")
    key = os.environ.get("DELIVERY_KEY", "release")
    plan_name, manifest_name, proof_name = delivery_assets(key)
    recovery = key != "release"
    values = Path("source/deploy/api/" + target + ".yaml")
    if not values.is_file():
        raise RuntimeError("Reviewed environment values are missing: " + str(values))
    record = release_for(tag, create=not recovery)
    reserved = download(record, plan_name)
    reservation = json.loads(reserved) if reserved is not None else None
    if reservation and reservation.get("candidate_manifest"):
        original = reservation["candidate_manifest"]
        candidate_record = release_for(original["tag"])
        candidate = read_manifest(candidate_record)
        if candidate != original or not receipt(candidate_record, candidate):
            raise RuntimeError("The reserved candidate changed or its validation was revoked")
    elif recovery:
        candidate = read_manifest(record)
        if not receipt(record, candidate):
            raise RuntimeError("Only a previously validated delivery can be redeployed")
    else:
        candidate = find_candidate(tag, sha) if target == "production" else None
    saved = download(record, manifest_name)
    if saved is not None:
        manifest = read_manifest(record, manifest_name)
        if manifest["source_sha"] != sha or manifest["registry"] != registry or manifest["cluster_server"] != server:
            raise RuntimeError("Existing release points at another source commit")
        if receipt(record, manifest, proof_name):
            complete(manifest)
            sys.stdout.write("This delivery has already been validated.\n")
            return
        wait_for_receipt(record, manifest, proof_name)
        return
    active = active_deployment(target, tag, key, recovery=recovery)
    if reservation is None:
        reservation = {
            "schema": 1,
            "tag": tag,
            "source_sha": sha,
            "target": target,
            "registry": registry,
            "cluster_server": server,
            "chart_version": next_chart_version(registry_tags(registry)),
            "candidate_manifest": candidate,
        }
        upload(record, plan_name, serialized(reservation))
    else:
        if any(
            reservation[k] != v
            for k, v in {
                "tag": tag,
                "source_sha": sha,
                "target": target,
                "registry": registry,
                "cluster_server": server,
            }.items()
        ):
            raise RuntimeError("Delivery reservation changed")
    own_deployment = active and active["payload"].get("tag") == tag and active["payload"].get("delivery_key", "release") == key
    previous = (active["payload"].get("recovery_of") if own_deployment else active["id"]) if active and recovery else None
    if not own_deployment:
        deployment = api(
            "deployments",
            {
                "ref": tag,
                "auto_merge": False,
                "required_contexts": [],
                "environment": "kapsule-" + target,
                "production_environment": target == "production",
                "payload": {
                    "coordinator": "tagged-release-v1",
                    "tag": tag,
                    "delivery_key": key,
                    "recovery_of": previous,
                },
            },
        )
        active = deployment
        api(f"deployments/{active['id']}/statuses", {"state": "in_progress", "auto_inactive": False})
    manifest = dict(
        reservation,
        deployment_id=active["id"],
        definition_sha=os.environ["GITHUB_SHA"],
        supersedes_deployment_id=previous,
        delivery_key=key,
    )
    manifest["chart_source_tree"] = command("git", "rev-parse", "HEAD:deploy/api", cwd="source")
    manifest["values_sha256"] = hashlib.sha256(values.read_bytes()).hexdigest()
    for name in ("api",):
        image = registry + "/assistant-rh-api"
        image_tag = "release-" + tag
        if candidate:
            source = candidate[name]
            expected_registry = registry if recovery else STAGING_REGISTRY
            if source["repository"] != expected_registry + "/assistant-rh-api":
                raise RuntimeError("Unexpected candidate image repository")
            source_config = os.environ["TARGET_REGISTRY_CONFIG"] if recovery else os.environ["SOURCE_REGISTRY_CONFIG"]
            command(
                "oras",
                "cp",
                "--from-registry-config",
                source_config,
                "--to-registry-config",
                os.environ["TARGET_REGISTRY_CONFIG"],
                source["repository"] + "@" + source["digest"],
                image + ":" + image_tag,
            )
            digest = command(
                "oras",
                "resolve",
                "--registry-config",
                os.environ["TARGET_REGISTRY_CONFIG"],
                image + ":" + image_tag,
            )
            if digest != source["digest"]:
                raise RuntimeError("Image promotion changed its digest")
        else:
            existing = subprocess.run(
                [
                    "oras",
                    "resolve",
                    "--registry-config",
                    os.environ["TARGET_REGISTRY_CONFIG"],
                    image + ":" + image_tag,
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            if existing.returncode and "404" not in existing.stderr:
                raise RuntimeError("Cannot establish whether a release image already exists")
            if existing.returncode:
                dockerfile = "docker/api/Dockerfile"
                command(
                    "docker",
                    "buildx",
                    "build",
                    "--platform",
                    "linux/amd64",
                    "--label",
                    "org.opencontainers.image.revision=" + sha,
                    "-f",
                    dockerfile,
                    "--load",
                    "-t",
                    image + ":" + image_tag,
                    ".",
                    cwd="source",
                )
            else:
                command("docker", "pull", image + "@" + existing.stdout.strip())
            reference = image + ":" + image_tag if existing.returncode else image + "@" + existing.stdout.strip()
            label = command(
                "docker",
                "image",
                "inspect",
                reference,
                "--format",
                '{{ index .Config.Labels "org.opencontainers.image.revision" }}',
            )
            if label != sha:
                raise RuntimeError("Existing release image was built from another source commit")
            command(
                "trivy",
                "image",
                "--scanners",
                "vuln,secret",
                "--severity",
                "HIGH,CRITICAL",
                "--exit-code",
                "1",
                reference,
            )
            if existing.returncode:
                command("docker", "push", reference)
            digest = command(
                "oras",
                "resolve",
                "--registry-config",
                os.environ["TARGET_REGISTRY_CONFIG"],
                image + ":" + image_tag,
            )
        if not DIGEST.fullmatch(digest):
            raise RuntimeError("Invalid published image digest")
        if candidate:
            command(
                "trivy",
                "image",
                "--scanners",
                "vuln,secret",
                "--severity",
                "HIGH,CRITICAL",
                "--exit-code",
                "1",
                image + "@" + digest,
            )
        manifest[name] = {"repository": image, "digest": digest}
    manifest["candidate"] = candidate["tag"] if candidate else None
    with tempfile.TemporaryDirectory() as temporary:
        package = "assistant-rh-api-" + manifest["chart_version"] + ".tgz"
        archive = download(record, package)
        if archive is None:
            env = os.environ.copy()
            env.update(
                SOURCE_ROOT="source",
                RELEASE_ENVIRONMENT=target,
                CHART_VERSION=manifest["chart_version"],
                BUNDLE_OUTPUT_DIR=temporary,
                IMAGE_DIGEST=manifest["api"]["digest"],
            )
            subprocess.run([sys.executable, "deploy/api/package-staging.py"], env=env, check=True)
            archive = (Path(temporary) / package).read_bytes()
            upload(record, package, archive)
        path = Path(temporary) / package
        path.write_bytes(archive)
        reference = registry + "/assistant-rh-api:" + manifest["chart_version"]
        previous_digest = existing_digest(reference)
        if previous_digest:
            stored = json.loads(
                command(
                    "oras",
                    "manifest",
                    "fetch",
                    "--registry-config",
                    os.environ["TARGET_REGISTRY_CONFIG"],
                    reference,
                )
            )
            expected_layer = "sha256:" + hashlib.sha256(archive).hexdigest()
            if not any(layer["digest"] == expected_layer for layer in stored["layers"]):
                raise RuntimeError("Refusing to replace an existing chart with different content")
        else:
            command(
                "helm",
                "push",
                "--registry-config",
                os.environ["TARGET_REGISTRY_CONFIG"],
                str(path),
                "oci://" + registry,
            )
    manifest["chart_digest"] = command(
        "oras",
        "resolve",
        "--registry-config",
        os.environ["TARGET_REGISTRY_CONFIG"],
        registry + "/assistant-rh-api:" + manifest["chart_version"],
    )
    if not DIGEST.fullmatch(manifest["chart_digest"]):
        raise RuntimeError("Invalid chart digest")
    upload(record, manifest_name, serialized(manifest))
    sys.stdout.write(f"Published {tag}; deployment {active['id']} awaits observed Flux state and recipe proof.\n")
    wait_for_receipt(record, manifest, proof_name)


def wait_for_receipt(record, manifest, proof_name=RECEIPT_ASSET):
    deadline = time.monotonic() + 1800
    while time.monotonic() < deadline:
        if receipt(record, manifest, proof_name):
            complete(manifest)
            sys.stdout.write("Observed deployment and authenticated recipe validated.\n")
            return
        time.sleep(20)
    raise RuntimeError("Recipe proof missing; delivery remains active until operator reconciliation")


def complete(manifest):
    api(
        f"deployments/{manifest['deployment_id']}/statuses",
        {"state": "success", "auto_inactive": False},
    )
    previous = manifest.get("supersedes_deployment_id")
    seen = {manifest["deployment_id"]}
    while previous:
        if previous in seen:
            raise RuntimeError("Cyclic deployment recovery chain")
        seen.add(previous)
        deployment = api(f"deployments/{previous}")
        payload = deployment.get("payload") or {}
        if payload.get("coordinator") != "tagged-release-v1":
            raise RuntimeError("Unexpected predecessor in deployment recovery chain")
        api(
            f"deployments/{previous}/statuses",
            {"state": "inactive", "auto_inactive": False},
        )
        previous = payload.get("recovery_of")


def ready(resource):
    return (
        resource["status"].get("observedGeneration") == resource["metadata"]["generation"]
        and any(c["type"] == "Ready" and c["status"] == "True" for c in resource["status"]["conditions"])
        and not any(c["type"] == "Reconciling" and c["status"] == "True" for c in resource["status"]["conditions"])
    )


def confirm(args):
    record = release_for(args.tag)
    _, manifest_name, proof_name = delivery_assets(args.delivery_id)
    manifest = read_manifest(record, manifest_name)
    recipe_bytes = Path(args.recipe).read_bytes()
    recipe = json.loads(recipe_bytes)
    if (
        any(recipe.get(k) != manifest[k] for k in ("tag", "source_sha", "target"))
        or not all(recipe.get("checks", {}).get(k) is True for k in ("authentication", "sse", "isolation"))
        or not str(recipe.get("report_url", "")).startswith("https://")
    ):
        raise RuntimeError("An authenticated recipe report bound to this delivery is required")
    validator = api("/user")["login"]
    if validator not in os.environ.get("RELEASE_VALIDATORS", "").split(","):
        raise RuntimeError("This operator is not a configured recipe validator")
    kubectl = [args.kubectl, "--kubeconfig", args.kubeconfig, "--request-timeout=20s"]
    server = command(*kubectl, "config", "view", "--minify", "-o", "jsonpath={.clusters[0].cluster.server}")
    if server != manifest["cluster_server"]:
        raise RuntimeError("Kubeconfig does not target this delivery's cluster")

    def read(kind, name, namespace="assistant-rh-api"):
        return json.loads(command(*kubectl, "-n", namespace, "get", kind, name, "-o", "json"))

    source = read("ocirepository", "assistant-rh-api-delivery")
    helm = read("helmrelease", "assistant-rh-api")
    expected_revision = manifest["chart_version"] + "@" + manifest["chart_digest"]
    if (
        not ready(source)
        or not ready(helm)
        or source["spec"]["url"] != "oci://" + manifest["registry"] + "/assistant-rh-api"
        or source["status"]["artifact"]["revision"] != expected_revision
        or helm["status"].get("lastAttemptedRevisionDigest") != manifest["chart_digest"]
        or helm["spec"].get("test", {}).get("enable") is not True
        or helm["spec"].get("test", {}).get("ignoreFailures", False)
    ):
        raise RuntimeError("Flux has not reconciled and tested the expected chart")
    history = helm["status"].get("history", [{}])[0]
    hooks = history.get("testHooks", {})
    if (
        history.get("appVersion") != manifest["source_sha"]
        or not history.get("chartVersion", "").startswith(manifest["chart_version"] + "+")
        or not hooks
        or not all(hook.get("phase") == "Succeeded" for hook in hooks.values())
    ):
        raise RuntimeError("The expected Helm revision has no successful test proof")
    observed = {}
    for name in ("api",):
        deployment = read("deployment", "assistant-rh-" + name)
        spec, status = deployment["spec"], deployment["status"]
        desired = spec.get("replicas", 1)
        if (
            status.get("observedGeneration") != deployment["metadata"]["generation"]
            or status.get("updatedReplicas", 0) != desired
            or status.get("readyReplicas", 0) != desired
            or status.get("replicas", 0) != desired
        ):
            raise RuntimeError("Workload rollout is incomplete")
        images = [c["image"] for c in spec["template"]["spec"]["containers"]]
        if not any(image.split("@")[-1] == manifest[name]["digest"] for image in images):
            raise RuntimeError("Observed image differs from the recipe candidate")
        observed[name] = images
    proof = {
        "schema": 1,
        "validated": True,
        "tag": manifest["tag"],
        "target": manifest["target"],
        "source_sha": manifest["source_sha"],
        "manifest_sha256": hashlib.sha256(serialized(manifest)).hexdigest(),
        "observed_images": observed,
        "flux_revision": expected_revision,
        "validator": validator,
        "recipe_sha256": hashlib.sha256(recipe_bytes).hexdigest(),
        "report_url": recipe["report_url"],
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "helm_values_sha256": hashlib.sha256(serialized(helm["spec"].get("values", {}))).hexdigest(),
    }
    proof["conversations_images"] = {}
    for name in ("backend", "frontend"):
        dependency = read("deployment", "conversations-" + name, "conversations")
        proof["conversations_images"][name] = [c["image"] for c in dependency["spec"]["template"]["spec"]["containers"]]
    upload(record, proof_name, serialized(proof))
    complete(manifest)
    sys.stdout.write("Flux, rollout and authenticated recipe proof recorded.\n")


def main():
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("plan")
    commands.add_parser("publish")
    confirm_parser = commands.add_parser("confirm")
    for name in ("tag", "recipe", "kubeconfig"):
        confirm_parser.add_argument("--" + name, required=True)
    confirm_parser.add_argument("--kubectl", default="kubectl")
    confirm_parser.add_argument("--delivery-id", default="release")
    args = parser.parse_args()
    {"plan": plan, "publish": publish, "confirm": lambda: confirm(args)}[args.command]()


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, KeyError, ValueError) as error:
        sys.stderr.write(str(error) + "\n")
        sys.exit(1)

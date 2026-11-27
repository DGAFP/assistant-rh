#!/usr/bin/env python3
"""Seal reviewed API values and an immutable image digest into an OCI Helm chart."""

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from release import DIGEST, REPOSITORY, SHA, STAGING_REGISTRY, classify


def main():
    if os.environ.get("GITHUB_REPOSITORY") != REPOSITORY:
        raise RuntimeError("Unexpected delivery repository")
    tag = os.environ["RELEASE_TAG"]
    _, target = classify(tag)
    if target != os.environ["RELEASE_ENVIRONMENT"]:
        raise RuntimeError("Tag does not select this environment")
    sha = os.environ["SOURCE_SHA"]
    if not SHA.fullmatch(sha):
        raise RuntimeError("Invalid source commit")
    version = os.environ["CHART_VERSION"]
    if not re.fullmatch(r"0\.[1-9][0-9]*\.0", version):
        raise RuntimeError("Invalid chart sequence")
    registry = os.environ["RELEASE_REGISTRY"]
    if not re.fullmatch(r"rg\.fr-par\.scw\.cloud/[a-z0-9-]+", registry):
        raise RuntimeError("Invalid registry")
    if (target == "staging") != (registry == STAGING_REGISTRY):
        raise RuntimeError("Registry does not match the target environment")
    digest = os.environ["IMAGE_DIGEST"]
    if not DIGEST.fullmatch(digest):
        raise RuntimeError("Invalid image digest")
    source = Path(os.environ.get("SOURCE_ROOT", ".")).resolve()
    overrides = source / "deploy/api" / (target + ".yaml")
    if not overrides.is_file():
        raise RuntimeError("Reviewed environment values are missing")
    destination = Path(os.environ["BUNDLE_OUTPUT_DIR"]).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as temporary:
        chart = Path(temporary) / "assistant-rh-api"
        shutil.copytree(source / "deploy/api", chart)
        template = chart / "templates/bundle-values.yaml"
        template.write_text("{{ .Values | toJson }}\n")
        args = [
            "helm",
            "template",
            "assistant-rh-api",
            str(chart),
            "--namespace",
            "assistant-rh-api",
            "--show-only",
            "templates/bundle-values.yaml",
            "-f",
            str(overrides),
            "--set-string",
            "image.repository=" + registry + "/assistant-rh-api",
            "--set-string",
            "image.digest=" + digest,
        ]
        rendered = subprocess.check_output(args, text=True)
        values = json.loads("\n".join(line for line in rendered.splitlines() if line.strip() and not line.startswith(("#", "---"))))
        if values["environment"] != target:
            raise RuntimeError("Application environment differs from the delivery")
        template.unlink()
        (chart / "values.yaml").write_text(json.dumps(values, indent=2) + "\n")
        shutil.copyfile(Path(__file__).with_name("staging-smoke.yaml"), chart / "templates/staging-smoke.yaml")
        subprocess.run(["helm", "lint", str(chart)], check=True)
        subprocess.run(["helm", "package", str(chart), "--destination", str(destination), "--version", version, "--app-version", sha], check=True)
    print(f"Prepared {target} chart assistant-rh-api {version} for {tag} ({sha})")


if __name__ == "__main__":
    main()

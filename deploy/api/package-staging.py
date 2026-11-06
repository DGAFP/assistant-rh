#!/usr/bin/env python3
"""Build a sealed staging Helm archive locally. This script never publishes it."""

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

APPLICATION = "api"
REPOSITORY = "DGAFP/assistant-rh"
CHART = "deploy/api"
RELEASE = "assistant-rh-api"


def main():
    if os.environ.get("GITHUB_REPOSITORY") != REPOSITORY or os.environ.get("GITHUB_REF_NAME") != "staging":
        raise RuntimeError("Only the intended repository staging branch can produce this bundle")
    sha = os.environ["GITHUB_SHA"]
    if not re.fullmatch(r"[a-f0-9]{40}", sha):
        raise RuntimeError("Invalid commit")
    numbers = [os.environ["GITHUB_RUN_NUMBER"], os.environ["GITHUB_RUN_ATTEMPT"]]
    if not all(re.fullmatch(r"[1-9][0-9]*", number) for number in numbers):
        raise RuntimeError("Invalid release sequence")
    version = "0." + ".".join(numbers)
    keys = ["IMAGE_DIGEST"] if APPLICATION == "api" else ["BACKEND_DIGEST", "FRONTEND_DIGEST"]
    for key in keys:
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", os.environ[key]):
            raise RuntimeError("Invalid image digest")
    destination = Path(os.environ["BUNDLE_OUTPUT_DIR"]).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as temporary:
        chart = Path(temporary) / RELEASE
        shutil.copytree(CHART, chart)
        values_template = chart / "templates" / "bundle-values.yaml"
        values_template.write_text("{{ .Values | toJson }}\n")
        args = ["helm", "template", RELEASE, str(chart), "--namespace", RELEASE, "--show-only", "templates/bundle-values.yaml"]
        if APPLICATION == "api":
            args += ["--set-string", "image.digest=" + os.environ["IMAGE_DIGEST"]]
        else:
            args += [
                "-f",
                "deploy/kapsule/staging.yaml",
                "--set-string",
                "image.tag=sha-" + sha + "@" + os.environ["BACKEND_DIGEST"],
                "--set-string",
                "frontend.image.tag=sha-" + sha + "@" + os.environ["FRONTEND_DIGEST"],
            ]
        rendered = subprocess.check_output(args, text=True)
        values = json.loads("\n".join(line for line in rendered.splitlines() if line.strip() and not line.startswith(("#", "---"))))
        values_template.unlink()
        # JSON is valid YAML; Helm does the values merge without another dependency.
        (chart / "values.yaml").write_text(json.dumps(values, indent=2) + "\n")
        shutil.copyfile(Path(__file__).with_name("staging-smoke.yaml"), chart / "templates" / "staging-smoke.yaml")
        subprocess.run(["helm", "lint", str(chart)], check=True)
        subprocess.run(["helm", "package", str(chart), "--destination", str(destination), "--version", version, "--app-version", sha], check=True)
    print("Prepared staging chart", RELEASE, version, "for", sha)
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as output:
            output.write("chart_version=" + version + "\n")
            output.write("chart_path=" + str(destination / (RELEASE + "-" + version + ".tgz")) + "\n")


if __name__ == "__main__":
    main()

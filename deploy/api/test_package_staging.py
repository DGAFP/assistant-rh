"""Inspect real sealed API charts and target isolation without registry access."""

import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).with_name("package-staging.py").resolve()
ROOT = SCRIPT.parents[2]
SHA = "a" * 40
DIGEST = "sha256:" + "b" * 64


class PackageTest(unittest.TestCase):
    def environment(self, directory):
        return dict(
            os.environ,
            GITHUB_REPOSITORY="DGAFP/assistant-rh",
            RELEASE_TAG="v0.15.0-rc.1",
            SOURCE_SHA=SHA,
            RELEASE_ENVIRONMENT="staging",
            CHART_VERSION="0.85.0",
            RELEASE_REGISTRY="rg.fr-par.scw.cloud/assistant-rh-staging",
            BUNDLE_OUTPUT_DIR=directory,
            IMAGE_DIGEST=DIGEST,
        )

    def run_package(self, env):
        return subprocess.run(["python3", str(SCRIPT)], env=env, capture_output=True, text=True)

    def test_real_bundle_preserves_runtime_limits_and_pins_the_image(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.run_package(self.environment(directory))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            archive = Path(directory) / "assistant-rh-api-0.85.0.tgz"
            with tarfile.open(archive) as package:
                values = json.load(package.extractfile("assistant-rh-api/values.yaml"))
                hook = package.extractfile("assistant-rh-api/templates/staging-smoke.yaml").read().decode()
                self.assertIn("helm.sh/hook: test", hook)
                self.assertNotIn("assistant-rh-api/templates/bundle-values.yaml", package.getnames())
            self.assertEqual(values["image"]["digest"], DIGEST)
            self.assertEqual(values["image"]["repository"], self.environment(directory)["RELEASE_REGISTRY"] + "/assistant-rh-api")
            self.assertEqual(values["streamWorkers"], 50)
            self.assertEqual(values["sqlSearchAdmissionTimeoutSeconds"], 30)
            rendered = subprocess.check_output(["helm", "template", "assistant-rh-api", str(archive)], text=True)
            self.assertIn(DIGEST, rendered)
            self.assertIn('name: APP_ENV, value: "staging"', rendered)
            self.assertIn('name: API_STREAM_WORKERS, value: "50"', rendered)
            self.assertIn('name: API_DB_SEARCH_TIMEOUT_SECONDS, value: "30"', rendered)
            self.assertNotIn("kind: Ingress\n", rendered)

    def test_invalid_identity_target_or_artifacts_cannot_produce_a_chart(self):
        for key, value in [
            ("GITHUB_REPOSITORY", "other/project"),
            ("SOURCE_SHA", "invalid"),
            ("RELEASE_TAG", "v0.15.0-rc.0"),
            ("CHART_VERSION", "0.0.0"),
            ("RELEASE_ENVIRONMENT", "production"),
            ("IMAGE_DIGEST", "latest"),
            ("RELEASE_REGISTRY", "rg.fr-par.scw.cloud/other"),
        ]:
            with self.subTest(key=key), tempfile.TemporaryDirectory() as directory:
                env = self.environment(directory) | {key: value}
                self.assertNotEqual(self.run_package(env).returncode, 0)
                self.assertEqual(list(Path(directory).glob("*.tgz")), [])

    def test_production_requires_reviewed_values_and_a_separate_registry(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source"
            shutil.copytree(ROOT / "deploy/api", source / "deploy/api")
            output = Path(directory) / "bundles"
            env = self.environment(str(output)) | {
                "SOURCE_ROOT": str(source),
                "RELEASE_TAG": "v0.15.0",
                "RELEASE_ENVIRONMENT": "production",
                "RELEASE_REGISTRY": "rg.fr-par.scw.cloud/assistant-rh-production",
            }
            self.assertNotEqual(self.run_package(env).returncode, 0)
            values = source / "deploy/api/production.yaml"
            values.write_text("environment: production\n")
            result = self.run_package(env)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            archive = output / "assistant-rh-api-0.85.0.tgz"
            rendered = subprocess.check_output(["helm", "template", "assistant-rh-api", str(archive)], text=True)
            self.assertIn('name: APP_ENV, value: "production"', rendered)
            self.assertIn(env["RELEASE_REGISTRY"] + "/assistant-rh-api@" + DIGEST, rendered)
            values.write_text("environment: staging\n")
            self.assertNotEqual(self.run_package(env).returncode, 0)


if __name__ == "__main__":
    unittest.main()

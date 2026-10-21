"""Build and inspect a real Helm bundle; no registry or cluster access."""

import json
import os
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path

REPOSITORY = "DGAFP/assistant-rh"
RELEASE = "assistant-rh-api"
SCRIPT = Path(__file__).with_name("package-staging.py").resolve()
SHA = "a" * 40
DIGEST = "sha256:" + "b" * 64


class PackageTest(unittest.TestCase):
    def environment(self, directory):
        return dict(
            os.environ,
            GITHUB_REPOSITORY=REPOSITORY,
            GITHUB_REF_NAME="staging",
            GITHUB_SHA=SHA,
            GITHUB_RUN_NUMBER="123",
            GITHUB_RUN_ATTEMPT="2",
            BUNDLE_OUTPUT_DIR=directory,
            GITHUB_OUTPUT=directory + "/outputs",
            IMAGE_DIGEST=DIGEST,
            BACKEND_DIGEST=DIGEST,
            FRONTEND_DIGEST=DIGEST,
        )

    def test_real_bundle_contains_pinned_images_and_health_hook(self):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(["python3", str(SCRIPT)], env=self.environment(directory), capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            archive = Path(directory) / (RELEASE + "-0.123.2.tgz")
            with tarfile.open(archive) as package:
                values = json.load(package.extractfile(RELEASE + "/values.yaml"))
                hook = package.extractfile(RELEASE + "/templates/staging-smoke.yaml").read().decode()
                self.assertIn("helm.sh/hook: test", hook)
                self.assertNotIn(RELEASE + "/templates/bundle-values.yaml", package.getnames())
            if RELEASE == "assistant-rh-api":
                self.assertEqual(values["image"]["digest"], DIGEST)
                self.assertEqual(values["streamWorkers"], 50)
            else:
                self.assertEqual(values["image"]["tag"], "sha-" + SHA + "@" + DIGEST)
                self.assertEqual(values["frontend"]["image"]["tag"], "sha-" + SHA + "@" + DIGEST)
                env = values["backend"]["envVars"]
                self.assertEqual(env["LOGIN_REDIRECT_URL"], "/")
                self.assertEqual(env["LOGOUT_REDIRECT_URL"], "/")
                self.assertEqual(env["FEATURE_FLAG_DOCUMENT_UPLOAD"], "DISABLED")
                self.assertFalse(values["backend"]["celeryBeat"]["enabled"])
                workers = values["backend"]["celeryWorkers"]
                self.assertEqual(len(workers), 1)
                self.assertEqual(workers[0]["replicas"], 1)
                self.assertIn("--concurrency=1", workers[0]["args"])
                self.assertIn("--check", str(values["backend"]["migrate"]))
            rendered = subprocess.run(["helm", "template", RELEASE, str(archive), "-n", RELEASE], capture_output=True, text=True)
            self.assertEqual(rendered.returncode, 0, rendered.stderr)
            self.assertIn(DIGEST, rendered.stdout)
            self.assertIn('name: API_STREAM_WORKERS, value: "50"', rendered.stdout)
            self.assertNotIn("kind: Ingress\n", rendered.stdout)

    def test_bad_identity_or_digest_cannot_produce_an_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            for key, value in [
                ("GITHUB_REPOSITORY", "other/project"),
                ("GITHUB_REF_NAME", "main"),
                ("GITHUB_SHA", "invalid"),
                ("GITHUB_RUN_NUMBER", "0"),
                ("GITHUB_RUN_ATTEMPT", "1/2"),
                ("IMAGE_DIGEST" if RELEASE == "assistant-rh-api" else "BACKEND_DIGEST", "latest"),
            ]:
                with self.subTest(key=key):
                    env = self.environment(directory)
                    env[key] = value
                    result = subprocess.run(["python3", str(SCRIPT)], env=env, capture_output=True)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual(list(Path(directory).glob("*.tgz")), [])


if __name__ == "__main__":
    unittest.main()

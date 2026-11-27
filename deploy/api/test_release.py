"""Delivery boundaries and promotion checks, without registry or cluster writes."""

import argparse
import copy
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import release as delivery

SHA = "a" * 40
DIGEST = "sha256:" + "b" * 64
SERVER = "https://staging.example.invalid:6443"


def manifest(tag="v0.1.0-rc.1", target="staging"):
    return {
        "schema": 1,
        "tag": tag,
        "target": target,
        "source_sha": SHA,
        "registry": delivery.STAGING_REGISTRY,
        "cluster_server": SERVER,
        "chart_version": "0.74.0",
        "chart_digest": DIGEST,
        "deployment_id": 7,
        "api": {
            "repository": delivery.STAGING_REGISTRY + "/assistant-rh-api",
            "digest": DIGEST,
        },
    }


class ReleaseTest(unittest.TestCase):
    @patch.object(delivery, "api")
    def test_successful_recovery_resolves_the_full_failed_deployment_chain(self, api):
        def response(path, data=None):
            if path == "deployments/2":
                return {"payload": {"coordinator": "tagged-release-v1", "recovery_of": 1}}
            if path == "deployments/1":
                return {"payload": {"coordinator": "tagged-release-v1"}}

        api.side_effect = response
        delivery.complete({"deployment_id": 3, "supersedes_deployment_id": 2})
        statuses = [(call.args[0], call.args[1]["state"]) for call in api.call_args_list if len(call.args) == 2]
        self.assertEqual(
            statuses,
            [
                ("deployments/3/statuses", "success"),
                ("deployments/2/statuses", "inactive"),
                ("deployments/1/statuses", "inactive"),
            ],
        )

    def test_only_complete_semantic_release_tags_select_an_environment(self):
        self.assertEqual(delivery.classify("v1.2.3-rc.2"), ("1.2.3", "staging"))
        self.assertEqual(delivery.classify("v1.2.3"), ("1.2.3", "production"))
        for tag in [
            "main",
            "v1.2",
            "v01.2.3",
            "v1.2.3-rc.0",
            "v1.2.3-rc.01",
            "v1.2.3+build",
            "v1.2.3\n",
        ]:
            with self.subTest(tag=tag), self.assertRaises(RuntimeError):
                delivery.classify(tag)

    def test_chart_sequence_advances_past_legacy_bundles(self):
        self.assertEqual(delivery.next_chart_version(["0.73.1", "0.9.4", "latest"]), "0.74.0")
        self.assertEqual(delivery.next_chart_version([]), "0.1.0")
        self.assertEqual(delivery.delivery_assets("987")[1], "redelivery-987-delivery.json")
        with self.assertRaises(RuntimeError):
            delivery.delivery_assets("../receipt")

    @patch.object(delivery, "api")
    def test_annotated_tag_resolves_to_the_commit(self, api):
        api.side_effect = [
            {"object": {"type": "tag", "sha": "c" * 40}},
            {"object": {"type": "commit", "sha": SHA}},
        ]
        self.assertEqual(delivery.resolve_tag("v0.1.0-rc.1"), SHA)

    @patch.object(delivery, "upload")
    @patch.object(delivery, "command")
    @patch.object(delivery, "resolve_tag", return_value=SHA)
    def test_main_and_pr_runs_cannot_publish_and_moved_tags_are_refused(self, resolve, command, upload):
        with tempfile.TemporaryDirectory() as temporary:
            event = Path(temporary) / "event.json"
            output = Path(temporary) / "output"
            env = {
                "GITHUB_REPOSITORY": delivery.REPOSITORY,
                "GITHUB_REF": "refs/heads/main",
                "GITHUB_EVENT_PATH": str(event),
                "GITHUB_OUTPUT": str(output),
            }
            run = {
                "event": "push",
                "conclusion": "success",
                "head_branch": "main",
                "head_sha": SHA,
                "head_repository": {"full_name": delivery.REPOSITORY},
            }
            with patch.dict(os.environ, env):
                event.write_text(json.dumps({"workflow_run": run}))
                delivery.plan()
                self.assertIn("delivery=false", output.read_text())
                command.assert_not_called()
                run.update(head_branch="v0.1.0-rc.1", head_sha="d" * 40)
                event.write_text(json.dumps({"workflow_run": run}))
                with self.assertRaisesRegex(RuntimeError, "changed"):
                    delivery.plan()
                run.update(event="pull_request", head_sha=SHA)
                event.write_text(json.dumps({"workflow_run": run}))
                delivery.plan()
                upload.assert_not_called()

    @patch.object(delivery, "api")
    @patch.object(delivery, "download")
    def test_immutable_artifacts_are_never_replaced(self, download, api):
        download.return_value = b"original"
        delivery.upload({"id": 1}, "delivery.json", b"original")
        api.assert_not_called()
        with self.assertRaisesRegex(RuntimeError, "replace"):
            delivery.upload({"id": 1}, "delivery.json", b"different")
        api.assert_not_called()

    @patch.object(delivery, "download")
    @patch.object(delivery, "asset")
    def test_recipe_requires_a_validator_and_the_exact_manifest(self, asset, download):
        value = manifest()
        metadata = {"uploader": {"login": "operator", "type": "User"}}
        asset.side_effect = lambda release, name: metadata if name == "receipt.json" else None
        proof = {
            "validated": True,
            "source_sha": SHA,
            "target": "staging",
            "manifest_sha256": hashlib.sha256(delivery.serialized(value)).hexdigest(),
        }
        download.return_value = delivery.serialized(proof)
        with patch.dict(os.environ, {"RELEASE_VALIDATORS": "operator"}):
            self.assertTrue(delivery.receipt({}, value))
            metadata["uploader"]["type"] = "Bot"
            self.assertIsNone(delivery.receipt({}, value))
            metadata["uploader"]["type"] = "User"
            proof["manifest_sha256"] = "wrong"
            download.return_value = delivery.serialized(proof)
            with self.assertRaises(RuntimeError):
                delivery.receipt({}, value)

    @patch.object(delivery, "api")
    @patch.object(delivery, "download", return_value=b"present")
    @patch.object(delivery, "read_manifest")
    @patch.object(delivery, "receipt", return_value=True)
    def test_stable_requires_a_validated_rc_of_the_same_version_and_sha(self, receipt, read, download, api):
        api.return_value = [{"tag_name": "v0.1.0-rc.1"}]
        value = manifest()
        read.return_value = value
        self.assertEqual(delivery.find_candidate("v0.1.0", SHA), value)
        with self.assertRaises(RuntimeError):
            delivery.find_candidate("v0.1.0", "d" * 40)
        with self.assertRaises(RuntimeError):
            delivery.find_candidate("v0.2.0", SHA)
        receipt.return_value = False
        with self.assertRaises(RuntimeError):
            delivery.find_candidate("v0.1.0", SHA)

    @patch.object(delivery, "api")
    def test_active_delivery_survives_job_timeout_and_blocks_another_tag(self, api):
        api.side_effect = [
            [{"id": 7, "payload": {"coordinator": "tagged-release-v1", "tag": "v0.1.0-rc.1"}}],
            [{"state": "in_progress"}],
        ]
        with self.assertRaisesRegex(RuntimeError, "still active"):
            delivery.active_deployment("staging", "v0.1.0-rc.2")

    def test_ready_requires_the_observed_generation_and_no_reconciliation(self):
        resource = {
            "metadata": {"generation": 2},
            "status": {
                "observedGeneration": 1,
                "conditions": [{"type": "Ready", "status": "True"}],
            },
        }
        self.assertFalse(delivery.ready(resource))
        resource["status"]["observedGeneration"] = 2
        self.assertTrue(delivery.ready(resource))
        resource["status"]["conditions"].append({"type": "Reconciling", "status": "True"})
        self.assertFalse(delivery.ready(resource))

    @patch.object(delivery, "wait_for_receipt")
    @patch.object(delivery, "existing_digest", return_value=None)
    @patch.object(delivery, "upload")
    @patch.object(delivery, "api", return_value={"id": 7})
    @patch.object(delivery, "registry_tags", return_value=["0.73.1"])
    @patch.object(delivery, "active_deployment", return_value=None)
    @patch.object(delivery, "download", return_value=None)
    @patch.object(delivery, "release_for", return_value={"id": 1})
    @patch.object(delivery, "find_candidate")
    @patch.object(delivery, "resolve_tag", return_value=SHA)
    @patch.object(delivery, "command")
    @patch.object(delivery.subprocess, "run")
    def test_promotion_copies_digests_without_building_images(
        self,
        run,
        command,
        resolve,
        candidate,
        release_for,
        download,
        active,
        tags,
        api,
        upload,
        existing,
        wait,
    ):
        candidate.return_value = manifest()
        calls = []

        def execute(*args, **kwargs):
            calls.append(args)
            if args[0] == "git":
                return SHA
            if args[:2] == ("oras", "resolve"):
                return DIGEST
            return ""

        command.side_effect = execute

        def package(args, env, check):
            (Path(env["BUNDLE_OUTPUT_DIR"]) / "assistant-rh-api-0.74.0.tgz").write_bytes(b"chart")

        run.side_effect = package
        before = os.getcwd()
        with tempfile.TemporaryDirectory() as temporary:
            os.chdir(temporary)
            try:
                values = Path("source/deploy/api/production.yaml")
                values.parent.mkdir(parents=True)
                values.write_text("reviewed production values")
                env = {
                    "RELEASE_TAG": "v0.1.0",
                    "SOURCE_SHA": SHA,
                    "GITHUB_SHA": SHA,
                    "RELEASE_VALIDATORS": "operator",
                    "RELEASE_CLUSTER_SERVER": SERVER,
                    "RELEASE_REGISTRY": "rg.fr-par.scw.cloud/assistant-rh-production",
                    "SOURCE_REGISTRY_CONFIG": "source.json",
                    "TARGET_REGISTRY_CONFIG": "target.json",
                }
                with patch.dict(os.environ, env):
                    delivery.publish()
                    reservation = json.loads(next(c.args[2] for c in upload.call_args_list if c.args[1] == "reservation.json"))
                    candidate.reset_mock()
                    candidate.return_value = manifest("v0.1.0-rc.2")
                    download.side_effect = lambda record, name: delivery.serialized(reservation) if name == "reservation.json" else None
                    with patch.object(delivery, "read_manifest", return_value=manifest()), patch.object(delivery, "receipt", return_value=True):
                        delivery.publish()
                    candidate.assert_not_called()  # A newer RC cannot change an interrupted promotion.
            finally:
                os.chdir(before)
        self.assertFalse(any(args[0] == "docker" for args in calls))
        self.assertFalse(any("/conversations" in str(part) for args in calls for part in args))
        self.assertTrue(any(str(part).endswith("/assistant-rh-api:0.74.0") for args in calls for part in args))
        self.assertEqual(sum(args[:2] == ("oras", "cp") for args in calls), 2)
        uploaded = json.loads(next(c.args[2] for c in upload.call_args_list if c.args[1] == "delivery.json"))
        self.assertEqual(uploaded["api"]["digest"], DIGEST)
        self.assertEqual(uploaded["candidate"], "v0.1.0-rc.1")
        self.assertEqual(wait.call_count, 2)


class ConfirmTest(unittest.TestCase):
    def resources(self):
        common = {
            "metadata": {"generation": 1},
            "spec": {},
            "status": {
                "observedGeneration": 1,
                "conditions": [{"type": "Ready", "status": "True"}],
            },
        }
        source = copy.deepcopy(common)
        source["spec"]["url"] = "oci://" + delivery.STAGING_REGISTRY + "/assistant-rh-api"
        source["status"]["artifact"] = {"revision": "0.74.0@" + DIGEST}
        helm = copy.deepcopy(common)
        helm["spec"]["test"] = {"enable": True, "ignoreFailures": False}
        helm["status"].update(
            lastAttemptedRevisionDigest=DIGEST,
            history=[
                {
                    "appVersion": SHA,
                    "chartVersion": "0.74.0+" + DIGEST[7:19],
                    "testHooks": {"smoke": {"phase": "Succeeded"}},
                }
            ],
        )
        workload = {
            "metadata": {"generation": 1},
            "spec": {
                "replicas": 2,
                "template": {"spec": {"containers": [{"image": "registry/image@" + DIGEST}]}},
            },
            "status": {
                "observedGeneration": 1,
                "updatedReplicas": 2,
                "readyReplicas": 2,
                "replicas": 2,
            },
        }
        return {"ocirepository": source, "helmrelease": helm, "deployment": workload}

    @patch.object(delivery, "upload")
    @patch.object(delivery, "api", return_value={"login": "operator"})
    @patch.object(delivery, "read_manifest", side_effect=lambda *a: manifest())
    @patch.object(delivery, "release_for", return_value={"id": 1})
    def test_confirmation_binds_cluster_chart_workloads_and_recipe(self, record, read, api, upload):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "recipe.json"
            path.write_text(
                json.dumps(
                    {
                        "tag": "v0.1.0-rc.1",
                        "source_sha": SHA,
                        "target": "staging",
                        "checks": {"authentication": True, "sse": True, "isolation": True},
                        "report_url": "https://example.invalid/recipe",
                    }
                )
            )
            args = argparse.Namespace(
                tag="v0.1.0-rc.1",
                recipe=str(path),
                kubeconfig="kubeconfig",
                kubectl="kubectl",
                delivery_id="release",
            )
            for defect in (
                None,
                "server",
                "old_generation",
                "old_chart",
                "old_image",
                "failed_hook",
            ):
                resources = self.resources()
                if defect == "old_generation":
                    resources["ocirepository"]["status"]["observedGeneration"] = 0
                if defect == "old_chart":
                    resources["helmrelease"]["status"]["lastAttemptedRevisionDigest"] = "old"
                if defect == "old_image":
                    resources["deployment"]["spec"]["template"]["spec"]["containers"][0]["image"] = "old"
                if defect == "failed_hook":
                    resources["helmrelease"]["status"]["history"][0]["testHooks"]["smoke"]["phase"] = "Failed"

                def command(*parts):
                    if "config" in parts:
                        return "https://wrong.invalid:6443" if defect == "server" else SERVER
                    return json.dumps(resources[parts[parts.index("get") + 1]])

                upload.reset_mock()
                with (
                    self.subTest(defect=defect),
                    patch.dict(os.environ, {"RELEASE_VALIDATORS": "operator"}),
                    patch.object(delivery, "command", side_effect=command),
                ):
                    if defect is None:
                        delivery.confirm(args)
                        proof = json.loads(upload.call_args.args[2])
                        self.assertTrue(proof["validated"])
                        self.assertEqual(proof["source_sha"], SHA)
                        self.assertIn("conversations_images", proof)
                    else:
                        with self.assertRaises(RuntimeError):
                            delivery.confirm(args)
                        upload.assert_not_called()


if __name__ == "__main__":
    unittest.main()

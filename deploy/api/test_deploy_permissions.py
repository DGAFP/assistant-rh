"""Vérifier les droits du déployeur sur les vraies ressources du chart rendu."""

import subprocess
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
RESOURCES = {
    ("", "Service"): "services",
    ("apps", "Deployment"): "deployments",
    ("policy", "PodDisruptionBudget"): "poddisruptionbudgets",
    ("networking.k8s.io", "NetworkPolicy"): "networkpolicies",
}


class DeploymentPermissionsTests(unittest.TestCase):
    def test_rendered_resources_can_be_managed_by_the_namespace_deployer(self):
        result = subprocess.run(
            [
                "helm",
                "template",
                "assistant-rh-api",
                str(ROOT / "deploy/api"),
                "--namespace",
                "assistant-rh-api",
                "--set-string",
                "image.digest=sha256:" + "a" * 64,
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        role = next(r for r in yaml.safe_load_all((ROOT / "deploy/ci-rbac.yaml").read_text()) if r and r["kind"] == "Role")
        self.assertEqual(role["metadata"]["namespace"], "assistant-rh-api")
        for resource in yaml.safe_load_all(result.stdout):
            if resource is None:
                continue
            group = resource["apiVersion"].split("/")[0] if "/" in resource["apiVersion"] else ""
            kind = resource["kind"]
            self.assertIn((group, kind), RESOURCES, f"Ajouter le contrat RBAC du nouveau type {kind}")
            plural = RESOURCES[group, kind]
            for verb in ("get", "create", "update", "patch", "delete"):
                with self.subTest(kind=kind, verb=verb):
                    self.assertTrue(
                        any(
                            group in rule["apiGroups"]
                            and plural in rule["resources"]
                            and verb in rule["verbs"]
                            and (not rule.get("resourceNames") or resource["metadata"]["name"] in rule["resourceNames"])
                            for rule in role["rules"]
                        ),
                        f"{verb} {plural} absent du rôle github-deploy",
                    )


if __name__ == "__main__":
    unittest.main()

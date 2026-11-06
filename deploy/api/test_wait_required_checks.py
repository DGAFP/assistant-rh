"""Exercise the delivery gate with synthetic GitHub run metadata; no network."""

import importlib.util
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("gate", Path(__file__).with_name("wait-required-checks.py"))
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)
SHA = "a" * 40


class DeliveryGateTest(unittest.TestCase):
    def runs(self):
        return [
            dict(
                id=i,
                run_attempt=1,
                head_sha=SHA,
                head_branch=gate.BRANCH,
                event="push",
                path=".github/workflows/" + path,
                status="completed",
                conclusion="success",
            )
            for i, path in enumerate(gate.REQUIRED, start=1)
        ]

    def test_success(self):
        self.assertEqual(gate.evaluate(self.runs(), SHA), [])

    def test_missing_workflow(self):
        self.assertEqual(gate.evaluate(self.runs()[1:], SHA), [gate.REQUIRED[0]])

    def test_other_revision_branch_event_and_path_are_not_proof(self):
        for key, value in [("head_sha", "b" * 40), ("head_branch", "main"), ("event", "pull_request"), ("path", ".github/workflows/other.yml")]:
            with self.subTest(key=key):
                runs = self.runs()
                runs[0][key] = value
                self.assertEqual(gate.evaluate(runs, SHA), [gate.REQUIRED[0]])

    def test_pending_is_not_success(self):
        runs = self.runs()
        runs[0].update(status="in_progress", conclusion=None)
        self.assertEqual(gate.evaluate(runs, SHA), [gate.REQUIRED[0]])

    def test_unsuccessful_completion_blocks(self):
        for conclusion in ["failure", "cancelled", "skipped", "neutral", "timed_out", None]:
            with self.subTest(conclusion=conclusion):
                runs = self.runs()
                runs[0]["conclusion"] = conclusion
                with self.assertRaises(RuntimeError):
                    gate.evaluate(runs, SHA)

    def test_latest_run_wins(self):
        runs = self.runs()
        runs.append(dict(runs[0], id=100, conclusion="failure"))
        with self.assertRaises(RuntimeError):
            gate.evaluate(runs, SHA)

    def test_latest_attempt_wins(self):
        runs = self.runs()
        runs.append(dict(runs[0], run_attempt=2, status="in_progress", conclusion=None))
        self.assertEqual(gate.evaluate(runs, SHA), [gate.REQUIRED[0]])


if __name__ == "__main__":
    unittest.main()

"""The staging write recipe rejects unsafe targets before attempting any network call."""

import subprocess
import sys

import pytest

from scripts.conformance.m0b_values import ROOT


@pytest.mark.parametrize(
    "environment,user,tls",
    [("production", "assistant_rh_migrator", "verify-ca"), ("staging", "admin", "verify-ca"), ("staging", "assistant_rh_migrator", "require")],
)
def test_invalid_operator_target_is_rejected_before_loading_cloud_credentials(tmp_path, environment, user, tls):
    operator = tmp_path / "operator.env"
    operator.write_text(f"APP_ENV={environment}\nSCW_POSTGRES_DSN='host=127.0.0.1 port=1 user={user} dbname=assistant_rh sslmode={tls}'\n")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.integration.m2_staging_operator",
            "--operator-env-file",
            str(operator),
            "--cloud-env-file",
            str(tmp_path / "missing-credentials"),
            "--kubeconfig",
            str(tmp_path / "missing-config"),
            "--private-dir",
            str(tmp_path / "private"),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert result.returncode != 0 and "Expected the verified TLS staging migrator DSN" in result.stderr
    assert not (tmp_path / "private" / "individual-report.json").exists()

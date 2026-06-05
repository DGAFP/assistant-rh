from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

DEPRECATED_ACTIVE_DEPLOYMENT_ARTIFACTS = (
    ".buildpacks",
    "Procfile",
    "Aptfile",
    "docs/SCALENO_BUILDPACK_COMPATIBILITY.md",
)

ACTIVE_DEPLOYMENT_PATHS = (
    REPO_ROOT / ".github" / "workflows",
    REPO_ROOT / ".github" / "scripts",
    REPO_ROOT / "Dockerfile.streamlit",
)


def test_scalingo_buildpack_artifacts_are_not_active_configuration() -> None:
    present = [path for path in DEPRECATED_ACTIVE_DEPLOYMENT_ARTIFACTS if (REPO_ROOT / path).exists()]

    assert present == []


def test_active_deployment_automation_does_not_reference_scalingo_buildpacks() -> None:
    needles = (".buildpacks", "Procfile", "Aptfile", "SCALENO_BUILDPACK_COMPATIBILITY")
    matches: list[str] = []

    for root in ACTIVE_DEPLOYMENT_PATHS:
        files = [root] if root.is_file() else [path for path in root.rglob("*") if path.is_file()]
        for file_path in files:
            text = file_path.read_text(encoding="utf-8")
            for needle in needles:
                if needle in text:
                    matches.append(f"{file_path.relative_to(REPO_ROOT)} contains {needle}")

    assert matches == []

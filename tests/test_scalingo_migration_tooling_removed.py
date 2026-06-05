from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

REMOVED_OPERATIONAL_ARTIFACTS = (
    "Dockerfile.scalingo_to_scaleway_migration",
    "scripts/copy_scalingo_tables_to_scaleway.py",
    "scripts/copy_scaleway_chunks_to_scalingo.py",
    "scripts/copy_service_public_scw_to_scalingo.py",
    "docs/SCALINGO_TO_SCALEWAY_STREAMLIT_MIGRATION.md",
    ".agents/skills/syncing-scalingo-to-supabase",
    ".agents/skills/syncing-scalingo-to-supabase.skill",
    ".skills/syncing-scalingo-to-supabase",
)

REMOVED_PUBLIC_FIXTURE_GLOB = "legifrance_scalingo_*.json"

ACTIVE_OPERATIONAL_PATHS = (
    REPO_ROOT / "scripts",
    REPO_ROOT / ".agents" / "skills",
    REPO_ROOT / ".skills",
)

ACTIVE_OPERATIONAL_NEEDLES = (
    "copy_scalingo_tables_to_scaleway",
    "copy_scaleway_chunks_to_scalingo",
    "copy_service_public_scw_to_scalingo",
    "Dockerfile.scalingo_to_scaleway_migration",
    "syncing-scalingo-to-supabase",
    "legifrance_scalingo_",
)


def test_historical_scalingo_migration_tools_are_not_public_artifacts() -> None:
    present = [path for path in REMOVED_OPERATIONAL_ARTIFACTS if (REPO_ROOT / path).exists()]

    assert present == []


def test_historical_scalingo_comparison_fixtures_are_not_public_artifacts() -> None:
    fixtures = sorted(path.relative_to(REPO_ROOT) for path in (REPO_ROOT / "tests").glob(REMOVED_PUBLIC_FIXTURE_GLOB))

    assert fixtures == []


def test_active_operational_surfaces_do_not_reference_removed_scalingo_migration_tools() -> None:
    matches: list[str] = []

    for root in ACTIVE_OPERATIONAL_PATHS:
        if not root.exists():
            continue
        files = [path for path in root.rglob("*") if path.is_file()]
        for file_path in files:
            text = file_path.read_text(encoding="utf-8", errors="ignore")
            for needle in ACTIVE_OPERATIONAL_NEEDLES:
                if needle in text:
                    matches.append(f"{file_path.relative_to(REPO_ROOT)} contains {needle}")

    assert matches == []

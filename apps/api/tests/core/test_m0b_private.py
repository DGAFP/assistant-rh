"""Public, synthetic checks for the private recording/download boundary."""

import hashlib
import subprocess
from types import SimpleNamespace

import pytest

from scripts.conformance import m0b_private as private
from scripts.conformance.m0b_record import record


@pytest.mark.anyio
@pytest.mark.parametrize("symlink", [False, True])
async def test_recording_inside_checkout_is_rejected_before_configuration(tmp_path, monkeypatch, symlink):
    root = tmp_path / "checkout"
    root.mkdir()
    monkeypatch.setattr(private, "ROOT", root)
    if symlink:
        alias = tmp_path / "alias"
        alias.symlink_to(root, target_is_directory=True)
        target = alias / "capture"
    else:
        target = root / "capture"
    with pytest.raises(AssertionError, match="outside the Git checkout"):
        await record(target, tmp_path / "no-credentials.env")
    assert not target.exists()


def test_other_git_checkouts_are_also_rejected(tmp_path):
    repo = tmp_path / "other"
    subprocess.run(["git", "init", "--quiet", str(repo)], check=True)
    with pytest.raises(AssertionError, match="outside every Git checkout"):
        private.private_directory(repo / "capture")


def test_download_requires_token_before_network(tmp_path, monkeypatch):
    monkeypatch.delenv("HF_TOKEN", raising=False)
    monkeypatch.delenv("HUGGINGFACE_HUB_TOKEN", raising=False)
    with pytest.raises(AssertionError, match="HF_TOKEN is required"):
        private.download(tmp_path / "capture")


@pytest.mark.parametrize("is_private,valid_hash", [(False, True), (True, False), (True, True)])
def test_download_checks_privacy_revision_and_exact_bytes(tmp_path, monkeypatch, is_private, valid_hash):
    payload = b"synthetic recording"
    checksums = tmp_path / "SHA256SUMS"
    checksums.write_text(hashlib.sha256(payload).hexdigest() + "  " + private.ARCHIVE_NAME + "\n")
    monkeypatch.setattr(private, "CHECKSUMS", checksums)
    monkeypatch.setenv("HF_TOKEN", "synthetic-token")
    calls = []

    def info(repo):
        assert repo == private.DATASET
        return SimpleNamespace(private=is_private, sha="immutable-revision")

    def fetch(**kwargs):
        calls.append(kwargs)
        path = kwargs["local_dir"] / private.ARCHIVE_NAME
        path.write_bytes(payload if valid_hash else b"wrong recording")
        return str(path)

    monkeypatch.setattr("huggingface_hub.HfApi", lambda **kwargs: SimpleNamespace(dataset_info=info))
    monkeypatch.setattr("huggingface_hub.hf_hub_download", fetch)
    directory = tmp_path / "private"
    if not is_private or not valid_hash:
        with pytest.raises(AssertionError, match="must be private" if not is_private else "checksum mismatch"):
            private.download(directory)
    else:
        archive = private.download(directory)
        assert archive.read_bytes() == payload
        assert archive.stat().st_mode & 0o777 == 0o600
        assert directory.stat().st_mode & 0o777 == 0o700
    assert bool(calls) == is_private
    if calls:
        assert calls[0]["revision"] == "immutable-revision"
        assert calls[0]["repo_id"] == private.DATASET
        assert calls[0]["filename"] == private.DATASET_PATH


def test_private_directory_does_not_change_permissions_of_existing_shared_directory(tmp_path):
    directory = tmp_path / "shared"
    directory.mkdir(mode=0o755)
    with pytest.raises(AssertionError, match="must be private"):
        private.private_directory(directory)
    assert directory.stat().st_mode & 0o777 == 0o755


def test_full_corpus_archive_is_not_distributed_in_public_checkout():
    assert not (private.CHECKSUMS.parent / private.ARCHIVE_NAME).exists()

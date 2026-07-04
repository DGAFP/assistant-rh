from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

# Formats bureautiques acceptés dans la dropzone (décision 2026-07-04, flux
# .doc/.xlsx récurrent dans les sources ministérielles). L'import UI vérifie
# la signature binaire; le bronze convertit en PDF avant OCR. Le cache OCR
# reste indexé par le sha256 du fichier d'origine, pas du PDF converti.
CONVERTIBLE_EXTENSIONS: tuple[str, ...] = (".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx")


class PdfConversionError(RuntimeError):
    """Échec de conversion bureautique -> PDF (LibreOffice headless)."""


def ensure_pdf(source_path: Path, workdir: Path, *, timeout: int = 300) -> Path:
    """Retourne un PDF pour le fichier source: tel quel s'il est déjà PDF,
    sinon converti via `soffice --headless --convert-to pdf`.

    LibreOffice nomme sa sortie `{stem}.pdf` dans --outdir; workdir doit être
    dédié au document courant pour éviter les collisions de stem.
    """
    extension = source_path.suffix.lower()
    if extension == ".pdf":
        return source_path
    if extension not in CONVERTIBLE_EXTENSIONS:
        raise PdfConversionError(f"Format non convertible en PDF: {source_path.name!r} (attendus: .pdf, {', '.join(CONVERTIBLE_EXTENSIONS)})")

    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if not soffice:
        raise PdfConversionError("LibreOffice (soffice) introuvable: requis pour convertir les sources bureautiques en PDF.")

    workdir.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [
            soffice,
            "--headless",
            "--norestore",
            "--convert-to",
            "pdf",
            "--outdir",
            str(workdir),
            str(source_path),
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    converted = workdir / f"{source_path.stem}.pdf"
    if result.returncode != 0 or not converted.exists():
        detail = (result.stderr or result.stdout or "").strip()[:500]
        raise PdfConversionError(f"Conversion PDF échouée pour {source_path.name!r}: {detail or 'sortie absente'}")
    return converted

"""Bronze partagé: dropzone -> sha256 -> conversion PDF -> OCR -> annotations.

Les non-PDF (.doc/.docx/.xls/.xlsx/.ppt/.pptx) sont convertis via LibreOffice
avant OCR; le cache OCR (bucket bronze) est indexé par le sha256 du fichier
d'origine, donc un re-run ne repaye jamais l'OCR ni la conversion. Si un
annotateur d'images est fourni, les crops de la réponse OCR sont annotés par
VLM (cache bronze par sha256, jamais de re-paiement) et les descriptions
remplacent les références `![img-N]` du markdown transmis au silver.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from ..utils.convert import ensure_pdf
from ..utils.grist import ManifestRow
from ..utils.helpers import ensure_dir, sha256_file, utc_now_iso, write_json
from ..utils.image_annotation import AlbertImageAnnotator, annotate_ocr_images, apply_image_annotations
from ..utils.ocr import OcrProvider, OcrResult
from ..utils.page_vision import (
    PAGE_VISION_LOGIC_VERSION,
    AlbertPageVisionReconstructor,
    apply_page_reconstructions,
    reconstruct_pages,
    select_risk_positions,
)
from ..utils.pdf_store import PdfSourceStore
from .identity import MinistryIdentity


@dataclass
class BronzeAsset:
    """Un document prêt pour le silver: fichier source + OCR (cache ou live).

    ocr porte le markdown enrichi (descriptions d'images à la place des refs);
    ocr_markdown_raw conserve la sortie OCR brute (contrat de la colonne
    rag_documents.doc_markdown_raw: toujours le texte AVANT transformations
    VLM). Le cache bronze archive la réponse OCR brute.
    """

    row: ManifestRow
    sha256: str
    source_path: Path
    ocr: OcrResult
    ocr_from_cache: bool
    ocr_markdown_raw: str = ""
    image_annotations: dict[str, dict[str, str]] = field(default_factory=dict)
    annotations_from_cache: bool = False
    page_reconstructions: dict[int, str] = field(default_factory=dict)
    page_vision_from_cache: bool = False
    # False si une panne TRANSITOIRE (VLM/rendu) a empêché de reconstruire
    # certaines pages à risque : le doc est servi en OCR mais la réconciliation
    # doit le re-traiter au prochain run (revue #320 finding 1). True si complet
    # (aucune page à risque, ou toutes ok/rejetées, ou servi depuis le cache).
    page_vision_complete: bool = True


class BronzeRepository:
    def __init__(self, bronze_dir: Path):
        self.root = ensure_dir(bronze_dir)
        self.downloads_dir = ensure_dir(self.root / "downloads")
        self.ocr_dir = ensure_dir(self.root / "ocr")
        self.manifest_dir = ensure_dir(self.root / "manifests")

    def save_ocr_markdown(self, short_id: str, markdown: str) -> Path:
        path = self.ocr_dir / f"{short_id}.md"
        path.write_text(markdown, encoding="utf-8")
        return path

    def save_manifest_snapshot(self, run_id: str, snapshot: dict[str, Any]) -> Path:
        path = self.manifest_dir / f"manifest_{run_id}.json"
        write_json(path, snapshot)
        return path


class BronzeFetcher:
    def __init__(
        self,
        identity: MinistryIdentity,
        store: PdfSourceStore,
        ocr_provider: OcrProvider,
        repository: BronzeRepository,
        *,
        target_env: str,
        force_reocr: bool = False,
        force_reprocess: bool = False,
        image_annotator: Optional[AlbertImageAnnotator] = None,
        max_images_per_doc: int = 150,
        page_reconstructor: Optional[AlbertPageVisionReconstructor] = None,
        page_vision_max_pages: int = 60,
    ):
        self.identity = identity
        self.store = store
        self.ocr_provider = ocr_provider
        self.repository = repository
        self.target_env = target_env
        self.force_reocr = force_reocr
        # force_reprocess: retraite silver/gold/page-vision en réutilisant le
        # cache OCR, mais IGNORE les caches d'enrichissement (annotations +
        # page-vision) pour ré-appliquer un changement de traitement (revue #320
        # finding 4a). force_reocr l'implique.
        self.force_reprocess = force_reocr or force_reprocess
        self.image_annotator = image_annotator
        self.max_images_per_doc = max_images_per_doc
        self.page_reconstructor = page_reconstructor
        self.page_vision_max_pages = page_vision_max_pages

    def download_and_hash(self, row: ManifestRow) -> tuple[Path, str]:
        """Télécharge le fichier dropzone et retourne (chemin local, sha256)."""
        filename = Path(row.cle_bucket).name or f"{row.short_id}.pdf"
        destination = ensure_dir(self.repository.downloads_dir / row.short_id) / filename
        local_path = self.store.fetch_source_pdf(row.cle_bucket, destination)
        return local_path, sha256_file(local_path)

    def fetch_asset(self, row: ManifestRow, source_path: Path, sha256: str) -> BronzeAsset:
        """OCR (cache-hit bronze sinon appel provider) + annotations d'images."""
        cached = None
        if not self.force_reocr:
            cached = self.store.get_cached_ocr(
                self.target_env,
                self.identity.ministere,
                self.ocr_provider.name,
                self.ocr_provider.version,
                sha256,
            )

        if cached is not None:
            ocr_result = cached
            from_cache = True
        else:
            pdf_path = ensure_pdf(source_path, source_path.parent / "converted")
            ocr_result = self.ocr_provider.ocr_pdf(pdf_path.read_bytes(), document_name=pdf_path.name)
            self.store.put_pdf(self.target_env, self.identity.ministere, sha256, pdf_path)
            self.store.put_ocr(self.target_env, self.identity.ministere, sha256, ocr_result)
            from_cache = False

        raw_markdown = ocr_result.markdown

        # Détection des pages à risque sur l'OCR PUR (revue #319 M5): AVANT toute
        # transformation (les descriptions d'images fausseraient les seuils de
        # taille/structure).
        risk_positions = select_risk_positions(ocr_result.pages)

        # Annotation d'images sur l'OCR COMPLET (revue #320 finding 3): le cache
        # d'annotations doit couvrir TOUTES les images du doc, indépendamment de
        # la page vision — sinon un cache partiel (images des pages reconstruites
        # omises) laisserait ces images non annotées si la page vision est plus
        # tard désactivée/rejetée.
        annotations, annotations_from_cache = self._annotate_images(ocr_result, sha256)
        if annotations:
            ocr_result = OcrResult(
                provider=ocr_result.provider,
                version=ocr_result.version,
                markdown=apply_image_annotations(ocr_result.markdown, annotations),
                pages=[{**page, "markdown": apply_image_annotations(str(page.get("markdown") or ""), annotations)} for page in ocr_result.pages],
                raw=ocr_result.raw,
            )

        # Re-passe vision pleine page en DERNIER: remplace le markdown des pages
        # à schéma aplati (positions détectées sur l'OCR pur) par une
        # reconstruction VLM fidèle aux associations gauche→droite (ex.
        # CONTRAT/AVENANT MASA). page_vision_complete=False si une panne
        # transitoire a empêché de reconstruire certaines pages -> re-traitement
        # au prochain run (revue #320 finding 1).
        reconstructions, page_vision_from_cache, page_vision_complete = self._reconstruct_pages(
            ocr_result, source_path, sha256, positions=risk_positions
        )
        if reconstructions:
            ocr_result = apply_page_reconstructions(ocr_result, reconstructions)

        # L'artefact bronze reste la sortie du provider: l'enrichi vit en
        # silver (doc_markdown), le brut reste diffable/déboguable.
        self.repository.save_ocr_markdown(row.short_id, raw_markdown)
        return BronzeAsset(
            row=row,
            sha256=sha256,
            source_path=source_path,
            ocr=ocr_result,
            ocr_from_cache=from_cache,
            ocr_markdown_raw=raw_markdown,
            image_annotations=annotations,
            annotations_from_cache=annotations_from_cache,
            page_reconstructions=reconstructions,
            page_vision_from_cache=page_vision_from_cache,
            page_vision_complete=page_vision_complete,
        )

    def _annotate_images(self, ocr_result: OcrResult, sha256: str) -> tuple[dict[str, dict[str, str]], bool]:
        """Annotations VLM du document: cache bronze d'abord, appels sinon.

        force_reocr contourne aussi ce cache (« tout est retraité »). Un lot
        avec échecs n'est JAMAIS mis en cache: le geler transformerait une
        panne transitoire du VLM en perte d'enrichissement permanente — le
        run suivant retentera les images manquantes.
        """
        if self.image_annotator is None:
            return {}, False

        # force_reprocess (⊇ force_reocr) ré-annote pour propager un changement
        # de traitement et garder le cache d'annotations complet (revue #320).
        if not self.force_reprocess:
            cached = self.store.get_cached_image_annotations(
                self.target_env,
                self.identity.ministere,
                self.image_annotator.name,
                self.image_annotator.version,
                sha256,
            )
            if cached is not None:
                return cached, True

        annotations, failed = annotate_ocr_images(
            ocr_result.pages,
            self.image_annotator,
            max_images=self.max_images_per_doc,
        )
        if failed:
            print(f"[warn] annotations incomplètes ({len(failed)} échec(s)) — lot non mis en cache, retentative au prochain run")
        else:
            self.store.put_image_annotations(
                self.target_env,
                self.identity.ministere,
                self.image_annotator.name,
                self.image_annotator.version,
                sha256,
                annotations,
            )
        return annotations, False

    def _reconstruct_pages(self, ocr_result: OcrResult, source_path: Path, sha256: str, *, positions: list[int]) -> tuple[dict[int, str], bool, bool]:
        """Re-passe vision des pages à risque: ({position: markdown}, from_cache, complete).

        ``positions`` (détectées sur l'OCR pur en amont) évitent tout rendu si le
        doc n'a pas de page structurée. ``complete`` vaut False si une panne
        TRANSITOIRE (VLM/rendu/conversion) a empêché de reconstruire des pages :
        le doc est servi en OCR mais la réconciliation le re-traitera (finding 1).
        Un lot avec panne n'est jamais mis en cache (retenté au prochain run)."""
        if self.page_reconstructor is None:
            return {}, False, True

        # Clé de cache: version reconstructeur (modèle+prompt+dpi) + version OCR
        # (les positions sont relatives à la liste de pages OCR) + version de la
        # LOGIQUE (détecteur/garde-fou) + max_pages (revue #319 M2 / #320 M4b) —
        # tout changement de ces règles change l'ensemble reconstruit.
        cache_version = (
            f"{self.page_reconstructor.version}+ocr-{self.ocr_provider.version}+{PAGE_VISION_LOGIC_VERSION}+max{self.page_vision_max_pages}"
        )

        # force_reprocess (⊇ force_reocr) ré-applique la page vision (cache ignoré).
        if not self.force_reprocess:
            cached = self.store.get_cached_page_reconstructions(
                self.target_env,
                self.identity.ministere,
                self.page_reconstructor.name,
                cache_version,
                sha256,
            )
            if cached is not None:
                return cached, True, True

        if not positions:
            # Cache l'absence de page à risque: un re-run court-circuite la
            # re-sélection (aucun rendu/VLM n'a lieu de toute façon).
            self.store.put_page_reconstructions(self.target_env, self.identity.ministere, self.page_reconstructor.name, cache_version, sha256, {})
            return {}, False, True

        # La page vision est un ENRICHISSEMENT: obtenir le PDF (conversion
        # LibreOffice pour les .xlsx/.pptx) et rendre les pages ne doit JAMAIS
        # faire échouer l'ingestion d'un doc dont l'OCR est déjà valide (cache).
        # Panne de conversion/rendu -> on saute (page en OCR), non cachée et
        # marquée incomplète (re-traitement au prochain run).
        try:
            pdf_path = ensure_pdf(source_path, source_path.parent / "converted")
            reconstructions, failed = reconstruct_pages(
                pdf_path.read_bytes(),
                ocr_result.pages,
                self.page_reconstructor,
                positions=positions,
                max_pages=self.page_vision_max_pages,
            )
        except Exception as exc:  # noqa: BLE001 — enrichissement best-effort, l'ingestion continue en OCR
            print(f"[warn] re-passe vision indisponible (rendu/conversion PDF) — doc conservé en OCR: {exc}")
            return {}, False, False
        if failed:
            # Panne TRANSITOIRE (VLM/rendu par page): lot non caché, doc marqué
            # incomplet -> la réconciliation le re-traitera (pas seulement au
            # prochain --force-reprocess manuel).
            print(f"[warn] re-passe vision incomplète ({len(failed)} page(s) en panne) — doc conservé en OCR, re-traité au prochain run")
            return reconstructions, False, False
        self.store.put_page_reconstructions(
            self.target_env, self.identity.ministere, self.page_reconstructor.name, cache_version, sha256, reconstructions
        )
        return reconstructions, False, True

    def snapshot_manifest(self, run_id: str, rows: list[ManifestRow]) -> Path:
        return self.repository.save_manifest_snapshot(
            run_id,
            {
                "run_id": run_id,
                "created_at": utc_now_iso(),
                "corpus": rows[0].corpus if rows else self.identity.corpus,
                "rows": [
                    {
                        "record_id": row.record_id,
                        "uid": row.uid,
                        "titre": row.titre,
                        "cle_bucket": row.cle_bucket,
                        "statut": row.statut,
                    }
                    for row in rows
                ],
            },
        )

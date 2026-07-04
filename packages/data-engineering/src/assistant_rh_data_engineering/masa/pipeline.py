from __future__ import annotations

import traceback
import uuid
from typing import Any, Optional

from ..utils.db import RagDbWriter
from ..utils.grist import (
    STATUT_ERREUR,
    STATUT_IGNORE,
    STATUT_OK,
    STATUT_SUPPRIME,
    STATUTS_INACTIFS,
    GristClient,
    ManifestRow,
    fetch_validated_manifest,
)
from ..utils.helpers import utc_now_iso
from ..utils.image_annotation import AlbertImageAnnotator
from ..utils.object_storage import ObjectStorageConfig, ScalewayObjectStorageSync
from ..utils.ocr import build_ocr_provider
from ..utils.pdf_store import PdfSourceStore
from .bronze import MasaBronzeFetcher, MasaBronzeRepository
from .config import CHUNK_TABLE, CORPUS, DOC_SOURCE, MINISTERE, MasaPipelineConfig
from .gold import GoldRepository, MasaGoldBuilder
from .silver import MasaSilverBuilder, SilverRepository


def plan_reconciliation(
    expected: dict[str, ManifestRow],
    current: dict[str, dict[str, Any]],
    checksums: dict[str, str],
    *,
    force_reocr: bool = False,
    protected: frozenset[str] | set[str] = frozenset(),
) -> dict[str, list[str]]:
    """Delta pur (testable sans I/O): classe chaque uid attendu en ingest ou
    ignore_inchange, et les uids en base absents du manifest en delete.

    Un document est inchangé si son sha256 correspond. Divergence vs le
    template MI: pas d'exigence nb_chunks > 0 — le filtre de payload gold
    (MIN_CHUNK_PAYLOAD_CHARS) rend légitime un document à zéro chunk (doc
    image-only dont les images sont décoratives), et ingest_document_bundle
    étant transactionnel, un checksum en base prouve que le lot complet a été
    écrit (la leçon MATTE « zéro chunk ⇒ retraiter » visait des ingestions
    legacy non atomiques); sans cela, ces documents seraient retraités à
    chaque run sans jamais converger.
    `protected` (uids dont le téléchargement a échoué) n'est JAMAIS classé en
    delete: un incident S3 transitoire ne doit pas supprimer un document sain.
    """
    to_ingest: list[str] = []
    unchanged: list[str] = []
    for short_id in sorted(expected):
        state = current.get(short_id)
        checksum = checksums.get(short_id)
        if not force_reocr and state is not None and checksum is not None and state.get("checksum") == checksum:
            unchanged.append(short_id)
        else:
            to_ingest.append(short_id)

    orphans = sorted(set(current) - set(expected) - set(protected))
    return {"ingest": to_ingest, "ignore_inchange": unchanged, "delete": orphans}


class MasaPipeline:
    """Pipeline médaillon MASA: manifest Grist -> bronze/silver/gold -> Postgres.

    Chaque run réconcilie la base avec le manifest (décision 2026-07-03):
    doc absent ou abrogé => suppression cascade tracée; doc inchangé (sha256)
    => ignore_inchange sans re-payer l'OCR; erreurs par document tracées en
    writeback, le run continue.
    """

    def __init__(
        self,
        config: Optional[MasaPipelineConfig] = None,
        *,
        grist_client: Optional[GristClient] = None,
        store: Optional[PdfSourceStore] = None,
        ocr_provider: Any = None,
        db_writer: Optional[RagDbWriter] = None,
        image_annotator: Any = None,
        schema: str = "public",
    ):
        self.config = config or MasaPipelineConfig()
        self.grist = grist_client or GristClient()
        self.store = store or PdfSourceStore(ScalewayObjectStorageSync(ObjectStorageConfig.from_env()))
        # include_images suit l'enrichissement: le namespace de cache OCR
        # (version suffixée -img) doit contenir les crops que l'annotateur
        # consomme — un cache sans images bloquerait l'enrichissement.
        self.ocr_provider = ocr_provider or build_ocr_provider(
            self.config.ocr_provider_name,
            include_images=self.config.images.enabled,
        )
        self._image_annotator = image_annotator
        self._db_writer = db_writer
        self.schema = schema

        self.bronze_repo = MasaBronzeRepository(self.config.paths.bronze_dir)
        self.silver_builder = MasaSilverBuilder(self.config.silver)
        self.silver_repo = SilverRepository(self.config.paths.silver_dir)
        self.gold_builder = MasaGoldBuilder(self.config.embeddings, self.config.gold)
        self.gold_repo = GoldRepository(self.config.paths.gold_dir)

    @property
    def db_writer(self) -> RagDbWriter:
        if self._db_writer is None:
            self._db_writer = RagDbWriter(schema=self.schema, chunk_table=CHUNK_TABLE)
        return self._db_writer

    @property
    def image_annotator(self) -> Any:
        # Lazy comme db_writer: le dry-run et les configs enrichissement-off
        # ne doivent pas exiger ALBERT_API_KEY.
        if self._image_annotator is None and self.config.images.enabled:
            self._image_annotator = AlbertImageAnnotator(model=self.config.images.vlm_model)
        return self._image_annotator

    def run(
        self,
        *,
        doc_ids: Optional[list[str]] = None,
        dry_run: bool = False,
        force_reocr: bool = False,
        skip_grist_writeback: bool = False,
        ingest: bool = True,
    ) -> dict[str, Any]:
        run_id = f"{MINISTERE}-{utc_now_iso().replace(':', '').replace('.', '')}-{uuid.uuid4().hex[:8]}"
        started_at = utc_now_iso()

        manifest = fetch_validated_manifest(self.grist, CORPUS)
        writeback_enabled = not (dry_run or skip_grist_writeback)

        rejected_fields_by_id = {record["id"]: record.get("fields") or {} for record in self.grist.list_records()} if manifest.rejected else {}
        details: dict[str, dict[str, Any]] = {}
        rejected_uids: set[str] = set()
        for rejected in manifest.rejected:
            if rejected.uid:
                rejected_uids.add(rejected.uid.strip().upper())
            details[rejected.uid or f"record:{rejected.record_id}"] = {
                "statut": STATUT_ERREUR,
                "erreur": "; ".join(rejected.errors),
            }
            # Colonne de statut partagée: une ligne rejetée qui porte déjà
            # a_supprimer/supprime garde son statut (l'intention de
            # l'opérateur ou l'état terminal ne sont jamais écrasés par une
            # erreur de saisie ailleurs sur la ligne).
            already = str(rejected_fields_by_id.get(rejected.record_id, {}).get("statut_ingestion") or "").strip().lower()
            if writeback_enabled and already not in STATUTS_INACTIFS:
                self._writeback(
                    rejected.record_id,
                    statut=STATUT_ERREUR,
                    erreur="; ".join(rejected.errors),
                )

        requested = {uid.strip().upper() for uid in doc_ids or [] if uid.strip()}
        expected: dict[str, ManifestRow] = {}
        abrogated: dict[str, ManifestRow] = {}
        for row in manifest.valid:
            if requested and row.short_id not in requested:
                continue
            if row.statut == "en_vigueur":
                expected[row.short_id] = row
            else:
                abrogated[row.short_id] = row

        current: dict[str, dict[str, Any]] = {}
        if ingest:
            current = self.db_writer.list_short_ids_with_checksum(DOC_SOURCE)

        fetcher = MasaBronzeFetcher(
            self.store,
            self.ocr_provider,
            self.bronze_repo,
            target_env=self.config.target_env,
            force_reocr=force_reocr,
            image_annotator=self.image_annotator if not dry_run else None,
            max_images_per_doc=self.config.images.max_images_per_doc,
        )

        # Download + hash d'abord (lecture seule): le delta sha256 décide
        # ensuite qui passe par OCR/silver/gold — jamais les docs inchangés.
        checksums: dict[str, str] = {}
        local_paths: dict[str, Any] = {}
        failures: dict[str, str] = {}
        for short_id, row in sorted(expected.items()):
            try:
                local_path, checksum = fetcher.download_and_hash(row)
                checksums[short_id] = checksum
                local_paths[short_id] = local_path
            except Exception as exc:  # noqa: BLE001 — erreur par document, le run continue
                failures[short_id] = str(exc)

        plan = plan_reconciliation(
            {uid: row for uid, row in expected.items() if uid not in failures},
            current,
            checksums,
            force_reocr=force_reocr,
            protected=set(failures) | rejected_uids,
        )
        # Un filtre --doc-id restreint le manifest vu par le run: seuls les
        # uids explicitement demandés restent supprimables (doc abrogé ou
        # retiré du manifest), jamais le reste du corpus.
        if requested:
            orphans = [uid for uid in plan["delete"] if uid in requested]
        else:
            orphans = plan["delete"]

        if dry_run:
            return {
                "run_id": run_id,
                "ministere": MINISTERE,
                "target_env": self.config.target_env,
                "dry_run": True,
                "expected": len(expected),
                "failed_count": len(failures),
                "rejected_count": len(manifest.rejected),
                "plan": {
                    "ingest": plan["ingest"],
                    "ignore_inchange": plan["ignore_inchange"],
                    "delete": orphans,
                    "acquittement_supprime": sorted(uid for uid in abrogated if uid not in current and uid not in orphans) if ingest else [],
                    "erreur": failures,
                },
            }

        fetcher.snapshot_manifest(run_id, list(expected.values()) + list(abrogated.values()))

        ingested: list[str] = []
        skipped: list[str] = []

        for short_id in plan["ignore_inchange"]:
            row = expected[short_id]
            nb_chunks = int(current.get(short_id, {}).get("nb_chunks") or 0)
            skipped.append(short_id)
            # La trace de run garde le détail ignore_inchange; la ligne Grist
            # reste sur le statut consolidé « ok » (colonne de statut unique),
            # la fraîcheur étant portée par derniere_ingestion.
            details[short_id] = {"statut": STATUT_IGNORE, "nb_chunks": nb_chunks}
            if writeback_enabled:
                self._writeback(
                    row.record_id,
                    statut=STATUT_OK,
                    nb_chunks=nb_chunks,
                    hash_contenu=checksums.get(short_id, ""),
                )

        for short_id in plan["ingest"]:
            row = expected[short_id]
            try:
                asset = fetcher.fetch_asset(row, local_paths[short_id], checksums[short_id])
                silver_bundle = self.silver_builder.persist_bundle(self.silver_repo, asset)
                gold_bundle = self.gold_builder.persist_bundle(self.gold_repo, silver_bundle)
                nb_chunks = len(gold_bundle.chunks)
                if ingest:
                    # Une seule transaction: un échec en cours de route ne doit
                    # pas laisser un checksum à jour avec des chunks périmés
                    # (le run suivant classerait le doc ignore_inchange).
                    self.db_writer.ingest_document_bundle(
                        silver_bundle.document,
                        silver_bundle.sections,
                        gold_bundle.chunks,
                    )
                ingested.append(short_id)
                details[short_id] = {
                    "statut": STATUT_OK,
                    "nb_chunks": nb_chunks,
                    "ocr_from_cache": asset.ocr_from_cache,
                    "images_annotees": len(asset.image_annotations),
                    "annotations_from_cache": asset.annotations_from_cache,
                }
                if writeback_enabled:
                    self._writeback(
                        row.record_id,
                        statut=STATUT_OK,
                        nb_chunks=nb_chunks,
                        hash_contenu=asset.sha256,
                    )
            except Exception as exc:  # noqa: BLE001 — erreur par document, le run continue
                failures[short_id] = str(exc)
                traceback.print_exc()

        for short_id, error in failures.items():
            details[short_id] = {"statut": STATUT_ERREUR, "erreur": error[:500]}
            row = expected.get(short_id)
            if row is not None and writeback_enabled:
                self._writeback(row.record_id, statut=STATUT_ERREUR, erreur=error[:500])

        deleted: list[str] = []
        if orphans and ingest:
            counts = self.db_writer.delete_documents_cascade(orphans, source=DOC_SOURCE)
            deleted = orphans
            for short_id in orphans:
                details[short_id] = {"statut": STATUT_SUPPRIME, "cascade": counts}
                abrogated_row = abrogated.get(short_id)
                if abrogated_row is not None and writeback_enabled:
                    self._writeback(abrogated_row.record_id, statut=STATUT_SUPPRIME, nb_chunks=0)

        # Acquittement des lignes inactives sans document en base (a_supprimer
        # jamais ingéré, abrogé déjà purgé): statut => supprime, une seule fois
        # — une ligne déjà à « supprime » n'est pas re-PATCHée à chaque run.
        # Uniquement en mode ingest: sans accès base, « pas dans current » ne
        # prouve rien et l'acquittement mentirait sur l'état réel.
        if ingest:
            for short_id, row in abrogated.items():
                if short_id in current or short_id in details:
                    continue
                details[short_id] = {"statut": STATUT_SUPPRIME, "nb_chunks": 0}
                already = str(row.fields.get("statut_ingestion") or "").strip().lower()
                if writeback_enabled and already != STATUT_SUPPRIME:
                    self._writeback(row.record_id, statut=STATUT_SUPPRIME, nb_chunks=0)

        finished_at = utc_now_iso()
        summary = {
            "run_id": run_id,
            "ministere": MINISTERE,
            "target_env": self.config.target_env,
            "started_at": started_at,
            "finished_at": finished_at,
            "ocr_provider": f"{self.ocr_provider.name}/{self.ocr_provider.version}",
            "expected_count": len(expected),
            "ingested_count": len(ingested),
            "skipped_count": len(skipped),
            "failed_count": len(failures),
            "deleted_count": len(deleted),
            "rejected_count": len(manifest.rejected),
            "details": details,
        }
        if ingest:
            self.db_writer.insert_ingestion_run(summary)
        return summary

    def _writeback(
        self,
        record_id: int,
        *,
        statut: str,
        nb_chunks: Optional[int] = None,
        hash_contenu: str = "",
        erreur: str = "",
    ) -> None:
        fields: dict[str, Any] = {
            "statut_ingestion": statut,
            "derniere_ingestion": utc_now_iso(),
            "erreur_ingestion": erreur,
        }
        if nb_chunks is not None:
            fields["nb_chunks"] = nb_chunks
        if hash_contenu:
            fields["hash_contenu"] = hash_contenu
        try:
            self.grist.writeback_status(record_id, fields)
        except Exception as exc:  # noqa: BLE001 — le writeback ne doit pas faire échouer l'ingestion
            print(f"[warn] writeback Grist échoué pour record {record_id}: {exc}")

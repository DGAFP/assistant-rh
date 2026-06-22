from __future__ import annotations

import json
from pathlib import Path

import pytest
from assistant_rh_data_engineering.legifrance import LegifrancePipeline, LegifrancePipelineConfig
from assistant_rh_data_engineering.legifrance.config import LakePaths
from assistant_rh_data_engineering.legifrance.db import LegifranceDbWriter


def make_pipeline(tmp_path: Path) -> LegifrancePipeline:
    config = LegifrancePipelineConfig(paths=LakePaths(root_dir=tmp_path / "lake"))
    config.embeddings.enable_m3 = False
    config.embeddings.enable_bge_scaleway = False
    config.gold.export_parquet = False
    config.gold.export_npy = False
    return LegifrancePipeline(config)


def test_local_raw_pipeline_projects_articles_to_dgafp_and_legacy_texts_to_legifrance(tmp_path: Path) -> None:
    pipeline = make_pipeline(tmp_path)

    article_payload = {
        "asset_type": "article",
        "article_id": "LEGIARTI000050546231",
        "cid": "LEGIARTI000050546231",
        "num_article": "R115-3",
        "num_norm": "R115-3",
        "title": "Code général de la fonction publique",
        "full_title": "Code général de la fonction publique",
        "subtitles": "PARTIE RÉGLEMENTAIRE > Livre Ier",
        "full_sections_title": "PARTIE RÉGLEMENTAIRE > Livre Ier",
        "text": "La communication des informations intervient au plus tard dans un délai de sept jours.",
        "source_url": "https://www.legifrance.gouv.fr/codes/article_lc/LEGIARTI000050546231/",
        "category": "CODE",
        "status": "VIGUEUR",
        "source_name": "LEGI",
        "section_parent_cid": "LEGISCTA000050546227",
        "section_parent_titre": "Section 2",
        "code_id": "LEGITEXT000044416551",
        "state": "vigueur",
        "date_version": "2025-02-01",
        "start_date": "2025-02-01",
        "end_date": "2999-01-01",
        "short_id": "R115-3",
    }
    article_path = pipeline.bronze_repo.articles_dir / "R115-3.json"
    article_path.write_text(json.dumps(article_payload, ensure_ascii=False, indent=2), encoding="utf-8")

    txt_path = pipeline.bronze_repo.legacy_text_sources_dir / "decret-test.txt"
    txt_path.write_text(
        "# Décret test\n\nArticle 1\n\nLe droit au repos est garanti.\n",
        encoding="utf-8",
    )

    bronze_assets = pipeline.run_bronze()
    silver_bundles = pipeline.run_silver(bronze_assets)
    gold_bundles = pipeline.run_gold(silver_bundles)
    gold_chunks = [chunk for bundle in gold_bundles for chunk in bundle.chunks]

    assert len(bronze_assets) == 2
    assert len(silver_bundles) == 2
    assert len(gold_chunks) >= 2

    article_chunk = next(chunk for chunk in gold_chunks if chunk.get("number") == "R115-3")
    assert article_chunk["chunk_id"] == "LEGIARTI000050546231_0"
    assert article_chunk["hash_id"] == "LEGIARTI000050546231_0"
    assert article_chunk["text"] == article_payload["text"]
    assert "Article R115-3" in article_chunk["chunk_text"]

    legacy_rows = LegifranceDbWriter.project_legacy_chunks(gold_chunks)
    modern_rows = LegifranceDbWriter.project_modern_chunks(gold_chunks)
    assert len(legacy_rows) == 1
    assert len(modern_rows) >= 1
    assert legacy_rows[0]["chunk_id"] == "LEGIARTI000050546231_0"
    assert all(row.get("number") is None for row in modern_rows)


def test_legacy_texts_use_legal_chunking_and_drop_export_residue(tmp_path: Path) -> None:
    pipeline = make_pipeline(tmp_path)

    txt_path = pipeline.bronze_repo.legacy_text_sources_dir / "decret-legacy.txt"
    txt_path.write_text(
        "\n".join(
            [
                "Décret n° 86-83 du 17 janvier 1986 relatif aux dispositions générales a... https://www.legifrance.gouv.fr/loda/id/JORFTEXT000000699956",
                "6 sur 45 09/10/2025, 16:01",
                "[PAGE 7]",
                "Titre Ier bis : Dispositions propres au contrat de projet (Articles 2-4 à 2-12)",
                "Modifié par Décret n°2024-1038 du 6 novembre 2024 - art. 10",
                "Article 2-2 (abrogé)",
                "Le contrat de projet est établi par écrit.",
                "755-10 du code de la sécurité sociale.",
                "",
                "Article 2-3",
                "Lorsque le contrat de projet a été conclu pour une durée inférieure à six ans, il peut être renouvelé.",
                "Décret n° 86-83 du 17 janvier 1986 relatif aux dispositions générales a... https://www.legifrance.gouv.fr/loda/id/JORFTEXT000000699956",
                "7 sur 45 09/10/2025, 16:01",
            ]
        ),
        encoding="utf-8",
    )

    bronze_assets = pipeline.run_bronze()
    silver_bundles = pipeline.run_silver(bronze_assets)
    gold_bundles = pipeline.run_gold(silver_bundles)
    gold_chunks = [chunk for bundle in gold_bundles for chunk in bundle.chunks]
    modern_rows = LegifranceDbWriter.project_modern_chunks(gold_chunks)

    assert len(bronze_assets) == 1
    assert [row["role"] for row in modern_rows] == ["LEGAL_ARTICLE", "LEGAL_ARTICLE"]
    assert all(row["role"] != "Q_ONLY" for row in modern_rows)
    assert all("755-10 du code de la sécurité sociale" not in row["section_path"] for row in modern_rows)
    assert modern_rows[0]["section_path"].endswith(
        "Titre Ier bis : Dispositions propres au contrat de projet (Articles 2-4 à 2-12) > Article 2-2 (abrogé)"
    )
    assert modern_rows[1]["section_path"].endswith("Titre Ier bis : Dispositions propres au contrat de projet (Articles 2-4 à 2-12) > Article 2-3")

    combined_text = "\n".join(row["chunk_text"] for row in modern_rows)
    assert "[PAGE" not in combined_text
    assert "sur 45" not in combined_text
    assert "legifrance.gouv.fr" not in combined_text
    assert "755-10 du code de la sécurité sociale." in combined_text


def test_local_raw_pipeline_loads_articles_from_xml_dump(tmp_path: Path) -> None:
    pipeline = make_pipeline(tmp_path)
    xml_dir = pipeline.bronze_repo.bulk_articles_dir / "Freemium_legi_global_20250713-140000"
    xml_dir.mkdir(parents=True, exist_ok=True)
    xml_path = xml_dir / "LEGIARTI000050546231.xml"
    xml_path.write_text(
        """
        <ARTICLE>
          <META>
            <META_COMMUN>
              <ID>LEGIARTI000050546231</ID>
            </META_COMMUN>
            <META_SPEC>
              <META_ARTICLE>
                <NUM>R115-3</NUM>
                <ETAT>VIGUEUR</ETAT>
                <DATE_DEBUT>2025-02-01</DATE_DEBUT>
                <DATE_FIN>2999-01-01</DATE_FIN>
              </META_ARTICLE>
            </META_SPEC>
          </META>
          <BLOC_TEXTUEL>
            <CONTENU>
              <P>La communication des informations intervient au plus tard dans un délai de sept jours.</P>
            </CONTENU>
          </BLOC_TEXTUEL>
          <CONTEXTE>
            <TEXTE nature="CODE">
              <TITRE_TXT id_txt="LEGITEXT000044416551">Code général de la fonction publique</TITRE_TXT>
              <TITRE_TM id="LEGISCTA000050546227">Section 2</TITRE_TM>
            </TEXTE>
          </CONTEXTE>
        </ARTICLE>
        """,
        encoding="utf-8",
    )

    bronze_assets = pipeline.run_bronze()

    assert len(bronze_assets) == 1
    assert bronze_assets[0].payload["num_article"] == "R115-3"
    assert (pipeline.bronze_repo.articles_dir / "LEGIARTI000050546231.json").exists()


def test_non_code_articles_use_loda_urls_and_historical_chunk_format(tmp_path: Path) -> None:
    pipeline = make_pipeline(tmp_path)
    xml_dir = pipeline.bronze_repo.bulk_articles_dir / "Freemium_legi_global_20250713-140000"
    xml_dir.mkdir(parents=True, exist_ok=True)
    xml_path = xml_dir / "LEGIARTI000006207978.xml"
    xml_path.write_text(
        (
            """
        <ARTICLE>
          <META>
            <META_COMMUN>
              <ID>LEGIARTI000006207978</ID>
            </META_COMMUN>
            <META_SPEC>
              <META_ARTICLE>
                <NUM>1</NUM>
                <ETAT>VIGUEUR</ETAT>
                <DATE_DEBUT>2002-02-07</DATE_DEBUT>
                <DATE_FIN>2999-01-01</DATE_FIN>
              </META_ARTICLE>
            </META_SPEC>
          </META>
          <BLOC_TEXTUEL>
            <CONTENU>
              <P>Les services du ministère peuvent déroger aux garanties minimales.</P>
            </CONTENU>
          </BLOC_TEXTUEL>
          <LIENS>
            <LIEN
              id="JORFTEXT000000208382"
              cidtexte="JORFTEXT000000208382"
              naturetexte="DECRET"
              numtexte="2000-815"
              sens="source"
              typelien="TXT_SOURCE"
            >Décret 2000-815 2000-08-25</LIEN>
            <LIEN
              id="LEGIARTI000006566084"
              cidtexte="JORFTEXT000000208382"
              naturetexte="DECRET"
              num="3"
              numtexte="2000-815"
              sens="source"
              typelien="CITATION"
            >Décret n°2000-815 du 25 août 2000 - art. 3 (M)</LIEN>
          </LIENS>
          <CONTEXTE>
            <TEXTE nature="DECRET" ministere="MINISTERE DE L'AMENAGEMENT DU TERRITOIRE ET DE L'ENVIRONNEMENT">
              <TITRE_TXT
                c_titre_court="Décret n°2002-141"
                debut="2002-02-07"
                fin="2999-01-01"
                id_txt="LEGITEXT000005632791"
              >"""
            "Décret n°2002-141 du 4 février 2002 portant dérogations aux garanties "
            "minimales de la durée du travail et de repos applicables à certains agents."
            """</TITRE_TXT>
            </TEXTE>
          </CONTEXTE>
        </ARTICLE>
        """
        ),
        encoding="utf-8",
    )

    bronze_assets = pipeline.run_bronze()
    silver_bundles = pipeline.run_silver(bronze_assets)
    gold_bundles = pipeline.run_gold(silver_bundles)
    gold_chunks = [chunk for bundle in gold_bundles for chunk in bundle.chunks]
    legacy_rows = LegifranceDbWriter.project_legacy_chunks(gold_chunks)

    assert bronze_assets[0].payload["source_url"] == "https://www.legifrance.gouv.fr/loda/article_lc/LEGIARTI000006207978"
    assert legacy_rows[0]["url"] == "https://www.legifrance.gouv.fr/loda/article_lc/LEGIARTI000006207978"
    assert legacy_rows[0]["title"] == "Décret n°2002-141"
    assert legacy_rows[0]["full_title"].startswith("Décret n°2002-141 du 4 février 2002")
    assert legacy_rows[0]["text"] == "Les services du ministère peuvent déroger aux garanties minimales."
    assert legacy_rows[0]["chunk_text"].startswith("Décret n°2002-141 du 4 février 2002 portant dérogations")
    assert "\nArticle 1\nStatut: VIGUEUR\n\nLes services du ministère" in legacy_rows[0]["chunk_text"]
    assert legacy_rows[0]["end_date"] is None
    assert legacy_rows[0]["ministry"] is None
    assert legacy_rows[0]["lien_citations_count"] == 1
    assert legacy_rows[0]["lien_citations"][0]["linkType"] == "CITATION"


def test_bulk_full_run_snapshot_splits_targets_as_expected() -> None:
    lake_root = Path("data/lake/legifrance_bulk_full_run")
    if not lake_root.exists():
        pytest.skip("Snapshot legifrance_bulk_full_run absent.")

    config = LegifrancePipelineConfig(paths=LakePaths(root_dir=lake_root))
    config.embeddings.enable_m3 = False
    config.embeddings.enable_bge_scaleway = False
    config.gold.export_parquet = False
    config.gold.export_npy = False
    pipeline = LegifrancePipeline(config)

    bronze_assets = pipeline.run_bronze()
    silver_bundles = pipeline.run_silver(bronze_assets)
    gold_bundles = pipeline.run_gold(silver_bundles)
    gold_chunks = [chunk for bundle in gold_bundles for chunk in bundle.chunks]

    legacy_rows = LegifranceDbWriter.project_legacy_chunks(gold_chunks)
    modern_rows = LegifranceDbWriter.project_modern_chunks(gold_chunks)

    assert len(legacy_rows) == 3992
    assert len(modern_rows) == 429

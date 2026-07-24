"""Pills de citation Légifrance (issue #350).

La route ``article_lc`` n'accepte que des ids de VERSION : le matcher doit
préférer l'URL stockée en base (``rag_chunks_dgafp.url``, injectée sur les refs
par ``context_builder._enrich_refs_with_cid``) et ne retomber sur la
construction ``codes/article_lc/{cid}`` qu'en l'absence d'URL stockée.
"""

from __future__ import annotations

from assistant_rh_rag_pipeline.citation_extractor import match_refs_with_response_v3

CID = "LEGIARTI000044423597"
VERSION_URL = "https://www.legifrance.gouv.fr/codes/article_lc/LEGIARTI000046874572"


def test_match_prefers_stored_url() -> None:
    refs = [
        {
            "number": "L621-10",
            "cid": CID,
            "title": "Code général de la fonction publique",
            "url": VERSION_URL,
        }
    ]

    matched = match_refs_with_response_v3("L'article L621-10 du CGFP institue la journée de solidarité.", refs)

    assert len(matched) == 1
    assert matched[0].cid == CID
    assert matched[0].url == VERSION_URL


def test_match_falls_back_to_cid_url_when_no_stored_url() -> None:
    for ref in ({"number": "L621-10", "cid": CID, "title": "CGFP"}, {"number": "L621-10", "cid": CID, "title": "CGFP", "url": ""}):
        matched = match_refs_with_response_v3("Voir l'article L621-10.", [ref])

        assert len(matched) == 1
        assert matched[0].url == f"https://www.legifrance.gouv.fr/codes/article_lc/{CID}"


def test_decree_pill_still_uses_known_decree_url() -> None:
    matched = match_refs_with_response_v3("Le décret n° 86-83 s'applique aux agents contractuels.", [])

    assert len(matched) == 1
    assert matched[0].is_decree
    assert matched[0].url == "https://www.legifrance.gouv.fr/loda/id/JORFTEXT000000699956/"

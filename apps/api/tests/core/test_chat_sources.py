from dataclasses import replace

from assistant_rh_api.core.chat import SOURCES_MARKER, final_sources, with_sources
from assistant_rh_api.core.models.context import ContextItem


def item(**changes):
    return replace(
        ContextItem(
            "s", "Section", "Content", 0.8, "MATTE", "Guide", "https://storage.invalid/file?signed=secret", metadata={"doc_short_id": "guide"}
        ),
        **changes,
    )


def test_sources_are_ordered_final_documents_and_never_internal_capability_urls():
    internal = item()
    public = item(
        document_title="Fiche publique",
        document_url="https://www.service-public.gouv.fr/particuliers/vosdroits/F1",
        publisher="Service-Public",
        metadata={"doc_short_id": "fiche"},
    )
    sources = final_sources((internal, public, internal))
    assert [s.doc_ref for s in sources] == ["guide", "fiche"]
    assert [s.access for s in sources] == ["authenticated", "public"]
    assert sources[0].url == "" and sources[1].url == public.document_url
    content = with_sources("Réponse", sources)
    assert content == "Réponse" + SOURCES_MARKER + (
        "1. Guide — MATTE\n2. [Fiche publique](https://www.service-public.gouv.fr/particuliers/vosdroits/F1) — Service-Public"
    )
    assert with_sources("Refus", ()) == "Refus"


def test_full_document_and_section_use_same_canonical_reference():
    document_id = "00000000-0000-0000-0000-000000000001"
    sources = final_sources(
        (item(metadata={"doc_id": document_id, "doc_short_id": "short"}), item(section_id=None, metadata={"doc_id": document_id}))
    )
    assert len(sources) == 1 and sources[0].doc_ref == document_id and sources[0].document_id == document_id


def test_source_metadata_cannot_inject_markdown_links_or_signed_public_urls():
    sources = final_sources(
        (item(document_title="[evil](javascript:alert)\n<script>", document_url="https://www.service-public.fr/path?signature=secret"),)
    )
    markdown = with_sources("Réponse", sources)
    assert sources[0].url == "" and "<script>" not in markdown and "[evil](" not in markdown


def test_unknown_source_reference_is_stable_and_nonempty():
    unknown = item(metadata={})
    assert final_sources((unknown,))[0].doc_ref == final_sources((unknown,))[0].doc_ref
    assert final_sources((unknown,))[0].doc_ref.startswith("source-")

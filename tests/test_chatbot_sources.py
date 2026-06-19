from __future__ import annotations

from assistant_rh_rag_pipeline.models import Chunk, ContextItem

from src.ui.chatbot_sources import context_items_to_v1_chunks


def test_context_items_to_v1_chunks_preserves_service_public_doc_short_id_as_sid() -> None:
    items = [
        ContextItem(
            section_id=None,
            heading="Section F1",
            content="Texte F1",
            score=0.9,
            publisher="Service-Public",
            document_title="Document F1",
            document_url="https://www.service-public.gouv.fr/particuliers/vosdroits/F1",
            metadata={
                "doc_short_id": "F1",
                "doc_url": "https://www.service-public.gouv.fr/particuliers/vosdroits/F1",
            },
        ),
        ContextItem(
            section_id=None,
            heading="Section F2",
            content="Texte F2",
            score=0.8,
            publisher="Service-Public",
            document_title="Document F2",
            document_url="https://www.service-public.gouv.fr/particuliers/vosdroits/F2",
            metadata={
                "doc_short_id": "F2",
                "doc_url": "https://www.service-public.gouv.fr/particuliers/vosdroits/F2",
            },
        ),
    ]

    chunks = context_items_to_v1_chunks(items, Chunk)

    assert [chunk.metadata["sid"] for chunk in chunks] == ["F1", "F2"]

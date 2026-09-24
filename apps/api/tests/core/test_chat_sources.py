from dataclasses import replace

import pytest
from assistant_rh_api.core.models.context import ContextItem
from assistant_rh_api.core.sources import SOURCES_MARKER, final_sources, with_sources


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


@pytest.mark.parametrize(
    "url",
    [
        "https://storage.invalid/PRIVATE_CAPABILITY?X-Amz-Signature=token",
        "https://www.service-public.fr/F1?signature=PRIVATE_CAPABILITY",
        "https://www.legifrance.gouv.fr/path#PRIVATE_CAPABILITY",
        "https://user:PRIVATE_CAPABILITY@www.service-public.fr/F1",
        "http://internal/PRIVATE_CAPABILITY",
        "s3://private/PRIVATE_CAPABILITY",
        "//storage.invalid/PRIVATE_CAPABILITY",
        "storage.invalid/file?X-Amz-Signature=PRIVATE_CAPABILITY",
        "storage.invalid:8443/file?signature=PRIVATE_CAPABILITY",
        "127.0.0.1/private/PRIVATE_CAPABILITY",
        "https://[::1]/PRIVATE_CAPABILITY",
        "https://storage.invalid/file(PRIVATE_CAPABILITY).pdf",
        "https://storage.invalid/file((PRIVATE_CAPABILITY)).pdf",
    ],
)
@pytest.mark.parametrize("template", ["Voir {}.", "[guide]({})", "<{}>", '<a href="{}">guide</a>', "[guide]: {}", "`{}`"])
@pytest.mark.parametrize("has_sources", [False, True])
def test_generated_links_follow_the_same_allowlist_even_without_sources(url, template, has_sources):
    answer = with_sources(template.format(url), final_sources((item(),)) if has_sources else ())
    assert "PRIVATE_CAPABILITY" not in answer
    assert "lien privé retiré" in answer


@pytest.mark.parametrize(
    "url",
    [
        "https://www.service-public.gouv.fr/particuliers/vosdroits/F1",
        "https://www.legifrance.gouv.fr/codes/article_lc/LEGIARTI123",
        "https://www.service-public.fr/F1(foo(bar))",
    ],
)
def test_generated_public_links_and_surrounding_text_are_preserved(url):
    answer = f"Voir [la référence]({url}), puis <{url}> ou {url}."
    assert with_sources(answer, ()) == answer


def test_adjacent_private_link_cannot_borrow_a_public_hostname():
    answer = "[public](https://www.service-public.fr/F1),[interne](https://storage.invalid/PRIVATE_CAPABILITY)"
    assert "PRIVATE_CAPABILITY" not in with_sources(answer, ())


@pytest.mark.parametrize("separator", ["", ",", "; "])
@pytest.mark.parametrize("private_index", [None, 0, 1])
def test_adjacent_links_are_redacted_individually(separator, private_index):
    urls = ["https://www.service-public.fr/F1", "https://www.legifrance.gouv.fr/codes/article_lc/LEGIARTI123"]
    expected_urls = urls.copy()
    if private_index is not None:
        urls[private_index] = "storage.invalid/file?X-Amz-Signature=PRIVATE_CAPABILITY"
        expected_urls[private_index] = "[lien privé retiré]"
    answer = separator.join(f"[{index}]({url})" for index, url in enumerate(urls))
    expected = separator.join(f"[{index}]({url})" for index, url in enumerate(expected_urls))
    assert with_sources(answer, ()) == expected


def test_filenames_versions_and_normal_prose_are_not_links():
    answer = "Lire config.json et guide.pdf avec Python 3.12. Le délai est de 2.5 jours."
    assert with_sources(answer, ()) == answer

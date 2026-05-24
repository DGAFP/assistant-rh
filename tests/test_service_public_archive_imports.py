from assistant_rh_data_engineering.service_public import section_splitter, silver


def test_service_public_silver_uses_packaged_parser_helpers() -> None:
    assert silver.parse_fiche_xml_from_bytes.__module__ == (
        "assistant_rh_data_engineering.service_public.xml_parser"
    )
    assert silver.split_document_into_sections.__module__ == (
        "assistant_rh_data_engineering.service_public.section_splitter"
    )


def test_section_splitter_uses_packaged_legal_reference_parser() -> None:
    assert section_splitter.LEGAL_REFS_AVAILABLE is True
    assert section_splitter.extract_and_parse_refs.__module__ == (
        "assistant_rh_data_engineering.service_public.legal_refs"
    )



def test_xml_parser_uses_call_local_definitions() -> None:
    import xml.etree.ElementTree as ET

    element = ET.fromstring('<Texte><LienIntra LienID="D1">Terme</LienIntra></Texte>')

    assert "première définition" in silver.parse_fiche_xml_from_bytes.__globals__[
        "extract_text_markdown"
    ](element, definitions={"D1": "première définition"})
    assert "seconde définition" in silver.parse_fiche_xml_from_bytes.__globals__[
        "extract_text_markdown"
    ](element, definitions={"D1": "seconde définition"})


def test_legal_ref_to_dict_preserves_false_values() -> None:
    from assistant_rh_data_engineering.service_public.legal_refs import LegalRef

    assert LegalRef(raw_text="L. 332-2", resolved=False).to_dict()["resolved"] is False


def test_figure_block_replacement_uses_exact_offsets() -> None:
    content = "duplicate <!-- FIGURE_TEXT: fig_1 -->real<!-- /FIGURE_TEXT: fig_1 --> duplicate"
    start = len("duplicate ")
    end = start + len("<!-- FIGURE_TEXT: fig_1 -->real<!-- /FIGURE_TEXT: fig_1 -->")

    result, figure_ids = section_splitter._remove_figure_blocks_from_content(
        content,
        [{"figure_id": "fig_1", "char_start": start, "char_end": end}],
        0,
        len(content),
    )

    assert figure_ids == ["fig_1"]
    assert result == "duplicate <!-- FIGURE_REF: fig_1 --> duplicate"

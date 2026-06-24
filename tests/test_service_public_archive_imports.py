from assistant_rh_data_engineering.service_public import section_splitter, silver


def test_service_public_silver_uses_packaged_parser_helpers() -> None:
    assert silver.parse_fiche_xml_from_bytes.__module__ == ("assistant_rh_data_engineering.service_public.xml_parser")
    assert silver.split_document_into_sections.__module__ == ("assistant_rh_data_engineering.service_public.section_splitter")


def test_section_splitter_uses_packaged_legal_reference_parser() -> None:
    assert section_splitter.LEGAL_REFS_AVAILABLE is True
    assert section_splitter.extract_and_parse_refs.__module__ == ("assistant_rh_data_engineering.service_public.legal_refs")


def test_xml_parser_preserves_multiple_introduction_blocks() -> None:
    xml_content = """<?xml version="1.0" encoding="UTF-8"?>
<Fiche spUrl="https://www.service-public.fr/particuliers/vosdroits/F32513"
       xmlns:dc="http://purl.org/dc/elements/1.1/">
  <dc:title>Supplément familial de traitement (SFT) dans la fonction publique</dc:title>
  <dc:type>Fiche pratique</dc:type>
  <dc:date>modified 2026-05-22</dc:date>
  <Theme>
    <Titre>Travail - Formation</Titre>
  </Theme>
  <Introduction>
    <Texte>
      <Paragraphe>Vous êtes fonctionnaire ou contractuel et vous avez un ou plusieurs enfants à charge ?</Paragraphe>
    </Texte>
    <Texte>
      <Paragraphe>Le SFT est versé à l'agent public qui a au moins 1 enfant de moins de 20 ans à charge.</Paragraphe>
    </Texte>
  </Introduction>
  <Texte>
    <Chapitre>
      <Titre>2 parents agents publics</Titre>
      <Texte>
        <Paragraphe>Un seul des parents peut percevoir le SFT.</Paragraphe>
      </Texte>
    </Chapitre>
  </Texte>
</Fiche>
""".encode("utf-8")

    parsed = silver.parse_fiche_xml_from_bytes(xml_content, "F32513")

    assert parsed is not None
    doc_markdown = parsed["doc_markdown"]
    introduction = parsed["metadata"]["introduction"]
    first_intro = "Vous êtes fonctionnaire ou contractuel"
    sft_intro = "au moins 1 enfant de moins de 20 ans à charge"

    assert first_intro in doc_markdown
    assert sft_intro in doc_markdown
    assert first_intro in introduction
    assert sft_intro in introduction
    assert doc_markdown.index(first_intro) < doc_markdown.index(sft_intro)
    assert doc_markdown.index(sft_intro) < doc_markdown.index("## 2 parents agents publics")


def test_xml_parser_preserves_root_preamble_before_situations() -> None:
    xml_content = """<?xml version="1.0" encoding="UTF-8"?>
<Fiche spUrl="https://www.service-public.fr/particuliers/vosdroits/F32513"
       xmlns:dc="http://purl.org/dc/elements/1.1/">
  <dc:title>Supplément familial de traitement (SFT) dans la fonction publique</dc:title>
  <dc:type>Fiche pratique</dc:type>
  <dc:date>modified 2026-05-22</dc:date>
  <Theme>
    <Titre>Travail - Formation</Titre>
  </Theme>
  <Introduction>
    <Texte>
      <Paragraphe>Vous êtes fonctionnaire ou contractuel et vous avez un ou plusieurs enfants à charge ?</Paragraphe>
    </Texte>
  </Introduction>
  <Texte>
    <Paragraphe>
      Le SFT est un complément de rémunération versé à tout agent public qui a
      <MiseEnEvidence>au moins 1\u00a0enfant de moins de 20\u00a0ans à charge</MiseEnEvidence>.
    </Paragraphe>
    <Paragraphe>Les conditions d'attribution du SFT varient selon la situation des parents.</Paragraphe>
  </Texte>
  <ListeSituations>
    <Situation>
      <Titre>2 parents agents publics</Titre>
      <Texte>
        <Chapitre>
          <Titre>Quel parent perçoit le supplément familial de traitement ?</Titre>
          <Paragraphe>Un seul des parents peut percevoir le SFT.</Paragraphe>
        </Chapitre>
      </Texte>
    </Situation>
  </ListeSituations>
</Fiche>
""".encode("utf-8")

    parsed = silver.parse_fiche_xml_from_bytes(xml_content, "F32513")

    assert parsed is not None
    doc_markdown = parsed["doc_markdown"]
    introduction = parsed["metadata"]["introduction"]
    first_intro = "Vous êtes fonctionnaire ou contractuel"
    sft_intro = "au moins 1 enfant de moins de 20 ans à charge"

    assert sft_intro in doc_markdown
    assert sft_intro in introduction
    assert doc_markdown.index(first_intro) < doc_markdown.index(sft_intro)
    assert doc_markdown.index(sft_intro) < doc_markdown.index("## 2 parents agents publics")


def test_xml_parser_uses_call_local_definitions() -> None:
    import xml.etree.ElementTree as ET

    element = ET.fromstring('<Texte><LienIntra LienID="D1">Terme</LienIntra></Texte>')

    assert "première définition" in silver.parse_fiche_xml_from_bytes.__globals__["extract_text_markdown"](
        element, definitions={"D1": "première définition"}
    )
    assert "seconde définition" in silver.parse_fiche_xml_from_bytes.__globals__["extract_text_markdown"](
        element, definitions={"D1": "seconde définition"}
    )


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

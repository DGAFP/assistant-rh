"""Tests du générateur de résumés d'article R2 (utils/article_summary.py).

Principe testé partout : « le résumé TROUVE, il ne DIT jamais » — le module ne
produit que des unités d'INDEX ; tout résumé qui invente une valeur numérique
absente du texte source est rejeté (l'article reste sans ligne R2), et le
cache versionné garantit la reprise idempotente sans re-paiement LLM.
"""

from __future__ import annotations

from typing import Any

import pytest
from assistant_rh_data_engineering.utils import article_summary as r2

ARTICLE_TEXT = (
    "Code général de la fonction publique\n"
    "PARTIE LÉGISLATIVE > Livre Ier : DROITS, OBLIGATIONS ET PROTECTIONS\n"
    "Article L123-2\n\n"
    "L'agent contractuel ne peut occuper un autre emploi permanent à temps "
    "complet au sein des administrations mentionnées à l'article L2, sauf "
    "dérogation prévue par décret en Conseil d'Etat."
)

GOOD_SUMMARY = (
    "Concerne les agents contractuels de la fonction publique qui souhaitent "
    "cumuler plusieurs emplois publics. Règle le cumul d'un emploi permanent "
    "à temps complet avec un autre emploi public : interdiction de principe, "
    "dérogations possibles. Mots-clés : cumul d'activités, double emploi, "
    "plusieurs contrats publics, temps complet, employeur public."
)


# --- Garde anti-invention de valeurs -----------------------------------------


def test_unsourced_numbers_accepts_numbers_present_in_source() -> None:
    # L123-2 et L2 sont dans la source: un résumé qui les reprend n'invente rien.
    assert r2.unsourced_numbers("Cumul interdit par l'article L123-2 et l'article L2.", ARTICLE_TEXT) == []


def test_unsourced_numbers_flags_invented_values() -> None:
    invented = r2.unsourced_numbers("Le cumul est plafonné à 70 % du temps de travail.", ARTICLE_TEXT)
    assert "70" in invented


def test_unsourced_numbers_tolerates_reformatted_references() -> None:
    # « 86-83 » vs « 86 83 » : mêmes groupes de chiffres, pas un rejet à tort.
    assert r2.unsourced_numbers("décret 86 83", "le décret n° 86-83 du 17 janvier 1986") == []


def test_unsourced_numbers_accepts_french_number_words_in_source() -> None:
    # Cas réel (décret 86-83 art. 14): la source écrit les durées en lettres,
    # un résumé fidèle les rend en chiffres -> pas un rejet à tort.
    source = "pendant un mois dès leur entrée en fonctions ; pendant deux mois après deux ans ; pendant trois mois après trois ans de services."
    assert r2.unsourced_numbers("plein traitement pendant 1, 2 ou 3 mois selon l'ancienneté", source) == []
    # Le sens inverse n'est PAS toléré: un chiffre absent reste une invention.
    assert r2.unsourced_numbers("plafonné à 50 % du traitement", source) == ["50"]


def test_is_acceptable_summary() -> None:
    assert r2.is_acceptable_summary(GOOD_SUMMARY, ARTICLE_TEXT) is True
    assert r2.is_acceptable_summary("", ARTICLE_TEXT) is False
    assert r2.is_acceptable_summary("Trop court.", ARTICLE_TEXT) is False
    assert r2.is_acceptable_summary(GOOD_SUMMARY + " Limité à 3 ans.", ARTICLE_TEXT) is False


# --- Summarizer (Albert mocké) ------------------------------------------------


class _FakeResponse:
    def __init__(self, payload: dict[str, Any], status_code: int = 200):
        self._payload = payload
        self.status_code = status_code
        self.text = ""

    def json(self) -> dict[str, Any]:
        return self._payload


def _chat_payload(content: str, finish_reason: str = "stop") -> dict[str, Any]:
    return {
        "choices": [{"message": {"content": content}, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 400, "completion_tokens": 120},
    }


def _make_summarizer(monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any] | Exception) -> tuple[r2.AlbertArticleSummarizer, list[dict]]:
    monkeypatch.setenv("ALBERT_API_KEY", "test-key")
    summarizer = r2.AlbertArticleSummarizer(model="openweight-medium")
    calls: list[dict] = []

    def fake_post(url: str, **kwargs: Any) -> _FakeResponse:
        calls.append({"url": url, **kwargs})
        if isinstance(payload, Exception):
            raise payload
        return _FakeResponse(payload)

    monkeypatch.setattr(summarizer._session, "post", fake_post)
    return summarizer, calls


def test_summarize_returns_summary_and_token_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    summarizer, calls = _make_summarizer(monkeypatch, _chat_payload(GOOD_SUMMARY))
    result = summarizer.summarize(ARTICLE_TEXT)
    assert result.summary == GOOD_SUMMARY
    assert result.prompt_tokens == 400
    assert result.completion_tokens == 120
    assert result.truncated is False
    # Le générateur ne reçoit QUE le texte authentique (jamais de question).
    body = calls[0]["json"]
    assert body["messages"][1]["content"] == ARTICLE_TEXT
    assert body["temperature"] == 0.0


def test_summarize_marks_truncation(monkeypatch: pytest.MonkeyPatch) -> None:
    summarizer, _ = _make_summarizer(monkeypatch, _chat_payload(GOOD_SUMMARY, finish_reason="length"))
    assert summarizer.summarize(ARTICLE_TEXT).truncated is True


def test_summarizer_http_error_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALBERT_API_KEY", "test-key")
    summarizer = r2.AlbertArticleSummarizer()
    monkeypatch.setattr(summarizer._session, "post", lambda url, **kw: _FakeResponse({}, status_code=429))
    with pytest.raises(r2.ArticleSummaryError):
        summarizer.summarize(ARTICLE_TEXT)


def test_version_depends_on_model_and_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALBERT_API_KEY", "test-key")
    v_medium = r2.AlbertArticleSummarizer(model="openweight-medium").version
    v_large = r2.AlbertArticleSummarizer(model="openweight-large").version
    assert v_medium != v_large  # le modèle entre dans la clé de cache
    assert v_medium.startswith(r2.R2_LOGIC_VERSION + "-")
    monkeypatch.setattr(r2, "SUMMARY_PROMPT", r2.SUMMARY_PROMPT + " Autre consigne.")
    assert r2.AlbertArticleSummarizer(model="openweight-medium").version != v_medium  # le prompt aussi


# --- Cache versionné -----------------------------------------------------------


def test_cache_roundtrip_and_self_healing(tmp_path) -> None:
    cache = r2.ArticleSummaryCache(tmp_path, "albert-article-summary", "r2s1-test-p0")
    checksum = r2.source_checksum(ARTICLE_TEXT)
    assert cache.get("LEGIARTI000044420769", checksum) is None  # miss initial
    path = cache.put("LEGIARTI000044420769", checksum, {"summary": GOOD_SUMMARY, "prompt_tokens": 1})
    hit = cache.get("LEGIARTI000044420769", checksum)
    assert hit is not None and hit["summary"] == GOOD_SUMMARY
    # Un autre checksum (article modifié) est un miss naturel.
    assert cache.get("LEGIARTI000044420769", "0" * 64) is None
    # Auto-guérison: fichier corrompu = miss, jamais une erreur collante.
    path.write_text("{corrompu", encoding="utf-8")
    assert cache.get("LEGIARTI000044420769", checksum) is None


def test_cache_put_checkpoints_persistent_artifact(tmp_path) -> None:
    checkpoints: list[tuple[object, str, str]] = []
    cache = r2.ArticleSummaryCache(
        tmp_path,
        "albert-article-summary",
        "r2s1-test-p0",
        on_put=lambda path, uid, checksum: checkpoints.append((path, uid, checksum)),
    )
    checksum = r2.source_checksum(ARTICLE_TEXT)

    path = cache.put("LEGIARTI000044420769", checksum, {"summary": GOOD_SUMMARY})

    assert checkpoints == [(path, "LEGIARTI000044420769", checksum)]
    assert cache.path_for("LEGIARTI000044420769", checksum) == path


# --- Orchestration du lot -------------------------------------------------------


class _StubSummarizer:
    name = "albert-article-summary"
    version = "r2s1-stub-p0"
    model = "stub"

    def __init__(self, outputs: dict[str, r2.ArticleSummary | Exception], corrections: dict[str, r2.ArticleSummary] | None = None):
        self.outputs = outputs
        self.corrections = corrections or {}
        self.calls: list[str] = []
        self.correction_calls: list[list[str]] = []

    def summarize(self, source_text: str, *, prior_summary: str | None = None, unsourced: list[str] | None = None) -> r2.ArticleSummary:
        if prior_summary is not None:
            self.correction_calls.append(list(unsourced or []))
            return self.corrections.get(source_text, self.outputs[source_text])  # type: ignore[return-value]
        self.calls.append(source_text)
        result = self.outputs[source_text]
        if isinstance(result, Exception):
            raise result
        return result


def test_summarize_articles_statuses(tmp_path) -> None:
    ok_text = ARTICLE_TEXT
    invented_text = "Texte source sans aucun chiffre pertinent pour le résumé fourni ici."
    failed_text = "Texte dont l'appel LLM échoue de façon transitoire (rate-limit)."
    summarizer = _StubSummarizer(
        {
            ok_text: r2.ArticleSummary(summary=GOOD_SUMMARY, prompt_tokens=400, completion_tokens=120),
            invented_text: r2.ArticleSummary(summary="Résumé qui invente une durée de 3 ans et un plafond de 70 %, pourtant absents."),
            failed_text: r2.ArticleSummaryError("429"),
        }
    )
    cache = r2.ArticleSummaryCache(tmp_path, summarizer.name, summarizer.version)
    items = r2.summarize_articles(
        [
            {"uid": "A_OK", "source_text": ok_text},
            {"uid": "A_INVENT", "source_text": invented_text},
            {"uid": "A_FAIL", "source_text": failed_text},
            {"uid": "A_VIDE", "source_text": "   "},
        ],
        summarizer,  # type: ignore[arg-type]
        cache,
        max_workers=2,
    )
    by_uid = {item.uid: item for item in items}
    assert by_uid["A_OK"].status == "ok" and by_uid["A_OK"].summary == GOOD_SUMMARY
    # Valeurs non sourcées: une passe de correction est tentée, puis rejet.
    assert by_uid["A_INVENT"].status == "rejected"
    assert summarizer.correction_calls == [["3", "70"]]
    assert by_uid["A_FAIL"].status == "failed"  # transitoire -> retenté au prochain run
    assert by_uid["A_VIDE"].status == "rejected"
    # Seul le résumé ACCEPTÉ est mis en cache (un rejet doit pouvoir être
    # re-tenté après évolution de la logique, un échec après retour de l'API).
    assert cache.get("A_OK", r2.source_checksum(ok_text)) is not None
    assert cache.get("A_INVENT", r2.source_checksum(invented_text)) is None


def test_summarize_articles_correction_pass_recovers_faithful_summary(tmp_path) -> None:
    # Première proposition chiffrée à tort -> la passe de correction produit un
    # résumé sans valeur non sourcée -> accepté (statut ok), tokens CUMULÉS.
    first = r2.ArticleSummary(summary=GOOD_SUMMARY + " La durée est plafonnée à 36 mois.", prompt_tokens=400, completion_tokens=120)
    corrected = r2.ArticleSummary(summary=GOOD_SUMMARY + " La durée est plafonnée réglementairement.", prompt_tokens=550, completion_tokens=110)
    summarizer = _StubSummarizer({ARTICLE_TEXT: first}, corrections={ARTICLE_TEXT: corrected})
    cache = r2.ArticleSummaryCache(tmp_path, summarizer.name, summarizer.version)

    items = r2.summarize_articles([{"uid": "A", "source_text": ARTICLE_TEXT}], summarizer, cache)  # type: ignore[arg-type]

    assert items[0].status == "ok"
    assert items[0].summary == corrected.summary
    assert items[0].prompt_tokens == 950 and items[0].completion_tokens == 230
    assert summarizer.correction_calls == [["36"]]
    cached = cache.get("A", r2.source_checksum(ARTICLE_TEXT))
    assert cached is not None and cached["summary"] == corrected.summary


def test_summarize_articles_resume_is_idempotent(tmp_path) -> None:
    summarizer = _StubSummarizer({ARTICLE_TEXT: r2.ArticleSummary(summary=GOOD_SUMMARY, prompt_tokens=400, completion_tokens=120)})
    cache = r2.ArticleSummaryCache(tmp_path, summarizer.name, summarizer.version)
    articles = [{"uid": "A_OK", "source_text": ARTICLE_TEXT}]

    first = r2.summarize_articles(articles, summarizer, cache)  # type: ignore[arg-type]
    assert [item.status for item in first] == ["ok"]
    assert len(summarizer.calls) == 1

    second = r2.summarize_articles(articles, summarizer, cache)  # type: ignore[arg-type]
    assert [item.status for item in second] == ["cached"]
    assert second[0].summary == GOOD_SUMMARY
    assert len(summarizer.calls) == 1  # reprise: ZÉRO nouvel appel LLM


def test_truncated_summary_is_rejected(tmp_path) -> None:
    summarizer = _StubSummarizer({ARTICLE_TEXT: r2.ArticleSummary(summary=GOOD_SUMMARY, truncated=True)})
    items = r2.summarize_articles([{"uid": "A", "source_text": ARTICLE_TEXT}], summarizer, None)  # type: ignore[arg-type]
    assert items[0].status == "rejected"


def test_summarizer_keyless_init_for_plan_mode(monkeypatch) -> None:
    """Revue #332, round 2 : le mode plan (name/version pour la clé de
    fraîcheur) doit fonctionner sans ALBERT_API_KEY — la clé n'est exigée
    qu'à la génération."""
    monkeypatch.delenv("ALBERT_API_KEY", raising=False)
    s = r2.AlbertArticleSummarizer(model="m-test")
    assert s.version
    with pytest.raises(r2.ArticleSummaryError):
        s.summarize("texte quelconque")

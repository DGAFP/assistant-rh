"""Résumés d'ARTICLE en langage métier RH — unités d'index ADDITIVES (R2).

Constat (investigation qualité RAG du 17/07/2026, `docs/evals/
revue-strategies-qualite-rag.md` §2.3) : les misses profonds du corpus
juridique sont un fossé de vocabulaire question-métier ↔ texte juridique
(gold aux rangs 58-463 en sémantique). La sonde R2 (résumés générés SANS voir
les questions, rangs simulés) convertit q194 (122→25) et q229 (183→24),
projection 5/7 misses profonds récupérés, contrôle différentiel PASS.

Principe non négociable : **le résumé TROUVE, il ne DIT jamais** — le résumé
ne sert qu'à calculer l'embedding d'index ; le texte servi au générateur de
réponses reste le texte juridique authentique de l'article (chunk_text). Le
garde ``unsourced_numbers`` rejette en plus tout résumé qui introduit une
valeur numérique absente du texte source (aucune valeur normée inventée ne
doit même biaiser la recherche).

Pattern calqué sur la re-passe vision (`page_vision.py`) : version de LOGIQUE
explicite entrant dans la clé de cache, version d'instance dérivée du modèle +
hash du prompt, cache versionné par article+checksum, throttle modeste, issues
par article ok / rejected (déterministe, pas de retry) / failed (transitoire,
retenté au prochain run).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import requests

# Deux appels concurrents maximum : l'API Albert est partagée (même contrainte
# que l'annotation d'images / la re-passe vision, resserrée pour un lot de
# plusieurs milliers d'articles).
MAX_SUMMARY_WORKERS = 2

# Version de la LOGIQUE R2 (contrat du prompt + garde anti-invention +
# politique de rejet). Elle entre dans la clé de cache ET dans le marqueur
# ``index_variant`` des lignes gold : changer le prompt-contrat ou le garde
# change l'ensemble des résumés -> caches et lignes doivent être invalidés.
# À incrémenter à CHAQUE évolution de ces règles.
R2_LOGIC_VERSION = "r2s1"

SUMMARY_PROMPT = (
    "Tu prépares l'INDEX DE RECHERCHE d'un assistant RH de la fonction publique"
    " française. À partir du texte juridique fourni (article de code, décret,"
    " arrêté), rédige un RÉSUMÉ D'INDEXATION en langage métier RH : un paragraphe"
    " compact de 3 à 6 phrases qui maximise les chances qu'une question concrète"
    " d'agent ou de gestionnaire RH retrouve ce texte.\n\n"
    "CONTENU ATTENDU :\n"
    "- QUI est concerné : fonctionnaire titulaire, stagiaire, agent contractuel"
    " (CDD/CDI), employeur public, versant de fonction publique si le texte le"
    " précise.\n"
    "- LE SUJET PRATIQUE : ce que le texte règle concrètement, dit avec les mots"
    " qu'emploierait un gestionnaire RH ou un agent (congé, arrêt maladie,"
    " rémunération, prime, cumul d'activités, temps partiel, mutation,"
    " reclassement, démission, retraite...).\n"
    "- LES MOTS-CLÉS MÉTIER et leurs synonymes courants, y compris les"
    " formulations naïves des questions RH (« garde-t-il son salaire »,"
    " « a-t-il droit à », « qui décide », « quelles conditions »...).\n\n"
    "RÈGLES CRITIQUES :\n"
    "- Ce résumé sert UNIQUEMENT à retrouver le texte ; il ne sera JAMAIS montré"
    " à l'utilisateur ni utilisé pour formuler une réponse.\n"
    "- N'INVENTE AUCUNE valeur normée : n'écris aucun chiffre, durée, taux,"
    " montant, seuil ou date qui ne figure pas déjà dans le texte fourni. En cas"
    " de doute, reste vague (« sous condition d'ancienneté », « dans la limite"
    " d'un plafond réglementaire »).\n"
    "- Ne juge pas, n'interprète pas au-delà du texte, ne complète pas avec des"
    " connaissances extérieures.\n"
    "- Rends UNIQUEMENT le paragraphe, sans préambule, sans titre, sans liste,"
    " sans référence juridique formelle."
)


class ArticleSummaryError(RuntimeError):
    """Erreur d'appel au modèle de résumé d'article."""


# --- Garde anti-invention de valeurs -----------------------------------------

_NUMBER_RE = re.compile(r"\d[\d  ,. /-]*\d|\d")


# Les textes juridiques écrivent souvent les petites valeurs en toutes lettres
# (« pendant trois mois après trois ans de services », décret 86-83 art. 14) ;
# un résumé fidèle qui les rend en chiffres ne doit pas être rejeté à tort
# (2 faux positifs sur les golds au lot pilote du 21/07). Seul le sens
# source->chiffre est toléré : un chiffre du résumé est sourcé si son numéral
# français figure dans la source. NB assumé : « un/une » étant aussi article,
# le chiffre 1 est de fait presque toujours autorisé — l'invention la moins
# risquée du spectre.
_FRENCH_NUMBER_WORDS: dict[str, str] = {
    "un": "1",
    "une": "1",
    "premier": "1",
    "première": "1",
    "deux": "2",
    "trois": "3",
    "quatre": "4",
    "cinq": "5",
    "six": "6",
    "sept": "7",
    "huit": "8",
    "neuf": "9",
    "dix": "10",
    "onze": "11",
    "douze": "12",
    "treize": "13",
    "quatorze": "14",
    "quinze": "15",
    "seize": "16",
    "vingt": "20",
    "trente": "30",
    "quarante": "40",
    "cinquante": "50",
    "soixante": "60",
    "cent": "100",
    "cents": "100",
    "mille": "1000",
}

_WORD_RE = re.compile(r"[a-zà-ÿ]+", re.IGNORECASE)


def _number_tokens(text: str, *, include_number_words: bool = False) -> set[str]:
    """Séquences de chiffres normalisées (séparateurs retirés) d'un texte.

    ``86-83`` et ``86 83`` produisent le même jeu {``86``, ``83``} : on compare
    les GROUPES de chiffres, pas leur ponctuation — un résumé qui reformate une
    référence présente dans la source ne doit pas être rejeté à tort.
    ``include_number_words`` (côté SOURCE uniquement) ajoute les équivalents
    chiffrés des numéraux français en toutes lettres.
    """
    tokens: set[str] = set()
    for match in _NUMBER_RE.findall(text):
        tokens.update(re.findall(r"\d+", match))
    if include_number_words:
        for word in _WORD_RE.findall(text):
            digit = _FRENCH_NUMBER_WORDS.get(word.lower())
            if digit:
                tokens.add(digit)
    return tokens


def unsourced_numbers(summary: str, source_text: str) -> list[str]:
    """Valeurs numériques du résumé ABSENTES du texte source (triées).

    Garde de fidélité R2 : le prompt interdit d'écrire des chiffres non
    présents dans la source ; toute occurrence résiduelle = invention probable
    (durée, taux, montant) -> le résumé doit être rejeté. Filet grossier
    volontaire : le résumé n'est jamais servi, le risque couvert est un *biais
    d'index* (attirer des questions vers une valeur fausse), pas une
    hallucination utilisateur. La source autorise aussi ses numéraux en toutes
    lettres (« trois mois » autorise ``3``) — jamais l'inverse.
    """
    return sorted(_number_tokens(summary) - _number_tokens(source_text, include_number_words=True))


def is_acceptable_summary(summary: str, source_text: str, *, min_chars: int = 80) -> bool:
    """Vrai si le résumé est exploitable comme unité d'index.

    Rejets déterministes (pas de retry) : vide/trop court (le modèle n'a rien
    produit d'indexable) ou valeurs numériques non sourcées (invention).
    """
    cleaned = (summary or "").strip()
    if len(cleaned) < min_chars:
        return False
    return not unsourced_numbers(cleaned, source_text)


# --- Génération LLM -----------------------------------------------------------


@dataclass(frozen=True)
class ArticleSummary:
    """Résumé généré + comptage de tokens (mesure de coût du lot)."""

    summary: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    truncated: bool = False


class AlbertArticleSummarizer:
    """Génère le résumé d'indexation d'un article via /chat/completions Albert.

    name/version entrent dans la clé du cache
    (``article_summaries/{name}/{version}/{cid}/{sha256}.json``) et dans le
    marqueur ``index_variant`` des lignes gold : changer de modèle OU de prompt
    invalide le cache (deux configurations produisent des résumés — donc des
    embeddings — incomparables sous la même clé, même piège que page_vision).
    """

    name = "albert-article-summary"

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        max_tokens: int = 700,
        timeout: int = 120,
    ):
        self.base_url = (base_url or os.getenv("ALBERT_BASE_URL") or "https://albert.api.etalab.gouv.fr/v1").rstrip("/")
        # La clé n'est exigée qu'à la GÉNÉRATION (summarize) : le mode plan du
        # job — lecture seule, name/version suffisent pour la clé de fraîcheur —
        # doit fonctionner sans ALBERT_API_KEY (revue #332, round 2).
        self.api_key = api_key or os.getenv("ALBERT_API_KEY", "")
        self.model = model or os.getenv("ALBERT_R2_SUMMARY_MODEL") or "openweight-medium"
        self.max_tokens = max_tokens
        prompt_hash = hashlib.sha1(SUMMARY_PROMPT.encode("utf-8")).hexdigest()[:8]
        safe_model = re.sub(r"[^0-9A-Za-z._-]+", "-", self.model).strip("-")
        self.version = f"{R2_LOGIC_VERSION}-{safe_model}-p{prompt_hash}"
        self.timeout = timeout
        self._session = requests.Session()

    def summarize(
        self,
        source_text: str,
        *,
        prior_summary: str | None = None,
        unsourced: list[str] | None = None,
    ) -> ArticleSummary:
        """Résumé d'indexation d'un texte d'article (texte juridique SEUL).

        Le générateur ne reçoit QUE le texte authentique (titre/contexte
        inclus dans le chunk_text gold) — jamais de question, jamais de
        connaissance externe : le contrôle différentiel de la sonde n'est
        valide que sous cette hypothèse.

        ``prior_summary``/``unsourced`` : passe de CORRECTION après un rejet du
        garde anti-invention — la première proposition et ses valeurs non
        sourcées sont renvoyées au modèle avec consigne de rester vague (la
        température 0 rend un simple re-appel inutile ; il faut changer
        l'entrée).
        """
        if not self.api_key:
            raise ArticleSummaryError("ALBERT_API_KEY manquant pour la génération de résumés R2.")
        messages: list[dict[str, str]] = [
            {"role": "system", "content": SUMMARY_PROMPT},
            {"role": "user", "content": source_text},
        ]
        if prior_summary and unsourced:
            messages.append({"role": "assistant", "content": prior_summary})
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "Ta proposition contient des valeurs chiffrées ABSENTES du texte fourni : "
                        + ", ".join(unsourced)
                        + ". Réécris le résumé en retirant toute valeur chiffrée qui ne figure pas"
                        " littéralement dans le texte ; reste vague sur ces points"
                        " (« sous condition d'ancienneté », « selon un plafond réglementaire »)."
                    ),
                }
            )
        body = {
            "model": self.model,
            "temperature": 0.0,
            "max_tokens": self.max_tokens,
            "messages": messages,
        }
        url = f"{self.base_url}/chat/completions"
        try:
            response = self._session.post(
                url,
                headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                json=body,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise ArticleSummaryError(f"POST {url} (modèle {self.model}) impossible: {exc}") from exc
        if response.status_code >= 400:
            raise ArticleSummaryError(f"POST {url} -> HTTP {response.status_code}: {response.text[:300]}")
        try:
            payload = response.json()
            choice = payload["choices"][0]
            content = choice["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ArticleSummaryError(f"Réponse résumé inattendue (modèle {self.model})") from exc
        usage = payload.get("usage") or {}
        return ArticleSummary(
            summary=str(content or "").strip(),
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            truncated=str(choice.get("finish_reason") or "").lower() == "length",
        )


# --- Cache versionné ------------------------------------------------------------


def source_checksum(source_text: str) -> str:
    """sha256 du texte source — clé de fraîcheur (delta par checksum)."""
    return hashlib.sha256((source_text or "").encode("utf-8")).hexdigest()


class ArticleSummaryCache:
    """Cache local versionné des résumés, un JSON par article+checksum.

    Layout : ``{root}/article_summaries/{name}/{version}/{cid}/{sha256}.json``
    — mêmes conventions de clés que le cache page_vision du bucket bronze : le
    répertoire se synchronise tel quel vers l'Object Storage
    (``ScalewayObjectStorageSync``), et un changement de version/checksum est
    un miss naturel. Auto-guérison : un fichier illisible est traité comme un
    miss (re-génération), jamais comme une erreur collante.
    """

    def __init__(
        self,
        root: Path | str,
        name: str,
        version: str,
        *,
        on_put: Callable[[Path, str, str], None] | None = None,
    ):
        self.base_dir = Path(root) / "article_summaries" / name / version
        self.on_put = on_put

    def path_for(self, article_uid: str, checksum: str) -> Path:
        """Return the stable local path for an article summary artifact."""
        safe_uid = re.sub(r"[^0-9A-Za-z._-]+", "-", str(article_uid)).strip("-") or "unknown"
        return self.base_dir / safe_uid / f"{checksum}.json"

    def get(self, article_uid: str, checksum: str) -> dict[str, Any] | None:
        path = self.path_for(article_uid, checksum)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError):
            print(f"[warn] cache résumé corrompu, re-génération: {path}")
            return None
        if not isinstance(payload, dict) or not str(payload.get("summary") or "").strip():
            print(f"[warn] cache résumé corrompu (forme inattendue), re-génération: {path}")
            return None
        return payload

    def put(self, article_uid: str, checksum: str, payload: dict[str, Any]) -> Path:
        path = self.path_for(article_uid, checksum)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        if self.on_put is not None:
            self.on_put(path, article_uid, checksum)
        return path


# --- Orchestration d'un lot ------------------------------------------------------


@dataclass(frozen=True)
class SummaryBatchItem:
    """Résultat par article: ``status`` ok | rejected | failed | cached."""

    uid: str
    checksum: str
    status: str
    summary: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reason: str = ""


def summarize_articles(
    articles: list[dict[str, Any]],
    summarizer: AlbertArticleSummarizer,
    cache: ArticleSummaryCache | None = None,
    *,
    max_workers: int = MAX_SUMMARY_WORKERS,
    on_result: Callable[[SummaryBatchItem], None] | None = None,
) -> list[SummaryBatchItem]:
    """Résume un lot d'articles ``[{"uid": ..., "source_text": ...}]``.

    Reprise idempotente : cache-hit -> ``cached`` (zéro appel LLM). Quatre
    issues par article :
    - **ok** : résumé accepté (garde passé), mis en cache ;
    - **cached** : servi depuis le cache versionné ;
    - **rejected** : vide/trop court, tronqué (max_tokens) ou valeurs
      numériques non sourcées -> déterministe, PAS de retry, jamais caché ;
    - **failed** : erreur/rate-limit transitoire -> retenté au prochain run.

    Ne fait jamais échouer le lot : un article sans résumé reste simplement
    sans ligne d'index additionnelle (comportement baseline).
    """

    def _one(article: dict[str, Any]) -> SummaryBatchItem:
        uid = str(article.get("uid") or "")
        source_text = str(article.get("source_text") or "")
        checksum = source_checksum(source_text)
        if not uid or not source_text.strip():
            return SummaryBatchItem(uid=uid, checksum=checksum, status="rejected", reason="source vide")

        if cache is not None:
            hit = cache.get(uid, checksum)
            if hit is not None:
                return SummaryBatchItem(
                    uid=uid,
                    checksum=checksum,
                    status="cached",
                    summary=str(hit.get("summary") or ""),
                    prompt_tokens=int(hit.get("prompt_tokens") or 0),
                    completion_tokens=int(hit.get("completion_tokens") or 0),
                )

        prompt_tokens = 0
        completion_tokens = 0
        try:
            result = summarizer.summarize(source_text)
            prompt_tokens += result.prompt_tokens
            completion_tokens += result.completion_tokens
            invented = unsourced_numbers(result.summary, source_text)
            if invented and not result.truncated:
                # UNE passe de correction : à température 0, seul un changement
                # d'entrée (proposition + valeurs fautives) peut débloquer un
                # résumé fidèle mais chiffré (faux positifs du lot pilote).
                result = summarizer.summarize(source_text, prior_summary=result.summary, unsourced=invented)
                prompt_tokens += result.prompt_tokens
                completion_tokens += result.completion_tokens
                invented = unsourced_numbers(result.summary, source_text)
        except ArticleSummaryError as exc:
            print(f"[warn] résumé {uid} échoué (LLM): {exc}")
            return SummaryBatchItem(uid=uid, checksum=checksum, status="failed", reason=str(exc))

        if result.truncated:
            print(f"[warn] résumé {uid} tronqué (max_tokens) — article sans ligne R2")
            return SummaryBatchItem(uid=uid, checksum=checksum, status="rejected", reason="tronqué")
        if invented:
            print(f"[warn] résumé {uid} rejeté (valeurs non sourcées après correction: {', '.join(invented[:5])})")
            return SummaryBatchItem(uid=uid, checksum=checksum, status="rejected", reason=f"valeurs non sourcées: {invented}")
        if not is_acceptable_summary(result.summary, source_text):
            print(f"[warn] résumé {uid} rejeté (vide/trop court) — article sans ligne R2")
            return SummaryBatchItem(uid=uid, checksum=checksum, status="rejected", reason="vide/trop court")

        if cache is not None:
            cache.put(
                uid,
                checksum,
                {
                    "uid": uid,
                    "checksum": checksum,
                    "summarizer": summarizer.name,
                    "version": summarizer.version,
                    "model": summarizer.model,
                    "summary": result.summary,
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                },
            )
        return SummaryBatchItem(
            uid=uid,
            checksum=checksum,
            status="ok",
            summary=result.summary,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )

    items: list[SummaryBatchItem] = []
    if not articles:
        return items
    with ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(articles)))) as pool:
        for item in pool.map(_one, articles):
            items.append(item)
            if on_result is not None:
                on_result(item)
    return items

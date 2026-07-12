"""Client PISTE (API Légifrance officielle) — énumération des articles d'un code.

Follow-live d'un code (#289 E2.2) : un appel ``consult/legi/tableMatieres`` sur
son ``LEGITEXT`` à une date renvoie **tous ses articles avec leur ETAT**
(VIGUEUR / ABROGE). Léger, autoritaire, répétable — remplace la liste de CIDs
figée (``config/legifrance_article_cids.json``, générée une seule fois).

Creds via l'environnement : ``LEGIFRANCE_CLIENT_ID`` / ``LEGIFRANCE_CLIENT_SECRET``
(+ ``LEGIFRANCE_TOKEN_URL`` / ``LEGIFRANCE_BASE_URL`` optionnels). Le token et les
secrets ne sont jamais journalisés.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

import requests

DEFAULT_TOKEN_URL = "https://oauth.piste.gouv.fr/api/oauth/token"
DEFAULT_BASE_URL = "https://api.piste.gouv.fr/dila/legifrance/lf-engine-app"


@dataclass(frozen=True)
class CodeArticle:
    """Un article d'un texte, agrégé depuis ``tableMatieres``/``lawDecree``.

    ``cid`` = l'identité **stable** retenue pour le corpus : le cid chronique
    LEGIARTI quand la réponse en porte un ; pour les articles dont le ``cid``
    API est un JORFARTI (arrêtés/décrets non re-chroniqués côté LEGI), c'est
    l'``id`` LEGIARTI de la version en vigueur. ``version_id`` = l'identifiant
    LEGIARTI de la version courante. ``alias_ids`` = TOUS les identifiants vus
    pour cet article (toutes versions + cid API) — le corpus historique étant
    keyé par version, ils servent à l'attribution (migration d'identité).

    L'API renvoie UN NŒUD PAR VERSION (revue #307 : le décret 86-83 art. 50 a
    un nœud VIGUEUR et un nœud ABROGE pour le même cid) : ``walk_table_matieres``
    agrège par article avec précédence VIGUEUR — jamais d'écrasement dernier-gagne.
    """

    cid: str  # identité stable (LEGIARTI)
    etat: str  # VIGUEUR | ABROGE | ...
    num: str | None = None  # numéro d'article (L1, R.331-7, ...)
    version_id: str = ""  # LEGIARTI... (version courante)
    alias_ids: tuple[str, ...] = ()  # tous les identifiants vus (versions + cid API)


def walk_table_matieres(payload: dict) -> list[CodeArticle]:
    """Extrait les articles d'une réponse ``tableMatieres``/``lawDecree``, quel
    que soit l'imbriquement, en agrégeant les nœuds PAR ARTICLE (l'API émet un
    nœud par version). Précédence d'état : VIGUEUR gagne sur tout autre état.

    Fonction pure (aucun I/O) — testable sur un fixture.
    """
    # Groupes par clé d'article = cid API (chronique LEGIARTI ou JORFARTI).
    groups: dict[str, list[dict]] = {}

    def _walk(node: object) -> None:
        if isinstance(node, dict):
            node_id = str(node.get("id") or "")
            node_cid = str(node.get("cid") or "")
            if node_id.startswith(("LEGIARTI", "JORFARTI")) or node_cid.startswith(("LEGIARTI", "JORFARTI")):
                key = node_cid or node_id
                groups.setdefault(key, []).append({"id": node_id, "cid": node_cid, "etat": str(node.get("etat") or ""), "num": node.get("num")})
            for value in node.values():
                _walk(value)
        elif isinstance(node, list):
            for value in node:
                _walk(value)

    _walk(payload)

    found: list[CodeArticle] = []
    for key, nodes in groups.items():
        current = next((n for n in nodes if n["etat"].upper() == "VIGUEUR"), nodes[-1])
        aliases = tuple(sorted({ident for n in nodes for ident in (n["id"], n["cid"]) if ident}))
        # Identité stable = le cid API, TOUJOURS (LEGIARTI chronique, ou
        # JORFARTI pour les textes non re-chroniqués côté LEGI — lui aussi
        # stable à travers les versions). Revue #307 bis : un fallback vers
        # l'id LEGIARTI de la version courante recréerait le churn d'identité
        # à chaque modification.
        found.append(
            CodeArticle(
                cid=key,
                etat=current["etat"],
                num=current["num"],
                version_id=current["id"] or key,
                alias_ids=aliases,
            )
        )
    return found


def article_parent_text_uids(payload: dict) -> set[str]:
    """JORFTEXT/LEGITEXT du (des) texte(s) parent(s) d'une réponse ``getArticle``.

    Fonction pure — retourne un set (vide si non résoluble : fail-closed).
    """
    article = payload.get("article") or payload or {}
    parents: set[str] = set()
    for title in article.get("textTitles") or []:
        for key in ("cid", "id"):
            ident = str((title or {}).get(key) or "").strip().upper()
            if ident.startswith(("JORFTEXT", "LEGITEXT")):
                parents.add(ident)
    for title in (article.get("context") or {}).get("titreTxt") or []:
        ident = str((title or {}).get("cid") or "").strip().upper()
        if ident.startswith(("JORFTEXT", "LEGITEXT")):
            parents.add(ident)
    return parents


def articles_en_vigueur(payload: dict) -> list[str]:
    """CIDs des articles ``VIGUEUR`` d'une réponse ``tableMatieres``, dédupliqués + triés."""
    return sorted({article.cid for article in walk_table_matieres(payload) if article.etat.upper() == "VIGUEUR"})


class PisteError(RuntimeError):
    pass


class PisteClient:
    """Client minimal PISTE : OAuth client-credentials + POST ``consult``."""

    def __init__(
        self,
        *,
        client_id: str | None = None,
        client_secret: str | None = None,
        token_url: str | None = None,
        base_url: str | None = None,
        timeout: int = 60,
    ) -> None:
        self.client_id = client_id or os.getenv("LEGIFRANCE_CLIENT_ID", "")
        self.client_secret = client_secret or os.getenv("LEGIFRANCE_CLIENT_SECRET", "")
        self.token_url = token_url or os.getenv("LEGIFRANCE_TOKEN_URL", DEFAULT_TOKEN_URL)
        self.base_url = (base_url or os.getenv("LEGIFRANCE_BASE_URL", DEFAULT_BASE_URL)).rstrip("/")
        self.timeout = timeout
        self._token: str | None = None

    def _headers(self) -> dict[str, str]:
        if self._token is None:
            if not (self.client_id and self.client_secret):
                raise PisteError("Creds PISTE manquants (LEGIFRANCE_CLIENT_ID / LEGIFRANCE_CLIENT_SECRET).")
            resp = requests.post(
                self.token_url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "scope": "openid",
                },
                timeout=self.timeout,
            )
            resp.raise_for_status()
            self._token = resp.json()["access_token"]
        return {"Authorization": f"Bearer {self._token}", "accept": "application/json", "Content-Type": "application/json"}

    def consult(self, path: str, payload: dict) -> dict:
        resp = requests.post(
            f"{self.base_url}/consult/{path.lstrip('/')}",
            headers=self._headers(),
            data=json.dumps(payload),
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return resp.json()

    def table_matieres(self, legitext: str, date_millis: int, *, nature: str = "CODE") -> dict:
        """Structure d'un code (LEGITEXT) à une date (epoch millis)."""
        return self.consult("legi/tableMatieres", {"textId": legitext, "date": date_millis, "sctId": "", "nature": nature})

    def get_article(self, article_id: str) -> dict:
        """Un article par son identifiant LEGIARTI (version ou chronique).

        La réponse porte l'**ownership vérifiable** : ``article.cid`` (chronique
        de l'article) et ``textTitles[].cid`` (JORFTEXT/LEGITEXT du texte
        parent) — sert à attribuer les anciennes versions du corpus que les
        TOCs ne listent plus.
        """
        return self.consult("getArticle", {"id": article_id})

    def law_decree(self, jorftext: str, date_millis: int) -> dict:
        """Contenu/structure d'un texte LODA (loi, décret, arrêté) par son
        JORFTEXT à une date — ses articles avec cid chronique + version + ETAT."""
        return self.consult("lawDecree", {"textId": jorftext, "date": date_millis, "searchedString": ""})

    def text_articles(self, text_uid: str, date_millis: int, *, kind: str = "code") -> list[CodeArticle]:
        """Articles (TOC follow-live) d'un texte suivi : ``kind='code'`` →
        ``legi/tableMatieres(LEGITEXT)`` ; ``kind='texte'`` → ``lawDecree(JORFTEXT)``."""
        payload = self.table_matieres(text_uid, date_millis) if kind == "code" else self.law_decree(text_uid, date_millis)
        return walk_table_matieres(payload)

    def code_articles_en_vigueur(self, legitext: str, date_millis: int) -> list[str]:
        """CIDs chroniques des articles en vigueur d'un code à une date."""
        return articles_en_vigueur(self.table_matieres(legitext, date_millis))

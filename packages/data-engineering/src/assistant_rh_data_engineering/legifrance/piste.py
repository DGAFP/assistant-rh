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
    """Un article d'un code, tel que renvoyé par ``tableMatieres``."""

    cid: str  # LEGIARTI...
    etat: str  # VIGUEUR | ABROGE | ...
    num: str | None = None  # numéro d'article (L1, R.331-7, ...)


def walk_table_matieres(payload: dict) -> list[CodeArticle]:
    """Extrait récursivement les articles (``cid`` LEGIARTI + ``etat`` + ``num``)
    d'une réponse ``tableMatieres``, quel que soit l'imbriquement des sections.

    Fonction pure (aucun I/O) — testable sur un fixture.
    """
    found: list[CodeArticle] = []

    def _walk(node: object) -> None:
        if isinstance(node, dict):
            ident = str(node.get("cid") or node.get("id") or "")
            if ident.startswith("LEGIARTI"):
                found.append(CodeArticle(cid=ident, etat=str(node.get("etat") or ""), num=node.get("num")))
            for value in node.values():
                _walk(value)
        elif isinstance(node, list):
            for value in node:
                _walk(value)

    _walk(payload)
    return found


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

    def code_articles_en_vigueur(self, legitext: str, date_millis: int) -> list[str]:
        """CIDs des articles en vigueur d'un code à une date (le set follow-live)."""
        return articles_en_vigueur(self.table_matieres(legitext, date_millis))

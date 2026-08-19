"""Identité d'un corpus PDF ministériel.

Chaque ministère porte sa propre identité — jamais de constante partagée entre
corpus (le hardcode SERVICE PUBLIC qui avait fui dans MATTE est exactement le
bug que cette structure évite). Le namespace uuid5 dédié garantit des
doc_id/section_id stables entre runs pour un même uid de manifest.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass


@dataclass(frozen=True)
class MinistryIdentity:
    ministere: str  # slug technique (préfixes S3, CLI, catalogue)
    corpus: str  # valeur source_corpus dans le référentiel Grist
    chunk_source: str  # colonne source des chunks (convention MAJUSCULES)
    doc_source: str  # colonne source de rag_documents (filtre de réconciliation)
    publisher: str  # libellé long du ministère
    chunk_table: str  # table Postgres des chunks
    namespace: uuid.UUID  # namespace uuid5 des doc_id/section_id

    @property
    def object_storage_source_name(self) -> str:
        """Nom de source pour les préfixes Object Storage (medallion_prefix/sync)."""
        return f"pdf_sources/{self.ministere}"

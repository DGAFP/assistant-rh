# %% [markdown]
# Comparaison XML officiel vs dataset vectorise
#
# Version script a cellules VS Code du notebook du meme nom.

# %% [markdown]
# Analyse des sources comparees
#
# On compare en fait 3 couches :
# - XML officiel DILA / Service-Public : source de verite brute.
# - data.gouv.fr : catalogue / point d'entree institutionnel vers le jeu de donnees vectorise.
# - Hugging Face `AgentPublic/service-public` : source operationnelle effectivement exploitable dans ce notebook.
# 
# Point important :
# - `data.gouv.fr` reference le jeu de donnees et sa documentation.
# - Hugging Face expose les fichiers Parquet et la dataset card avec la methode de chunking/vectorisation.
# - Le notebook utilise Hugging Face pour charger les donnees vectorisees, car c'est l'endpoint le plus direct pour l'analyse et l'usage pipeline.

# %%
from __future__ import annotations

import difflib
import io
import json
import re
import sys
import zipfile
from pathlib import Path
from urllib.request import Request, urlopen

import pandas as pd

cwd = Path.cwd().resolve()
REPO_ROOT = cwd.parent if cwd.name == "scripts" else cwd
PACKAGE_SOURCE_PATHS = [
    REPO_ROOT,
    REPO_ROOT / "packages" / "data-engineering" / "src",
]
for source_path in reversed(PACKAGE_SOURCE_PATHS):
    path_str = str(source_path)
    if path_str not in sys.path:
        sys.path.insert(0, path_str)

from assistant_rh_data_engineering.service_public.xml_parser import parse_fiche_xml_from_bytes  # noqa: E402

FICHE_ID = "F12391"
XML_ZIP_FALLBACK_URL = "https://lecomarquage.service-public.gouv.fr/vdd/3.4/part/zip/vosdroits-latest.zip"
DATA_GOUV_API_ROOT = "https://www.data.gouv.fr/api/1"
DATASET_SLUG = "service-public-fr-guide-vos-droits-et-demarches-particuliers"
HF_DATASET = "AgentPublic/service-public"
BS = chr(92)
NL = chr(10)
CR = chr(13)
DATA_GOUV_DATASET_URL = "https://www.data.gouv.fr/datasets/fiches-pratiques-service-public-fr-vectorisees"
HF_DATASET_URL = "https://huggingface.co/datasets/AgentPublic/service-public"

print("Repo root:", REPO_ROOT)
print("Fiche ID :", FICHE_ID)

# %%
source_analysis = pd.DataFrame(
    [
        {
            "source": "xml_officiel_dila",
            "url": XML_ZIP_FALLBACK_URL,
            "role": "source primaire",
            "contenu": "documents XML officiels complets",
            "chunking": "aucun",
            "embeddings": "aucun",
            "controle_pipeline": "maximal",
            "avantages": "source de verite, structure riche, re-chunking libre",
            "inconvenients": "pipeline a construire et maintenir",
        },
        {
            "source": "data_gouv_catalogue",
            "url": DATA_GOUV_DATASET_URL,
            "role": "catalogue institutionnel",
            "contenu": "metadonnees + lien vers jeu vectorise",
            "chunking": "documente, pas exploite directement ici",
            "embeddings": "documentes",
            "controle_pipeline": "faible",
            "avantages": "visibilite institutionnelle, publication officielle",
            "inconvenients": "pas l'endpoint le plus pratique pour le chargement analytique",
        },
        {
            "source": "huggingface_agentpublic",
            "url": HF_DATASET_URL,
            "role": "source operationnelle vectorisee",
            "contenu": "34k+ chunks parquet avec metadonnees",
            "chunking": "RecursiveCharacterTextSplitter, chunk_size=1024, overlap=0",
            "embeddings": "BAAI/bge-m3 sur chunk_text",
            "controle_pipeline": "moyen",
            "avantages": "pret a charger, schema riche, embeddings deja calcules",
            "inconvenients": "chunking impose, embedding impose, pas la source brute",
        },
    ]
)
source_analysis

# %%
def http_get(url: str) -> bytes:
    request = Request(
        url,
        headers={
            "User-Agent": "assistant-rh/1.0 (+https://www.data.gouv.fr/)",
            "Accept": "*/*",
        },
    )
    with urlopen(request, timeout=60) as response:
        return response.read()


def fetch_xml_zip_url() -> str:
    api_url = f"{DATA_GOUV_API_ROOT}/datasets/{DATASET_SLUG}/"
    try:
        dataset = json.loads(http_get(api_url).decode("utf-8"))
        candidates: list[str] = []
        for resource in dataset.get("resources", []):
            for key in ("url", "latest", "original_url"):
                value = resource.get(key)
                if isinstance(value, str) and value:
                    candidates.append(value)

        for url in candidates:
            lowered = url.lower()
            if lowered.endswith("vosdroits-latest.zip") or "/zip/" in lowered:
                return url
        for url in candidates:
            if url.lower().endswith(".zip"):
                return url
    except Exception as exc:
        print(f"API data.gouv.fr indisponible, fallback URL directe: {exc}")
    return XML_ZIP_FALLBACK_URL


def load_fiche_xml_from_zip(zip_bytes: bytes, fiche_id: str) -> bytes:
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
        expected_suffixes = (
            f"/{fiche_id}.xml",
            f"{BS}{fiche_id}.xml",
            f"{fiche_id}.xml",
        )
        for member in archive.namelist():
            if member.endswith(expected_suffixes):
                return archive.read(member)
    raise FileNotFoundError(f"Fiche {fiche_id}.xml introuvable dans le ZIP.")


def normalize_for_compare(text: str) -> str:
    text = text.replace(CR + NL, NL).replace(CR, NL)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def preview(text: str, n: int = 1200) -> str:
    return text[:n].strip()


# %%
zip_url = fetch_xml_zip_url()
xml_bytes = load_fiche_xml_from_zip(http_get(zip_url), FICHE_ID)
xml_parsed = parse_fiche_xml_from_bytes(xml_bytes, FICHE_ID)
assert xml_parsed, f"Echec du parsing XML pour {FICHE_ID}"

xml_text = xml_parsed["doc_markdown"]
print("ZIP XML utilise :", zip_url)
print("Titre XML      :", xml_parsed["title"])
print("URL XML        :", xml_parsed["source_url"])
print("Longueur XML   :", len(xml_text))

# %%
# Si besoin avant execution:
# pip install datasets

from datasets import load_dataset  # noqa: E402

hf_ds = load_dataset(HF_DATASET, split="train")
df = hf_ds.to_pandas()
print("Colonnes disponibles :")
print(sorted(df.columns.tolist()))
print("Nb lignes           :", len(df))

# %%
fiche_df = pd.DataFrame()
if "doc_id" in df.columns:
    fiche_df = df[df["doc_id"].astype(str) == FICHE_ID].copy()

if fiche_df.empty and "url" in df.columns:
    fiche_df = df[df["url"].astype(str).str.endswith("/" + FICHE_ID)].copy()

assert not fiche_df.empty, f"Aucune ligne trouvee pour {FICHE_ID}"

if "chunk_index" in fiche_df.columns:
    fiche_df = fiche_df.sort_values("chunk_index").reset_index(drop=True)

fiche_df[[c for c in ["doc_id", "url", "title", "chunk_index"] if c in fiche_df.columns]].head(10)

# %%
if "text" in fiche_df.columns:
    reconstructed_text = NL.join(fiche_df["text"].astype(str).tolist())
    reconstruction_field = "text"
elif "chunk_text" in fiche_df.columns:
    reconstructed_text = NL.join(fiche_df["chunk_text"].astype(str).tolist())
    reconstruction_field = "chunk_text"
else:
    raise KeyError("Le dataset ne contient ni text ni chunk_text")

vector_title = fiche_df["title"].iloc[0] if "title" in fiche_df.columns else ""
vector_url = fiche_df["url"].iloc[0] if "url" in fiche_df.columns else ""

print("Champ utilise          :", reconstruction_field)
print("Titre vectorise        :", vector_title)
print("URL vectorisee         :", vector_url)
print("Nb chunks fiche        :", len(fiche_df))
print("Longueur reconstituee  :", len(reconstructed_text))

# %%
xml_norm = normalize_for_compare(xml_text)
vector_norm = normalize_for_compare(reconstructed_text)

matcher = difflib.SequenceMatcher(None, xml_norm, vector_norm)
ratio = matcher.ratio()

comparison = pd.DataFrame(
    [
        {
            "source": "xml_officiel",
            "chars": len(xml_text),
            "words": len(xml_norm.split()),
            "title": xml_parsed["title"],
        },
        {
            "source": f"vectorise_reconstruit::{reconstruction_field}",
            "chars": len(reconstructed_text),
            "words": len(vector_norm.split()),
            "title": vector_title,
        },
        {
            "source": "similarite_normalisee",
            "chars": ratio,
            "words": None,
            "title": "",
        },
    ]
)
comparison

# %%
print("=== Apercu XML officiel ===")
print(preview(xml_text))
print()
print("=== Apercu dataset vectorise reconstitue ===")
print(preview(reconstructed_text))

# %%
xml_lines = xml_norm.split(". ")
vector_lines = vector_norm.split(". ")
for line in difflib.unified_diff(
    xml_lines[:40],
    vector_lines[:40],
    fromfile="xml",
    tofile="vectorise",
    lineterm="",
):
    print(line)

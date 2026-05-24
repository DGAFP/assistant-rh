from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from difflib import SequenceMatcher
from pathlib import Path

from assistant_rh_data_engineering.legifrance import (
    LegifrancePipeline,
    LegifrancePipelineConfig,
)


def normalize_text(text: str) -> str:
    text = (text or "").strip()
    text = re.sub(r"\s+", " ", text)
    return text


def group_existing_chunks(rows: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        article_number = row.get("number") or ""
        grouped[article_number].append(
            {
                "chunk_index": row.get("chunk_index", 0),
                "text": row.get("chunk_text") or row.get("text") or "",
                "cid": row.get("cid"),
            }
        )
    for article_number in grouped:
        grouped[article_number].sort(key=lambda row: (row["chunk_index"], row.get("cid") or ""))
    return grouped


def group_generated_chunks(gold_bundles: list) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for bundle in gold_bundles:
        article_number = bundle.document.get("metadata", {}).get("num_article") or bundle.document["short_id"]
        for chunk in bundle.chunks:
            grouped[article_number].append(
                {
                    "chunk_index": chunk.get("chunk_index", 0),
                    "text": chunk.get("chunk_text") or chunk.get("text") or "",
                    "hash_id": chunk.get("hash_id"),
                }
            )
    for article_number in grouped:
        grouped[article_number].sort(key=lambda row: row["chunk_index"])
    return grouped


def compare_article_chunks(article_number: str, existing_chunks: list[dict], generated_chunks: list[dict]) -> dict:
    max_len = max(len(existing_chunks), len(generated_chunks))
    per_chunk: list[dict] = []
    exact_matches = 0
    similarity_sum = 0.0

    for chunk_index in range(max_len):
        existing = existing_chunks[chunk_index] if chunk_index < len(existing_chunks) else None
        generated = generated_chunks[chunk_index] if chunk_index < len(generated_chunks) else None
        existing_text = normalize_text(existing["text"]) if existing else ""
        generated_text = normalize_text(generated["text"]) if generated else ""
        similarity = SequenceMatcher(a=existing_text, b=generated_text).ratio() if (existing or generated) else 1.0
        exact = existing_text == generated_text and bool(existing or generated)
        exact_matches += int(exact)
        similarity_sum += similarity
        per_chunk.append(
            {
                "chunk_index": chunk_index,
                "existing_present": existing is not None,
                "generated_present": generated is not None,
                "existing_chars": len(existing_text),
                "generated_chars": len(generated_text),
                "exact_text_match": exact,
                "similarity": round(similarity, 4),
            }
        )

    existing_full = normalize_text("\n\n".join(chunk["text"] for chunk in existing_chunks))
    generated_full = normalize_text("\n\n".join(chunk["text"] for chunk in generated_chunks))
    article_similarity = (
        SequenceMatcher(a=existing_full, b=generated_full).ratio()
        if (existing_full or generated_full)
        else 1.0
    )
    return {
        "num_article": article_number,
        "existing_chunk_count": len(existing_chunks),
        "generated_chunk_count": len(generated_chunks),
        "chunk_count_delta": len(generated_chunks) - len(existing_chunks),
        "exact_chunk_matches": exact_matches,
        "avg_chunk_similarity": round(similarity_sum / max_len, 4) if max_len else 1.0,
        "article_similarity": round(article_similarity, 4),
        "per_chunk": per_chunk,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compare existing DGAFP/Legifrance chunks with the new Legifrance medallion pipeline output."
    )
    parser.add_argument("--report", default="tests/legifrance_db_vs_pipeline_report.json")
    parser.add_argument("--schema", default="public")
    parser.add_argument("--table", default="rag_chunks_dgafp")
    args = parser.parse_args()

    config = LegifrancePipelineConfig()
    config.bronze.dgafp_table_name = args.table
    config.embeddings.enable_m3 = False
    config.embeddings.enable_bge_scaleway = False
    pipe = LegifrancePipeline(config)

    bronze_assets = pipe.run_bronze()
    silver_bundles = pipe.run_silver(bronze_assets)
    gold_bundles = pipe.run_gold(silver_bundles)

    article_numbers = sorted(
        {
            asset.payload["num_article"]
            for asset in bronze_assets
            if asset.asset_type == "article" and asset.payload.get("num_article")
        }
    )
    existing_rows = pipe.bronze_builder.fetch_chunk_rows_from_dgafp_table(article_numbers)
    existing_by_article = group_existing_chunks(existing_rows)
    generated_by_article = group_generated_chunks(gold_bundles)

    article_numbers_to_compare = sorted(set(existing_by_article) | set(generated_by_article))
    comparisons = [
        compare_article_chunks(
            article_number,
            existing_by_article.get(article_number, []),
            generated_by_article.get(article_number, []),
        )
        for article_number in article_numbers_to_compare
    ]

    summary = {
        "existing_articles": len(existing_by_article),
        "generated_articles": len(generated_by_article),
        "compared_articles": len(comparisons),
        "exact_article_matches": sum(
            1
            for row in comparisons
            if row["chunk_count_delta"] == 0 and row["article_similarity"] == 1.0
        ),
        "avg_article_similarity": round(
            sum(row["article_similarity"] for row in comparisons) / len(comparisons),
            4,
        )
        if comparisons
        else 0.0,
        "avg_chunk_count_delta": round(
            sum(abs(row["chunk_count_delta"]) for row in comparisons) / len(comparisons),
            4,
        )
        if comparisons
        else 0.0,
        "comparisons": comparisons,
    }

    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

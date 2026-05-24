from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

import pandas as pd


def load_jsonl(path: Path) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return pd.DataFrame(rows)


def text_signature(text: str) -> str:
    text = (text or "").strip().lower()
    text = " ".join(text.split())
    return text[:300]


def filter_old_df(df: pd.DataFrame, fiche_id: str) -> pd.DataFrame:
    if "source_name" not in df.columns:
        return df.iloc[0:0].copy()

    masks = [
        df["source_name"].astype(str).str.contains(fiche_id, case=False, na=False),
        df["source_name"].astype(str).str.contains(f"{fiche_id}.xml", case=False, na=False),
        df["source_name"].astype(str).str.contains(f"{fiche_id}.pdf", case=False, na=False),
    ]
    mask = masks[0]
    for extra in masks[1:]:
        mask = mask | extra
    return df[mask].copy()


def summarize(df: pd.DataFrame, label: str) -> dict[str, Any]:
    out: dict[str, Any] = {
        "label": label,
        "rows": len(df),
        "columns": sorted(df.columns.tolist()),
    }
    if "role" in df.columns:
        out["role_counts"] = dict(Counter(df["role"].fillna("").astype(str)))
    if "text" in df.columns:
        texts = df["text"].fillna("").astype(str)
        out["text_chars_total"] = int(texts.str.len().sum())
        out["text_chars_mean"] = float(texts.str.len().mean()) if len(texts) else 0.0
    return out


def compare_frames(old_df: pd.DataFrame, new_df: pd.DataFrame) -> dict[str, Any]:
    report: dict[str, Any] = {
        "old": summarize(old_df, "old_notebook"),
        "new": summarize(new_df, "new_xml_pipeline"),
    }

    common_cols = sorted(set(old_df.columns) & set(new_df.columns))
    report["common_columns"] = common_cols
    report["old_only_columns"] = sorted(set(old_df.columns) - set(new_df.columns))
    report["new_only_columns"] = sorted(set(new_df.columns) - set(old_df.columns))

    if "text" in old_df.columns and "text" in new_df.columns:
        old_sig = Counter(text_signature(v) for v in old_df["text"].fillna("").astype(str) if v)
        new_sig = Counter(text_signature(v) for v in new_df["text"].fillna("").astype(str) if v)
        intersection = sum((old_sig & new_sig).values())
        old_total = sum(old_sig.values()) or 1
        new_total = sum(new_sig.values()) or 1
        report["text_overlap"] = {
            "shared_signatures": intersection,
            "old_coverage_ratio": round(intersection / old_total, 4),
            "new_coverage_ratio": round(intersection / new_total, 4),
        }

    if "role" in old_df.columns and "role" in new_df.columns:
        old_roles = Counter(old_df["role"].fillna("").astype(str))
        new_roles = Counter(new_df["role"].fillna("").astype(str))
        report["role_delta"] = {
            role: {
                "old": int(old_roles.get(role, 0)),
                "new": int(new_roles.get(role, 0)),
                "delta": int(new_roles.get(role, 0) - old_roles.get(role, 0)),
            }
            for role in sorted(set(old_roles) | set(new_roles))
        }

    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare notebook chunk output with XML medallion output.")
    parser.add_argument("--old-jsonl", required=True, help="JSONL produced by the old notebook pipeline.")
    parser.add_argument("--new-jsonl", required=True, help="JSONL produced by the XML medallion pipeline.")
    parser.add_argument("--fiche-id", help="Optional fiche ID filter for the old JSONL, ex: F12391.")
    args = parser.parse_args()

    old_path = Path(args.old_jsonl)
    new_path = Path(args.new_jsonl)

    old_df = load_jsonl(old_path)
    new_df = load_jsonl(new_path)

    if args.fiche_id:
        old_df = filter_old_df(old_df, args.fiche_id)

    report = compare_frames(old_df, new_df)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

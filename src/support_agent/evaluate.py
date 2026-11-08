"""Human-labeled, independently produced results; demo outputs cannot establish model quality."""

import json
from itertools import combinations
from pathlib import Path


def pr(gold, predicted):
    tp = len(gold & predicted)
    return {
        "precision": tp / len(predicted) if predicted else None,
        "recall": tp / len(gold) if gold else None,
        "true_positive": tp,
        "gold": len(gold),
        "predicted": len(predicted),
    }


def evaluate(gold_path=None, results_path=None):
    if not gold_path or not results_path:
        return {
            "status": "not_run",
            "reason": "Independent gold labels and real internal-model results required",
            "pilot_targets": {
                "extraction_precision": 0.9,
                "extraction_recall": 0.8,
                "pairwise_precision": 0.9,
            },
        }
    gold = json.loads(Path(gold_path).read_text())
    results = json.loads(Path(results_path).read_text())
    if results.get("provider") != "internal" or gold.get("synthetic") or results.get("synthetic"):
        return {
            "status": "not_run",
            "reason": "Mock/synthetic responses establish scenarios, not production quality",
        }

    def labels(data):
        return {(r["ticket_id"], i["kind"], i["need_id"]) for r in data["tickets"] for i in r["items"]}

    def pairs(data):
        return {
            tuple(sorted(pair)) for group in data.get("groups", []) for pair in combinations(set(group), 2)
        }

    cases = results["tickets"]
    evidence = [e for r in cases for i in r["items"] for e in i.get("evidence_valid", [])]
    relevant = [(r["gold_candidate_id"], r["candidate_ids"]) for r in cases if r.get("gold_candidate_id")]
    channels = {r["channel"] for r in gold["tickets"]}
    return {
        "status": "measured_from_supplied_files",
        "extraction": pr(labels(gold), labels(results)),
        "pairwise_grouping": pr(pairs(gold), pairs(results)),
        "candidate_recall": sum(g in ids for g, ids in relevant) / len(relevant) if relevant else None,
        "evidence_validity": sum(evidence) / len(evidence) if evidence else None,
        "channel_coverage": {
            c: sum(r.get("complete", False) for r in cases if r["channel"] == c)
            / max(1, sum(r["channel"] == c for r in gold["tickets"]))
            for c in channels
        },
    }

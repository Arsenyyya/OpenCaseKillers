"""Evaluate independently annotated files; mock answers never establish production quality."""

import json
from itertools import combinations
from pathlib import Path
from support_agent.errors import Permanent

KINDS = {"feature_request", "defect", "service_request", "incident", "unclear"}
CHANNELS = {"text", "audio", "document", "image"}


def pr(gold, predicted):
    tp = len(gold & predicted)
    return {
        "precision": tp / len(predicted) if predicted else None,
        "recall": tp / len(gold) if gold else None,
        "true_positive": tp,
        "gold": len(gold),
        "predicted": len(predicted),
    }


def _read(path):
    try:
        path = Path(path)
        if path.stat().st_size > 10_000_000:
            raise ValueError()
        value = json.loads(path.read_text())
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except (OSError, ValueError):
        raise Permanent("invalid_evaluation_file") from None


def _validate(data):
    tickets = data.get("tickets")
    if not isinstance(tickets, list) or len(tickets) > 100000:
        raise Permanent("invalid_evaluation_tickets")
    by_id, occurrences = {}, set()
    for ticket in tickets:
        if (
            not isinstance(ticket, dict)
            or not isinstance(ticket.get("ticket_id"), str)
            or not ticket["ticket_id"]
            or ticket["ticket_id"] in by_id
            or ticket.get("channel") not in CHANNELS
        ):
            raise Permanent("invalid_evaluation_ticket_identity")
        if type(ticket.get("complete", False)) is not bool:
            raise Permanent("invalid_evaluation_coverage")
        items = ticket.get("items")
        if not isinstance(items, list) or len(items) > 100:
            raise Permanent("invalid_evaluation_items")
        labels = set()
        for item in items:
            if (
                not isinstance(item, dict)
                or item.get("kind") not in KINDS
                or not isinstance(item.get("need_id"), str)
                or not item["need_id"]
            ):
                raise Permanent("invalid_evaluation_item")
            label = (ticket["ticket_id"], item["kind"], item["need_id"])
            if label in labels:
                raise Permanent("duplicate_evaluation_label")
            labels.add(label)
            occurrence = item.get("occurrence_id", "::".join(label))
            if not isinstance(occurrence, str) or not occurrence or occurrence in occurrences:
                raise Permanent("invalid_evaluation_occurrence")
            occurrences.add(occurrence)
            validity = item.get("evidence_valid", [])
            if not isinstance(validity, list) or any(type(value) is not bool for value in validity):
                raise Permanent("invalid_evidence_judgement")
        candidates = ticket.get("candidate_ids", [])
        if (
            not isinstance(candidates, list)
            or len(candidates) > 100
            or any(not isinstance(x, str) for x in candidates)
            or len(set(candidates)) != len(candidates)
        ):
            raise Permanent("invalid_evaluation_candidates")
        by_id[ticket["ticket_id"]] = ticket
    groups = data.get("groups", [])
    if not isinstance(groups, list):
        raise Permanent("invalid_evaluation_groups")
    assigned = set()
    for group in groups:
        if (
            not isinstance(group, list)
            or any(not isinstance(x, str) for x in group)
            or len(set(group)) != len(group)
            or not set(group) <= occurrences
            or assigned.intersection(group)
        ):
            raise Permanent("invalid_evaluation_group_membership")
        assigned.update(group)
    return by_id


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
    gold, results = _read(gold_path), _read(results_path)
    if results.get("provider") != "internal" or gold.get("synthetic") or results.get("synthetic"):
        return {
            "status": "not_run",
            "reason": "Mock/synthetic responses establish scenarios, not production quality",
        }
    truth, cases = _validate(gold), _validate(results)
    if not set(cases) <= set(truth) or any(cases[key]["channel"] != truth[key]["channel"] for key in cases):
        raise Permanent("evaluation_scope_mismatch")

    def labels(data):
        return {(r["ticket_id"], i["kind"], i["need_id"]) for r in data["tickets"] for i in r["items"]}

    def pairs(data):
        return {tuple(sorted(pair)) for group in data.get("groups", []) for pair in combinations(group, 2)}

    evidence = [e for r in cases.values() for i in r["items"] for e in i.get("evidence_valid", [])]
    # Candidate truth comes from independent gold, never from a model's own output.
    relevant = [(key, row["gold_candidate_id"]) for key, row in truth.items() if row.get("gold_candidate_id")]
    channels = {r["channel"] for r in truth.values()}
    return {
        "status": "measured_from_supplied_files",
        "extraction": pr(labels(gold), labels(results)),
        "pairwise_grouping": pr(pairs(gold), pairs(results)),
        "candidate_recall": sum(
            expected in cases.get(key, {}).get("candidate_ids", []) for key, expected in relevant
        )
        / len(relevant)
        if relevant
        else None,
        "evidence_validity": sum(evidence) / len(evidence) if evidence else None,
        "channel_coverage": {
            channel: sum(
                cases.get(key, {}).get("complete", False)
                for key, row in truth.items()
                if row["channel"] == channel
            )
            / sum(row["channel"] == channel for row in truth.values())
            for channel in channels
        },
        "missing_result_tickets": len(set(truth) - set(cases)),
    }

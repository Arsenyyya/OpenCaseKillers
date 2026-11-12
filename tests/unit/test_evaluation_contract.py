import json
import pytest
from support_agent.evaluate import evaluate
from support_agent.errors import Permanent


def files(tmp_path):
    one = {
        "ticket_id": "atlas:t1",
        "channel": "text",
        "gold_candidate_id": "g1",
        "items": [{"kind": "feature_request", "need_id": "csv", "occurrence_id": "n1"}],
    }
    two = {
        "ticket_id": "atlas:t2",
        "channel": "text",
        "gold_candidate_id": "g1",
        "items": [{"kind": "feature_request", "need_id": "csv", "occurrence_id": "n2"}],
    }
    gold = {"tickets": [one, two], "groups": [["n1", "n2"]]}
    predicted = {
        "provider": "internal",
        "tickets": [dict(one, complete=True, candidate_ids=["g1"])],
        "groups": [["n1"]],
    }
    a, b = tmp_path / "gold.json", tmp_path / "predicted.json"
    a.write_text(json.dumps(gold))
    b.write_text(json.dumps(predicted))
    return a, b, gold, predicted


def test_independent_gold_denominator_includes_missing_results(tmp_path):
    a, b, _, _ = files(tmp_path)
    result = evaluate(a, b)
    assert result["extraction"]["precision"] == 1 and result["extraction"]["recall"] == 0.5
    assert result["candidate_recall"] == 0.5 and result["channel_coverage"]["text"] == 0.5
    assert result["missing_result_tickets"] == 1 and result["evidence_validity"] is None


@pytest.mark.parametrize("mutation", ["duplicate", "foreign", "channel", "group"])
def test_evaluation_rejects_inflated_or_foreign_scope(tmp_path, mutation):
    a, b, _, predicted = files(tmp_path)
    if mutation == "duplicate":
        predicted["tickets"] *= 2
    elif mutation == "foreign":
        predicted["tickets"][0]["ticket_id"] = "other:t1"
    elif mutation == "channel":
        predicted["tickets"][0]["channel"] = "audio"
    else:
        predicted["groups"] = [["made-up-occurrence"]]
    b.write_text(json.dumps(predicted))
    with pytest.raises(Permanent):
        evaluate(a, b)

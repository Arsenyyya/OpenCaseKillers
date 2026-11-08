import hashlib
import json
import re
from pydantic import ValidationError
from support_agent.contracts import Extraction, Match
from support_agent.errors import InvalidOutput


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str).encode()
    ).hexdigest()


MASK_PATTERNS = [
    ("EMAIL", re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")),
    ("PHONE", re.compile(r"(?<!\w)(?:\+7|8)[\s(-]*\d{3}[\s)-]*\d{3}[\s-]*\d{2}[\s-]*\d{2}(?!\d)")),
    ("SECRET", re.compile(r"(?i)(?:bearer\s+|api[_-]?key\s*[:=]\s*|password\s*[:=]\s*)[\w./+\-=]{6,}")),
]


def mask(text, product):
    for label, pattern in MASK_PATTERNS:
        text = pattern.sub(lambda m: f"[{label}_{digest([product, m.group().lower()])[:10]}]", text)
    return text


def evidence_valid(items, segments):
    lookup = {s["segment_id"]: s["text"] for s in segments}
    for item in items:
        for e in item.evidence:
            if e.segment_id not in lookup or e.quote not in lookup[e.segment_id]:
                raise InvalidOutput()


def parse_extraction(raw, segments, limit):
    try:
        result = Extraction.model_validate_json(raw, strict=True)
    except (ValueError, ValidationError):
        raise InvalidOutput() from None
    if len(result.items) > limit:
        raise InvalidOutput()
    evidence_valid(result.items, segments)
    return result


def parse_match(raw, candidates, segments):
    try:
        m = Match.model_validate_json(raw, strict=True)
    except (ValueError, ValidationError):
        raise InvalidOutput() from None
    if (m.action == "match" and m.candidate_id not in {c["id"] for c in candidates}) or (
        m.action != "match" and m.candidate_id is not None
    ):
        raise InvalidOutput()
    evidence_valid([m], segments)
    return m

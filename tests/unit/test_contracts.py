import json
import io
import zipfile
import pytest
from pydantic import ValidationError
from support_agent.policy import parse_extraction, parse_match, mask
from support_agent.errors import InvalidOutput, Permanent
from support_agent.settings import Settings
from support_agent.contracts import Attachment
from support_agent.services.media.normalize import normalize_attachment
from support_agent.adapters.storage import LocalStorage
from support_agent.evaluate import evaluate


SEGMENTS = [{"segment_id": "s", "text": "Хочу CSV для списка заказов"}]
ITEM = {
    "kind": "feature_request",
    "urgency": "unknown",
    "need": "Добавить CSV",
    "context": "",
    "evidence": [{"segment_id": "s", "quote": "Хочу CSV"}],
    "needs_review": False,
    "review_reason": None,
}


@pytest.mark.parametrize(
    "mutate",
    [
        lambda v: v.update(product_id="boreal"),
        lambda v: v.update(kind="mixed"),
        lambda v: v.update(needs_review="false"),
        lambda v: v["evidence"][0].update(segment_id="foreign"),
        lambda v: v["evidence"][0].update(quote="invented quote"),
        lambda v: v.update(need="x" * 501),
    ],
)
def test_strict_extraction_blocks_untrusted_fields_and_bad_evidence(mutate):
    value = json.loads(json.dumps(ITEM))
    mutate(value)
    with pytest.raises(InvalidOutput):
        parse_extraction(json.dumps({"items": [value]}), SEGMENTS, 12)


def test_matching_candidate_allowlist_and_evidence():
    value = {"action": "match", "candidate_id": "foreign", "reason": "test", "evidence": ITEM["evidence"]}
    with pytest.raises(InvalidOutput):
        parse_match(json.dumps(value), [{"id": "allowed"}], SEGMENTS)
    value.update(candidate_id="allowed")
    assert parse_match(json.dumps(value), [{"id": "allowed"}], SEGMENTS).candidate_id == "allowed"
    with pytest.raises(InvalidOutput):
        parse_extraction("not json", SEGMENTS, 12)


def test_mask_preserves_meaning_and_is_scoped():
    text = "a@example.test +7 (999) 123-45-67 Bearer DEMO_SECRET_123456 ошибка 500, CSV 30 дней"
    masked = mask(text, "atlas")
    assert mask(text, "atlas") == masked and mask(text, "boreal") != masked
    assert "ошибка 500, CSV 30 дней" in masked
    for secret in ["a@example.test", "DEMO_SECRET_123456", "123-45-67"]:
        assert secret not in masked


def test_internal_configuration_cannot_silently_fallback():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, mode="internal")
    with pytest.raises(ValidationError):
        Settings(_env_file=None, app_env="internal", mode="demo")


async def test_media_type_zip_limits_paths_and_pdf_text_layer(tmp_path):
    settings = Settings(_env_file=None)
    calls = []

    async def recognize(data, kind, page):
        calls.append((kind, page))
        return [{"text": "Synthetic scanned page", "quality": "unknown"}]

    root = settings.fixtures_dir / "assets"
    pdf = Attachment(id="pdf", content_ref="mixed.pdf", declared_type="audio")
    result = await normalize_attachment((root / "mixed.pdf").read_bytes(), pdf, "atlas", settings, recognize)
    assert calls == [("pdf_page", 2)]
    assert result[0]["media_type"] == "pdf_text" and result[1]["quality"] == "unknown"
    assert result[1]["confidence"] is None
    with pytest.raises(Permanent):
        await normalize_attachment(
            (root / "mixed.pdf").read_bytes(),
            pdf,
            "atlas",
            settings.model_copy(update={"max_pages": 1}),
            recognize,
        )
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("word/document.xml", "<xml/>")
        z.writestr("word/vbaProject.bin", "evil")
    with pytest.raises(Permanent):
        await normalize_attachment(b.getvalue(), pdf, "atlas", settings, recognize)
    (tmp_path / "safe").mkdir()
    (tmp_path / "outside").write_text("secret")
    storage = LocalStorage(tmp_path / "safe", 100)
    with pytest.raises(Permanent):
        storage.read("../outside")
    (tmp_path / "safe/link").symlink_to(tmp_path / "outside")
    with pytest.raises(Permanent):
        storage.read("link")


def test_evaluation_never_reports_mock_as_model_quality(tmp_path):
    assert evaluate()["status"] == "not_run"
    gold = tmp_path / "gold.json"
    result = tmp_path / "result.json"
    gold.write_text('{"synthetic":true}')
    result.write_text('{"provider":"demo"}')
    assert evaluate(gold, result)["status"] == "not_run"

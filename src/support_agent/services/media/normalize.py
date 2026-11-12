import math

from support_agent.contracts import Segment
from support_agent.errors import Permanent
from support_agent.policy import mask

PROCESSOR = "norm-v1-mask-v1"


def text_segment(sid, ref, media, text, product, **kw):
    return Segment(
        segment_id=sid,
        source_ref=ref,
        media_type=media,
        text=mask(text, product),
        processor_version=PROCESSOR,
        **kw,
    ).model_dump()


def detect(data):
    if data.startswith(b"%PDF-"):
        return "pdf"
    if data.startswith(b"PK\x03\x04"):
        return "docx"
    if data.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff")):
        return "image"
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "audio"
    raise Permanent()


async def normalize_attachment(data, attachment, product, settings, recognize):
    if len(data) > settings.max_file_bytes:
        raise Permanent()
    kind = detect(data)
    sid, ref = attachment.id, attachment.content_ref
    result = []
    from support_agent.services.media.safe_parse import safe_parse

    parsed = await safe_parse(data, kind, settings)
    if kind == "docx":
        result.append(text_segment(f"{sid}-1", ref, "docx", parsed[0]["text"], product, quality="usable"))
    elif kind == "pdf":
        for row in parsed:
            i, text = row["page"], row["text"]
            if len(text.strip()) >= 20:
                result.append(
                    text_segment(f"{sid}-{i}", ref, "pdf_text", text, product, page=i, quality="usable")
                )
            else:
                rows = await recognize(data, "pdf_page", i)
                result.extend(
                    media_segments(rows, sid, ref, "pdf_scan", product, page=i, limit=settings.max_segments)
                )
    elif kind == "image":
        result.extend(
            media_segments(
                await recognize(data, "image", None), sid, ref, "image", product, limit=settings.max_segments
            )
        )
    elif kind == "audio":
        duration = parsed[0]["duration"]
        if attachment.transcript and attachment.transcript_quality == "usable":
            result.append(
                text_segment(
                    f"{sid}-1",
                    ref,
                    "audio_transcript",
                    attachment.transcript,
                    product,
                    quality="usable",
                    start_ms=0,
                    end_ms=int(duration * 1000),
                )
            )
        else:
            result.extend(
                media_segments(
                    await recognize(data, "audio", None),
                    sid,
                    ref,
                    "audio",
                    product,
                    duration_ms=int(duration * 1000),
                    limit=settings.max_segments,
                )
            )
    if not result or len(result) > settings.max_segments:
        raise Permanent()
    return result


def media_segments(rows, sid, ref, kind, product, page=None, duration_ms=None, limit=80):
    if not isinstance(rows, list) or not rows or len(rows) > limit:
        raise Permanent()
    result = []
    for i, row in enumerate(rows, 1):
        if not isinstance(row, dict) or set(row) - {"text", "quality", "confidence", "start_ms", "end_ms"}:
            raise Permanent()
        confidence = row.get("confidence")
        if confidence is not None and (
            type(confidence) not in (int, float) or not math.isfinite(confidence) or not 0 <= confidence <= 1
        ):
            raise Permanent()
        seg = text_segment(
            f"{sid}-{page or 1}-{i}",
            ref,
            kind,
            row.get("text", ""),
            product,
            page=page,
            quality=row.get("quality", "unknown"),
            confidence=row.get("confidence"),
            start_ms=row.get("start_ms"),
            end_ms=row.get("end_ms"),
        )
        if not seg["text"].strip():
            raise Permanent()
        if duration_ms is not None and (
            seg["start_ms"] is None
            or seg["end_ms"] is None
            or not 0 <= seg["start_ms"] < seg["end_ms"] <= duration_ms
        ):
            raise Permanent()
        result.append(seg)
    return result

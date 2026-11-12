from support_agent.contracts import TicketContent
from support_agent.db.models import Revision, MediaCache, Metric
from support_agent.policy import digest
from support_agent.services.media.normalize import normalize_attachment, text_segment, PROCESSOR
from support_agent.errors import AgentError, Capacity, Transient, Stale, Budget
from sqlalchemy import select
from pypdf.errors import PdfReadError
from zipfile import BadZipFile


async def run_media(app, c):
    p = c.product_id
    with app.db.tx([p]) as s:
        app.queue.guard(s, c)
        r = s.get(Revision, (p, c.revision_id))
        raw = r.raw
    content = TicketContent.model_validate_json(__import__("json").dumps(raw))
    segments, failed = [], []
    if content.text.strip():
        segments.append(text_segment("body-1", "ticket-text", "text", content.text, p, quality="usable"))
    attachments = content.attachments[: app.settings.max_files]
    if len(attachments) < len(content.attachments):
        failed.append("file_count_limit")
    config_hash = digest(
        {
            "pages": app.settings.max_pages,
            "audio_seconds": app.settings.max_audio_seconds,
            "file_bytes": app.settings.max_file_bytes,
            "segments": app.settings.max_segments,
            "parse_memory_mb": app.settings.parse_memory_mb,
            "parse_timeout_seconds": app.settings.parse_timeout_seconds,
            "mode": app.settings.mode,
            "asr_model": app.settings.asr_model,
            "vl_model": app.settings.vl_model,
        }
    )
    for a in attachments:
        try:
            data = (
                await app.gateway.attachment(p, a.content_ref)
                if app.gateway
                else app.storage.read(a.content_ref)
            )
            key = digest([data.hex(), a.model_dump(), PROCESSOR])
            with app.db.tx([p]) as s:
                cached = s.scalar(
                    select(MediaCache).where(
                        MediaCache.product_id == p,
                        MediaCache.revision_id == c.revision_id,
                        MediaCache.content_hash == key,
                        MediaCache.processor_version == PROCESSOR,
                        MediaCache.settings_hash == config_hash,
                    )
                )
                rows = cached.segments if cached else None
            if rows is None:

                async def recognize(data, media, page):
                    provider = "asr" if media == "audio" else "vl"
                    output = await app.model_call(
                        c, provider, lambda: app.models.recognize(data, media, page)
                    )
                    with app.db.tx([p]) as s:
                        app.queue.guard(s, c)
                        amount = 1
                        name = "vl_pages"
                        if provider == "asr":
                            import io
                            import wave

                            with wave.open(io.BytesIO(data)) as wav:
                                amount = wav.getnframes() / wav.getframerate() / 60
                            name = "asr_minutes"
                        s.add(Metric(product_id=p, name=name, value=amount, created_at=app.clock.now()))
                    return output

                rows = await normalize_attachment(data, a, p, app.settings, recognize)
                with app.db.tx([p]) as s:
                    app.queue.guard(s, c)
                    s.add(
                        MediaCache(
                            product_id=p,
                            revision_id=c.revision_id,
                            content_hash=key,
                            processor_version=PROCESSOR,
                            settings_hash=config_hash,
                            segments=rows,
                            created_at=app.clock.now(),
                        )
                    )
            segments.extend(rows)
            if any(x["quality"] != "usable" for x in rows):
                failed.append(f"{a.id}:quality_unknown_or_poor")
        except (Capacity, Transient, Stale, Budget):
            raise
        except (AgentError, ValueError, OSError, EOFError, KeyError, PdfReadError, BadZipFile):
            failed.append(f"{a.id}:invalid_or_unsupported")
    # Split oversized segments explicitly; canonical text is exactly what evidence validates against.
    normalized = []
    for seg in segments:
        text = seg["text"]
        for offset in range(0, len(text), app.settings.context_chars):
            row = dict(seg)
            if len(text) > app.settings.context_chars:
                row["segment_id"] = f"{seg['segment_id']}-part{offset // app.settings.context_chars + 1}"
            row["text"] = text[offset : offset + app.settings.context_chars]
            normalized.append(row)
    if len(normalized) > app.settings.max_segments:
        failed.append("segment_limit")
        normalized = normalized[: app.settings.max_segments]
    if not normalized:
        failed.append("no_usable_segments")
    with app.db.tx([p]) as s:
        app.queue.guard(s, c)
        r = s.get(Revision, (p, c.revision_id))
        if digest(r.segments or []) != digest(normalized):
            r.extracted_items, r.processed_at = None, None
        r.segments = normalized
        r.coverage = {
            "complete": not failed,
            "failures": failed,
            "segments": len(normalized),
            "attachments_expected": len(content.attachments),
            "attachments_processed": len(attachments) - sum("invalid" in x for x in failed),
        }
        r.status = "normalized" if not failed else "partial"
        app.queue.complete(s, c, "analysis")

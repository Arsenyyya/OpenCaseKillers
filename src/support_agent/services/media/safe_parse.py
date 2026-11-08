"""Bounded local decoders in disposable processes; no document code or network is executed."""

import asyncio
import io
import multiprocessing as mp
import sys
import wave
import zipfile


def _child(connection, data, kind, limits):
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_CPU, (8, 8))
        if sys.platform == "linux":
            memory = limits["memory_mb"] * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
        if kind == "pdf":
            from pypdf import PdfReader
            import logging

            logging.getLogger("pypdf").disabled = True
            reader = PdfReader(io.BytesIO(data), strict=True)
            if reader.is_encrypted or len(reader.pages) > limits["pages"]:
                raise ValueError()
            result = []
            size = 0
            for i, page in enumerate(reader.pages, 1):
                text = (page.extract_text() or "").strip()
                size += len(text)
                if size > 1_000_000 or len(text) > 200000:
                    raise ValueError()
                result.append({"page": i, "text": text})
        elif kind == "docx":
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                files = archive.infolist()
                if len(files) > 1000 or sum(f.file_size for f in files) > 30_000_000:
                    raise ValueError()
                names = [f.filename for f in files]
                if any("vbaproject" in n.lower() or ".." in n.split("/") for n in names):
                    raise ValueError()
                if "word/document.xml" not in names:
                    raise ValueError()
            from docx import Document

            doc = Document(io.BytesIO(data))
            text = (
                "\n".join(p.text for p in doc.paragraphs)
                + "\n"
                + "\n".join(
                    " | ".join(c.text for c in row.cells) for table in doc.tables for row in table.rows
                )
            )
            if not text.strip() or len(text) > 200000:
                raise ValueError()
            result = [{"text": text.strip()}]
        elif kind == "image":
            from PIL import Image

            with Image.open(io.BytesIO(data)) as image:
                if image.width * image.height > 20_000_000:
                    raise ValueError()
                image.verify()
            result = []
        elif kind == "audio":
            with wave.open(io.BytesIO(data), "rb") as audio:
                duration = audio.getnframes() / audio.getframerate()
                if duration > limits["audio"] or audio.getnchannels() > 2:
                    raise ValueError()
            result = [{"duration": duration}]
        else:
            raise ValueError()
        connection.send({"ok": result})
    except BaseException:
        # Never send library exception strings containing source bytes across the boundary.
        connection.send({"error": "invalid_or_over_limit"})
    finally:
        connection.close()


def _parse(data, kind, limits):
    from support_agent.errors import Permanent

    ctx = mp.get_context("spawn")
    receive, send = ctx.Pipe(duplex=False)
    child = ctx.Process(target=_child, args=(send, data, kind, limits), daemon=True)
    try:
        child.start()
        send.close()
        if not receive.poll(limits["seconds"]):
            raise Permanent()
        result = receive.recv()
        if "error" in result:
            raise Permanent()
        return result["ok"]
    except (OSError, EOFError):
        raise Permanent() from None
    finally:
        if child.pid:
            if child.is_alive():
                child.terminate()
            child.join(timeout=2)
            if child.is_alive():
                child.kill()
                child.join(timeout=2)
        receive.close()
        send.close()


async def safe_parse(data, kind, settings):
    return await asyncio.to_thread(
        _parse,
        data,
        kind,
        {
            "pages": settings.max_pages,
            "audio": settings.max_audio_seconds,
            "memory_mb": settings.parse_memory_mb,
            "seconds": settings.parse_timeout_seconds,
        },
    )

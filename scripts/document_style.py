"""Shared typography, palette and clean metadata for published documents."""

from io import BytesIO
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZIP_DEFLATED, ZipFile

from pypdf import PdfReader, PdfWriter
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "submission/assets"
BLUE, NAVY, CYAN = "0070BA", "004596", "0097D8"
ORANGE, PALE, INK, MUTED, LINE = "E65907", "F2F2F2", "222222", "606060", "D7DFE5"
TITLE = "ИИ-агент для анализа обращений"


def register_fonts(prefix="Submission", font_dir=None):
    candidates = []
    if font_dir:
        candidates += [(Path(font_dir), "Arial.ttf", "Arial Bold.ttf", "Arial")]
    candidates += [
        (Path("/System/Library/Fonts/Supplemental"), "Arial.ttf", "Arial Bold.ttf", "Arial"),
        (
            Path("/usr/share/fonts/truetype/msttcorefonts"),
            "Arial.ttf",
            "Arial_Bold.ttf",
            "Arial",
        ),
        (
            Path("/usr/share/fonts/truetype/liberation2"),
            "LiberationSans-Regular.ttf",
            "LiberationSans-Bold.ttf",
            "Liberation Sans",
        ),
    ]
    for directory, regular, bold, name in candidates:
        if not ((directory / regular).is_file() and (directory / bold).is_file()):
            continue
        pdfmetrics.registerFont(TTFont(prefix, str(directory / regular)))
        pdfmetrics.registerFont(TTFont(prefix + "-Bold", str(directory / bold)))
        pdfmetrics.registerFont(TTFont(prefix + "-Mono", str(directory / regular)))
        pdfmetrics.registerFontFamily(
            prefix, normal=prefix, bold=prefix + "-Bold", italic=prefix, boldItalic=prefix + "-Bold"
        )
        return name
    raise SystemExit("Install Arial or Liberation Sans, or supply an Arial font directory")


def clean_pdf(path, title):
    reader = PdfReader(path)
    writer = PdfWriter(clone_from=reader)
    writer.metadata = {"/Title": title}
    buffer = BytesIO()
    writer.write(buffer)
    Path(path).write_bytes(buffer.getvalue())
    assert set(PdfReader(path).metadata) == {"/Title"}


def clean_office(path):
    path = Path(path)
    buffer = BytesIO()
    with ZipFile(path) as source, ZipFile(buffer, "w", compression=ZIP_DEFLATED) as target:
        for info in source.infolist():
            data = source.read(info.filename)
            if info.filename == "docProps/core.xml":
                root = ET.fromstring(data)
                for child in list(root):
                    if child.tag.split("}")[-1] != "title":
                        root.remove(child)
                data = ET.tostring(root, encoding="utf-8", xml_declaration=True)
            target.writestr(info, data)
    path.write_bytes(buffer.getvalue())

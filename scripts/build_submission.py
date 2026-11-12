"""Build the case submission: a bounded solution, editable slides and matching PDFs.

The case itself remains a private input. Public sources live under submission/source.
Run with the docs extra installed: python scripts/build_submission.py
"""

import json
import re
from decimal import Decimal
from html import escape
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt as WordPt, RGBColor as WordColor
from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR
from pptx.dml.color import RGBColor
from pptx.util import Inches, Pt
from pypdf import PdfReader
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from reportlab.platypus import (
    Flowable,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "submission/source"
FINAL = ROOT / "submission/final"
NAVY, TEAL, ORANGE = "142C46", "087F8C", "E95D22"
INK, MUTED, PALE, LINE = "24364B", "596B7C", "EFF5F8", "CFDCE5"
WHITE, GREEN = "FFFFFF", "287957"
FONT = "Arial"
PAGE_WIDTH = A4[0] - 84


def register_fonts():
    global FONT
    candidates = [
        (Path("/System/Library/Fonts/Supplemental"), "Arial.ttf", "Arial Bold.ttf", "Arial"),
        (
            Path("/usr/share/fonts/truetype/liberation2"),
            "LiberationSans-Regular.ttf",
            "LiberationSans-Bold.ttf",
            "Liberation Sans",
        ),
        (
            Path("/usr/share/fonts/truetype/dejavu"),
            "DejaVuSans.ttf",
            "DejaVuSans-Bold.ttf",
            "DejaVu Sans",
        ),
    ]
    for directory, regular, bold, name in candidates:
        if (directory / regular).is_file() and (directory / bold).is_file():
            pdfmetrics.registerFont(TTFont("Submission", str(directory / regular)))
            pdfmetrics.registerFont(TTFont("Submission-Bold", str(directory / bold)))
            pdfmetrics.registerFontFamily("Submission", normal="Submission", bold="Submission-Bold")
            FONT = name
            return
    raise SystemExit("Install Arial, Liberation Sans or DejaVu Sans to build the submission")


def color(value):
    return colors.HexColor("#" + value)


def check_glyphs(text):
    for name in ["Submission", "Submission-Bold"]:
        glyphs = pdfmetrics.getFont(name).face.charToGlyph
        missing = sorted({c for c in text if ord(c) > 32 and ord(c) not in glyphs})
        if missing:
            raise ValueError(f"Unsupported glyphs in {name}: {missing}")


class Pipeline(Flowable):
    def __init__(self, labels):
        super().__init__()
        self.labels, self.width, self.height = labels, PAGE_WIDTH, 43

    def draw(self):
        width = (self.width - 5 * 10) / 6
        for i, label in enumerate(self.labels):
            x = i * (width + 10)
            self.canv.setFillColor(color(NAVY if i == 5 else PALE))
            self.canv.roundRect(x, 7, width, 29, 4, fill=1, stroke=0)
            self.canv.setFillColor(color(WHITE if i == 5 else INK))
            self.canv.setFont("Submission", 12)
            self.canv.drawCentredString(x + width / 2, 17, label)
            if i < 5:
                self.canv.setStrokeColor(color(TEAL))
                self.canv.line(x + width + 1, 21, x + width + 9, 21)


def build_solution_pdf(data):
    body = ParagraphStyle(
        "body",
        fontName="Submission",
        fontSize=12,
        leading=14.1,
        textColor=color(INK),
        spaceAfter=6,
    )
    heading = ParagraphStyle(
        "heading",
        parent=body,
        fontName="Submission-Bold",
        fontSize=19,
        leading=23,
        textColor=color(NAVY),
        spaceAfter=10,
    )
    quote = ParagraphStyle(
        "quote",
        parent=body,
        backColor=color(PALE),
        borderPadding=7,
        spaceBefore=3,
        spaceAfter=10,
    )
    cell = ParagraphStyle("cell", parent=body, leading=13.8, spaceAfter=0)
    headcell = ParagraphStyle("headcell", parent=cell, fontName="Submission-Bold", textColor=color(WHITE))
    story = []
    for i, page in enumerate(data["pages"]):
        if i:
            story.append(PageBreak())
        story.append(Paragraph(escape(page["title"]), heading))
        for block in page["blocks"]:
            kind = block["type"]
            if kind in {"p", "quote"}:
                story.append(
                    Paragraph(
                        escape(block["text"]).replace("\n", "<br/>"), quote if kind == "quote" else body
                    )
                )
            elif kind == "pipeline":
                story.append(Pipeline(block["labels"]))
            elif kind == "table":
                rows = [[Paragraph(escape(t), headcell) for t in block["headers"]]]
                rows += [[Paragraph(escape(t), cell) for t in row] for row in block["rows"]]
                n = len(block["headers"])
                widths = [PAGE_WIDTH * 0.31, PAGE_WIDTH * 0.69] if n == 2 else [PAGE_WIDTH / n] * n
                table = Table(rows, colWidths=widths, hAlign="LEFT", repeatRows=1)
                table.setStyle(
                    TableStyle(
                        [
                            ("BACKGROUND", (0, 0), (-1, 0), color(NAVY)),
                            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [color(PALE), colors.white]),
                            ("VALIGN", (0, 0), (-1, -1), "TOP"),
                            ("LEFTPADDING", (0, 0), (-1, -1), 7),
                            ("RIGHTPADDING", (0, 0), (-1, -1), 7),
                            ("TOPPADDING", (0, 0), (-1, -1), 5),
                            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                            ("LINEBELOW", (0, 0), (-1, -1), 0.25, color(LINE)),
                        ]
                    )
                )
                story.extend([table, Spacer(1, 7)])

    def page_frame(c, doc):
        c.setFillColor(color(NAVY))
        c.rect(0, A4[1] - 9, A4[0], 9, fill=1, stroke=0)
        c.setFont("Submission-Bold", 9)
        c.drawString(42, A4[1] - 30, "OPENCASEKILLERS · INTELLIGENCE CUP")
        c.setFont("Submission", 9)
        c.setFillColor(color(MUTED))
        c.drawRightString(A4[0] - 42, A4[1] - 30, data["date"])
        c.setStrokeColor(color(LINE))
        c.line(42, 34, A4[0] - 42, 34)
        c.drawString(42, 21, "Основное решение · вопросы 1–4, 6")
        c.drawRightString(A4[0] - 42, 21, str(doc.page))

    doc = SimpleDocTemplate(
        str(FINAL / "Решение.pdf"),
        pagesize=A4,
        leftMargin=42,
        rightMargin=42,
        topMargin=47,
        bottomMargin=43,
        title="OpenCaseKillers — решение кейса",
        author="OpenCaseKillers",
    )
    doc.build(story, onFirstPage=page_frame, onLaterPages=page_frame)


def shade(cell, fill):
    tcpr = cell._tc.get_or_add_tcPr()
    node = OxmlElement("w:shd")
    node.set(qn("w:fill"), fill)
    tcpr.append(node)


def build_solution_docx(data):
    doc = Document()
    section = doc.sections[0]
    section.page_width, section.page_height = Cm(21), Cm(29.7)
    section.top_margin, section.bottom_margin = Cm(1.65), Cm(1.65)
    section.left_margin, section.right_margin = Cm(1.48), Cm(1.48)
    normal = doc.styles["Normal"]
    normal.font.name, normal.font.size = FONT, WordPt(12)
    normal.font.color.rgb = WordColor.from_string(INK)
    normal.paragraph_format.space_after = WordPt(6)
    normal.paragraph_format.line_spacing = 1.02
    for style_name in ["Heading 1", "Heading 2"]:
        doc.styles[style_name].font.name = FONT
        doc.styles[style_name].font.color.rgb = WordColor.from_string(NAVY)
    doc.styles["Heading 1"].font.size = WordPt(19)
    doc.styles["Heading 1"].paragraph_format.space_after = WordPt(10)
    header = section.header.paragraphs[0]
    header.text = f"OPENCASEKILLERS · INTELLIGENCE CUP                            {data['date']}"
    header.style = doc.styles["Normal"]
    for run in header.runs:
        run.font.size = WordPt(9)
    footer = section.footer.paragraphs[0]
    footer.text = "Основное решение · вопросы 1–4, 6                                        "
    field = OxmlElement("w:fldSimple")
    field.set(qn("w:instr"), "PAGE")
    footer._p.append(field)
    for run in footer.runs:
        run.font.size = WordPt(9)
    for i, page in enumerate(data["pages"]):
        if i:
            doc.add_page_break()
        doc.add_heading(page["title"], level=1)
        for block in page["blocks"]:
            kind = block["type"]
            if kind in {"p", "quote"}:
                p = doc.add_paragraph(block["text"])
                if kind == "quote":
                    props = p._p.get_or_add_pPr()
                    shd = OxmlElement("w:shd")
                    shd.set(qn("w:fill"), PALE)
                    props.append(shd)
            elif kind == "pipeline":
                doc.add_paragraph(" → ".join(block["labels"]))
            elif kind == "table":
                table = doc.add_table(rows=1, cols=len(block["headers"]))
                table.alignment = WD_TABLE_ALIGNMENT.CENTER
                table.autofit = False
                widths = [5.59, 12.45] if len(block["headers"]) == 2 else [4.51] * 4
                for column, width in zip(table.columns, widths):
                    column.width = Cm(width)
                for cell, text, width in zip(table.rows[0].cells, block["headers"], widths):
                    cell.width = Cm(width)
                    cell.text = text
                    shade(cell, NAVY)
                    for run in cell.paragraphs[0].runs:
                        run.bold = True
                        run.font.color.rgb = WordColor.from_string(WHITE)
                repeat = OxmlElement("w:tblHeader")
                table.rows[0]._tr.get_or_add_trPr().append(repeat)
                for idx, row in enumerate(block["rows"]):
                    cells = table.add_row().cells
                    for cell, text, width in zip(cells, row, widths):
                        cell.width = Cm(width)
                        cell.text = text
                        if idx % 2 == 0:
                            shade(cell, PALE)
                for row in table.rows:
                    cant_split = OxmlElement("w:cantSplit")
                    row._tr.get_or_add_trPr().append(cant_split)
                    for cell in row.cells:
                        for p in cell.paragraphs:
                            p.paragraph_format.space_after = WordPt(3)
                            p.paragraph_format.space_before = WordPt(3)
                            for run in p.runs:
                                run.font.size = WordPt(12)
                doc.add_paragraph().paragraph_format.space_after = WordPt(0)
    doc.core_properties.title = "OpenCaseKillers — решение кейса"
    doc.core_properties.author = "OpenCaseKillers"
    doc.save(FINAL / "Решение.docx")


def wrap(text, width_pt, font, size):
    """Use explicit line breaks in both renderers so exported PDFs match editable slides."""
    lines = []
    for paragraph in text.split("\n"):
        line = ""
        for word in paragraph.split():
            trial = f"{line} {word}".strip()
            if line and pdfmetrics.stringWidth(trial, font, size) > width_pt:
                lines.append(line)
                line = word
            else:
                line = trial
        lines.append(line)
    return lines


class Deck:
    """A small vector scene rendered into native PowerPoint shapes and a text PDF."""

    def __init__(self):
        self.pptx = Presentation()
        self.pptx.slide_width, self.pptx.slide_height = Inches(13.333), Inches(7.5)
        self.pptx.core_properties.title = "OpenCaseKillers — Intelligence Cup"
        self.pptx.core_properties.author = "OpenCaseKillers"
        self.pdf = canvas.Canvas(str(FINAL / "Презентация.pdf"), pagesize=(960, 540))
        self.pdf.setTitle("OpenCaseKillers — Intelligence Cup")
        self.pdf.setAuthor("OpenCaseKillers")
        self.slides = []
        self.issues = []
        self.texts = []

    def rect(self, x, y, w, h, fill, radius=False, line=None):
        shape = self.slide.shapes.add_shape(
            MSO_SHAPE.ROUNDED_RECTANGLE if radius else MSO_SHAPE.RECTANGLE,
            Inches(x),
            Inches(y),
            Inches(w),
            Inches(h),
        )
        shape.fill.solid()
        shape.fill.fore_color.rgb = RGBColor.from_string(fill)
        if radius:
            shape.adjustments[0] = 0.07
        if line:
            shape.line.color.rgb = RGBColor.from_string(line)
        else:
            shape.line.fill.background()
        self.pdf.setFillColor(color(fill))
        self.pdf.setStrokeColor(color(line or fill))
        if radius:
            self.pdf.roundRect(x * 72, 540 - (y + h) * 72, w * 72, h * 72, 6, fill=1, stroke=bool(line))
        else:
            self.pdf.rect(x * 72, 540 - (y + h) * 72, w * 72, h * 72, fill=1, stroke=bool(line))

    def text(self, x, y, w, h, text, size=20, fill=INK, bold=False, align="left"):
        check_glyphs(text)
        font = "Submission-Bold" if bold else "Submission"
        lines = wrap(text, w * 72 - 2, font, size)
        leading = size * 1.14
        if len(lines) * leading > h * 72 + 0.5:
            self.issues.append(f"slide {len(self.slides)}: text overflow: {text[:65]}")
        for line in lines:
            if pdfmetrics.stringWidth(line, font, size) > w * 72:
                self.issues.append(f"slide {len(self.slides)}: long word: {line}")
        self.texts.append(text)
        shape = self.slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
        tf = shape.text_frame
        tf.clear()
        tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
        tf.word_wrap = False
        tf.vertical_anchor = MSO_ANCHOR.TOP
        from pptx.enum.text import PP_ALIGN

        for i, line in enumerate(lines):
            p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
            p.text, p.space_before, p.space_after = line, Pt(0), Pt(0)
            p.line_spacing = Pt(leading)
            p.alignment = PP_ALIGN.CENTER if align == "center" else PP_ALIGN.LEFT
            p.font.name, p.font.size, p.font.bold = FONT, Pt(size), bold
            p.font.color.rgb = RGBColor.from_string(fill)
            self.pdf.setFont(font, size)
            self.pdf.setFillColor(color(fill))
            baseline = 540 - y * 72 - size * 0.92 - i * leading
            if align == "center":
                self.pdf.drawCentredString((x + w / 2) * 72, baseline, line)
            else:
                self.pdf.drawString(x * 72, baseline, line)

    def arrow(self, x, y, w=0.3, fill=TEAL):
        self.text(x, y, w, 0.35, "→", size=21, fill=fill, align="center")

    def card(self, x, y, w, h, number, title, text, accent=TEAL):
        self.rect(x, y, w, h, PALE, radius=True)
        self.text(x + 0.18, y + 0.15, w - 0.36, 0.45, number, size=24, fill=accent, bold=True)
        self.text(x + 0.18, y + 0.75, w - 0.36, 0.65, title, size=20, bold=True)
        self.text(x + 0.18, y + 1.55, w - 0.36, h - 1.65, text, size=16)

    def start(self, number, tag, title, subtitle=None, dark=False):
        if self.slides:
            self.pdf.showPage()
        self.slide = self.pptx.slides.add_slide(self.pptx.slide_layouts[6])
        self.slides.append(self.slide)
        self.rect(0, 0, 13.333, 7.5, NAVY if dark else WHITE)
        self.rect(0.5, 0.45, 0.08, 0.25, ORANGE)
        self.text(0.7, 0.44, 10, 0.3, tag.upper(), size=12, fill="A7C6D9" if dark else TEAL, bold=True)
        self.text(0.65, 1.0, 12, 0.9, title, size=32, fill=WHITE if dark else NAVY, bold=True)
        if subtitle:
            self.text(0.68, 1.91, 11.9, 0.55, subtitle, size=17, fill="C2D4E0" if dark else MUTED)
        self.text(
            0.65,
            7.12,
            10,
            0.2,
            "OpenCaseKillers  ·  Intelligence Cup  ·  13.11.2026",
            size=10,
            fill="A7C6D9" if dark else MUTED,
        )
        self.text(12.1, 7.1, 0.6, 0.25, f"{number:02}", size=11, fill="A7C6D9" if dark else MUTED)

    def save(self):
        if self.issues:
            raise ValueError("\n".join(self.issues))
        self.pptx.save(FINAL / "Презентация.pptx")
        self.pdf.save()


def build_deck():
    d = Deck()
    d.start(
        1,
        "ИИ-агент для поддержки и продуктовых команд",
        "Из обращений — в понятный backlog",
        "Повторяющиеся потребности, подтверждённые источниками и человеком",
        dark=True,
    )
    labels = ["Обращения", "Потребности", "Повторы", "Карточки", "Решение"]
    for i, label in enumerate(labels):
        x = 0.68 + i * 2.46
        d.rect(x, 3.0, 2.13, 1.05, "23425E" if i < 4 else TEAL, radius=True)
        d.text(x + 0.08, 3.35, 1.97, 0.45, label, size=20, fill=WHITE, bold=True, align="center")
        if i < 4:
            d.arrow(x + 2.14, 3.31, fill="A7C6D9")
    for x, value, label in [
        (0.7, "4", "границы сервисов"),
        (4.8, "1", "внутренний контур"),
        (8.9, "Человек", "принимает решение"),
    ]:
        d.text(x, 4.8, 3.7, 0.65, value, size=35, fill="63CDD0", bold=True)
        d.text(x, 5.58, 3.7, 0.5, label, size=19, fill=WHITE)
    d.text(
        0.7,
        6.45,
        12,
        0.35,
        "Реализован локальный MVP. Корпоративный пилот и качество Qwen предстоит проверить.",
        size=14,
        fill="C2D4E0",
    )

    d.start(
        2,
        "Бизнес-процесс",
        "Повторы доходят до владельца продукта",
        "Срочные инциденты остаются в штатном процессе поддержки",
    )
    for y, title, labels, fill in [
        (2.7, "СЕЙЧАС", ["Тикеты и звонки", "Ручная выгрузка", "Ручной анализ", "Передача команде"], MUTED),
        (
            4.15,
            "С АГЕНТОМ",
            ["Изменения источника", "Распознавание + ИИ", "Частоты + цитаты", "Решение владельца"],
            TEAL,
        ),
    ]:
        d.text(0.7, y + 0.27, 1.4, 0.6, title, size=14, fill=fill, bold=True)
        for i, label in enumerate(labels):
            x = 2.25 + 2.56 * i
            d.rect(x, y, 2.25, 1.05, PALE, radius=True)
            d.text(x + 0.12, y + 0.2, 2.01, 0.7, label, size=19, align="center")
            if i < 3:
                d.arrow(x + 2.25, y + 0.32)
    for x, value, label in [
        (0.7, "≈40", "менеджеров — штат, не поток"),
        (4.9, "1 год", "история в системе тикетов"),
        (9.1, "≤5", "карточек за рабочий день"),
    ]:
        d.text(x, 5.62, 3.6, 0.52, value, size=29, fill=TEAL, bold=True)
        d.text(x, 6.23, 3.6, 0.52, label, size=16)

    d.start(
        3,
        "1 · Архитектура",
        "Четыре границы, одна кодовая база",
        "Внешние LLM и runtime API исключены условием кейса",
    )
    services = [
        ("01", "ingest", "Тикеты, версии, курсор\nИзоляция сбоев источника"),
        ("02", "media", "Текст, ASR и VL\nТяжёлые файлы отдельно"),
        ("03", "analysis", "Потребности и группы\nБюджет model-вызовов"),
        ("04", "delivery", "Карточка → reviewer\nOutbox → backlog"),
    ]
    for i, (number, title, text) in enumerate(services):
        d.card(0.68 + i * 3.12, 2.65, 2.9, 2.75, number, title, text)
    d.rect(0.7, 5.67, 7.3, 0.8, NAVY, radius=True)
    d.text(
        0.87,
        5.87,
        6.96,
        0.5,
        "PostgreSQL: данные + очередь + RLS + лимиты",
        size=20,
        fill=WHITE,
        align="center",
    )
    d.rect(8.25, 5.67, 4.35, 0.8, TEAL, radius=True)
    d.text(8.4, 5.87, 4.05, 0.5, "Qwen 3.8 / ASR / VL", size=20, fill=WHITE, align="center")
    d.text(
        0.73,
        6.66,
        12,
        0.3,
        "Кеш, инкрементальный импорт и сохранённый extraction уменьшают повторную работу.",
        size=14,
        fill=MUTED,
    )

    d.start(
        4,
        "2 · Tools и доступ",
        "Модель предлагает. Workflow проверяет.",
        "Идентификаторы продукта и права берутся из доверенного контекста",
    )
    tools = [
        ("Читать", "list_ticket_changes · read_ticket_content"),
        ("Найти", "find_similar_groups · read_product_context"),
        ("Собрать", "build_digest: частоты вычисляет код"),
        ("Согласовать", "record_product_decision: только reviewer"),
    ]
    for i, (title, label) in enumerate(tools):
        y = 2.65 + i * 0.89
        d.rect(0.7, y, 7.5, 0.72, PALE, radius=True)
        d.text(0.88, y + 0.16, 1.75, 0.4, title, size=18, bold=True, fill=TEAL)
        d.text(2.7, y + 0.17, 5.28, 0.4, label, size=15)
    d.rect(8.55, 2.65, 4.05, 3.4, NAVY, radius=True)
    d.text(8.8, 2.9, 3.55, 0.6, "Два уровня защиты", size=21, fill=WHITE, bold=True)
    d.text(
        8.8,
        3.8,
        3.55,
        1.9,
        "JWT + серверные grants + RLS\n\nМодель без write-tools; запись после решения человека",
        size=18,
        fill=WHITE,
    )
    d.text(
        0.74,
        6.46,
        11.8,
        0.45,
        "Обновление источника инвалидирует карточку; старое согласование не даёт права отправить новую версию.",
        size=16,
        fill=MUTED,
    )

    d.start(
        5,
        "3 · Промпт",
        "Одна тема ещё не означает одну потребность",
        "Недоверенный текст → строгий JSON → проверка цитат и разрешённых ID",
    )
    for x, label, quote, outcome in [
        (0.7, "ПОЖЕЛАНИЕ", "«Добавьте экспорт в CSV»", "feature_request\nНовая возможность"),
        (6.85, "ДЕФЕКТ", "«Экспорт в CSV перестал работать»", "defect\nСуществующая функция сломана"),
    ]:
        d.rect(x, 2.7, 5.75, 2.2, PALE, radius=True)
        d.text(x + 0.2, 2.89, 5.35, 0.35, label, size=13, fill=TEAL, bold=True)
        d.text(x + 0.2, 3.38, 5.35, 0.8, quote, size=23, bold=True)
        d.text(x + 0.2, 4.27, 5.35, 0.5, outcome.replace("\n", " · "), size=16)
    d.rect(0.7, 5.17, 11.9, 1.27, NAVY, radius=True)
    d.text(
        0.94,
        5.37,
        11.4,
        0.9,
        "Правила промпта: только переданные сегменты; 1–3 точные цитаты; смешанные запросы разделить; причины и выгоду не выдумывать; при сомнении — needs_review / uncertain.",
        size=18,
        fill=WHITE,
    )
    d.text(
        0.74,
        6.65,
        12,
        0.3,
        "Один повтор для исправления формата. Невалидный ответ не попадает в доставку.",
        size=14,
        fill=MUTED,
    )

    d.start(
        6,
        "Нагрузка · сценарий, не замер Qwen",
        "Пиковый поток требует проверки capacity",
        "1 000 обращений/сутки, пик ×3; 12 вызовов/тикет по 10 с — верхняя оценка",
    )
    rows = [
        ("Обычный поток", 12000, TEAL),
        ("Пик ×3", 36000, ORANGE),
        ("Ёмкость: 2 слота", 17280, NAVY),
        ("Ёмкость: 6 слотов", 51840, "607D94"),
    ]
    for i, (label, value, fill) in enumerate(rows):
        y = 2.75 + i * 0.76
        d.text(0.73, y + 0.04, 3.1, 0.45, label, size=18)
        width = value / 51840 * 6.55
        d.rect(3.85, y, width, 0.4, fill, radius=True)
        d.text(3.95 + width, y + 0.02, 1.65, 0.4, f"{value:,}".replace(",", " "), size=17, bold=True)
    d.text(
        0.75,
        5.92,
        11.8,
        0.65,
        "ASR: 1 000 мин/сутки × RTF 0,25 = 250 слот-минут. VL: 100 стр. × 3 с = 300 с. На пике — ×3. При 6 LLM-слотах загрузка пика ≈69%.",
        size=17,
    )
    d.text(
        0.75,
        6.72,
        11.8,
        0.25,
        "Проверено: очередь 3 000 заданий / 36,295 с. Это синтетический тест очереди, без реальных GPU и Qwen.",
        size=12,
        fill=MUTED,
    )

    d.start(
        7,
        "4 · Тестирование и релиз",
        "Локальный MVP проверен; впереди пилот",
        "Фактические результаты отделены от условий корпоративной приёмки",
    )
    gates = [
        ("86 / 86", "Код и протокол", "Тесты прошли 12.11\nПовторы, RLS, TTL, HTTPS/JWT", GREEN),
        ("≥90% / ≥80%", "Качество ИИ", "Precision / recall\nНезависимый gold ≥400 origin", MUTED),
        ("≥95% / 24 ч", "Реальная нагрузка", "Полный охват новых данных\nРеальные модели и медиа", MUTED),
        ("Приёмка", "Бизнес и ИБ", "Чистая экономия времени\nДоступ, backup и rollback", MUTED),
    ]
    for i, (number, title, text, accent) in enumerate(gates):
        d.card(0.68 + i * 3.12, 2.7, 2.9, 3.1, number, title, text, accent=accent)
    d.text(
        0.75,
        6.16,
        11.8,
        0.65,
        "Demo: 40 синтетических тикетов → 6 карточек → 2 локальные доставки. Реальное качество Qwen, медиа-нагрузка и 24-часовой SLO ещё не измерены.",
        size=18,
        fill=MUTED,
    )

    d.start(
        8,
        "5 · Метрики",
        "Меньше ручной работы — при сохранении качества",
        "Основной эффект: часы baseline минус вся работа после внедрения, включая review",
    )
    metrics = [
        ("Время", "часы на 1 000 origin\nи на аналитический цикл"),
        ("Качество", "precision / recall\nчистота групп + цитаты"),
        ("Полнота", "complete / принятые\nохват источника — отдельно"),
        ("Передача", "задержка от 2-го origin\nдо карточки и решения"),
    ]
    for i, (title, text) in enumerate(metrics):
        x = 0.7 + 3.12 * i
        d.rect(x, 2.7, 2.9, 1.73, PALE, radius=True)
        d.text(x + 0.15, 2.91, 2.6, 0.4, title, size=22, fill=TEAL, bold=True)
        d.text(x + 0.15, 3.5, 2.6, 0.8, text, size=16)
    d.rect(0.7, 4.85, 5.68, 0.78, NAVY, radius=True)
    d.text(0.86, 5.03, 5.36, 0.4, "4 недели: замер ручного процесса", size=19, fill=WHITE, align="center")
    d.arrow(6.4, 5.06, 0.4)
    d.rect(6.85, 4.85, 5.75, 0.78, TEAL, radius=True)
    d.text(7.0, 5.03, 5.45, 0.4, "4 недели: shadow / пилот", size=19, fill=WHITE, align="center")
    d.text(
        0.75,
        5.99,
        11.8,
        0.82,
        "Ретроспектива — из годовой истории и журналов. Нет времени в логах → хронометраж; нет разметки → качество не измерено; нет source count → охват источника неизвестен. Контрольная группа помогает отделить эффект агента.",
        size=17,
    )

    d.start(
        9,
        "6 · Финансовая модель",
        "Потолок вложений на 3 года: сценарии",
        "Высвобождённые часы становятся денежным эффектом только после подтверждения реализации",
    )
    for i, (label, value, fill) in enumerate(
        [("Сдержанный", 154125, "9FB6C8"), ("Базовый", 1042912.5, TEAL), ("Высокий", 2766543.75, ORANGE)]
    ):
        y = 2.65 + i * 0.87
        d.text(0.75, y + 0.06, 2.2, 0.4, label, size=18)
        width = value / 2766543.75 * 5.5
        d.rect(3.0, y, width, 0.43, fill, radius=True)
        d.text(
            3.15 + width,
            y + 0.02,
            1.55,
            0.5,
            f"{value / 1000000:.3f} млн".replace(".", ","),
            size=18,
            bold=True,
        )
    d.rect(10.2, 2.65, 2.4, 2.38, PALE, radius=True)
    d.text(10.39, 2.88, 2.02, 0.65, "69,6 ч/мес.", size=25, fill=TEAL, bold=True)
    d.text(10.39, 3.7, 2.02, 1.07, "База: 584 640 руб./год\nu = 70%", size=18)
    d.text(
        0.76,
        5.36,
        11.8,
        0.69,
        "Imax = Σ Bt/(1+r)^t; B1=0,5E, B2=1,05E, B3=1,05²E; r=20%. При условных I0=800 тыс. руб. базовый NPV=243 тыс. руб.; дисконтированный возврат — до конца 3-го года.",
        size=18,
    )
    d.text(
        0.76,
        6.23,
        11.8,
        0.44,
        "Все входы — допущения. ЦБ: ставка 14%, инфляция 6,4% за сентябрь; r=20% и рост 5% — сценарии команды.",
        size=14,
        fill=MUTED,
    )
    d.text(
        0.76,
        6.79,
        11.8,
        0.2,
        "Источник макроданных: Банк России, https://www.cbr.ru/dkp/ · проверено 13.11.2026",
        size=10,
        fill=MUTED,
    )

    d.start(
        10,
        "Решение о внедрении",
        "Сначала подтвердить эффект на пилоте",
        "Масштабирование — после качества, мощности, безопасности и финансовой приёмки",
        dark=True,
    )
    for i, (title, text) in enumerate(
        [
            ("Измерить", "Поток и ручной baseline\nQwen / ASR / VL на gold"),
            ("Сопоставить", "Ручной процесс и правила\nСтоимость капитала"),
            ("Принять", "Бюджет ≤ Imax\nВладелец продукта + ИБ"),
        ]
    ):
        x = 0.7 + i * 4.18
        d.rect(x, 2.8, 3.95, 2.05, "23425E", radius=True)
        d.text(x + 0.2, 3.05, 3.55, 0.5, title, size=24, fill="63CDD0", bold=True)
        d.text(x + 0.2, 3.83, 3.55, 0.8, text, size=18, fill=WHITE)
    d.text(
        0.76,
        5.23,
        11.8,
        0.98,
        "Если SQL/правила дают 40% эффекта, их потолок — 417 тыс. руб. Максимальная доплата за агента — 626 тыс. руб. при тех же сценарных входах. Фактический выбор — по подтверждённому NPV.",
        size=19,
        fill=WHITE,
    )
    d.text(
        0.76,
        6.36,
        11.8,
        0.43,
        "Нет подтверждённой монетизации часов → показываем часы, денежный эффект = 0.",
        size=17,
        fill="A7C6D9",
    )
    d.save()
    return d


def finance_snapshot():
    results = []
    for name, h, coverage, saving, review, realization in [
        ("conservative", "2", ".7", ".4", "8", ".5"),
        ("base", "4", ".85", ".6", "12", ".7"),
        ("high", "6", ".95", ".7", "16", ".9"),
    ]:
        h, coverage, saving, review, realization = map(Decimal, [h, coverage, saving, review, realization])
        hours = 40 * h * coverage * saving - review
        annual = 12 * hours * 1000 * realization
        flows = [annual / 2, annual * Decimal("1.05"), annual * Decimal("1.05") ** 2]
        ceiling = sum(flow / Decimal("1.2") ** t for t, flow in enumerate(flows, 1))
        results.append(
            {
                "scenario": name,
                "net_hours_month": float(hours),
                "steady_annual_benefit_rub": float(annual),
                "year_end_flows_rub": list(map(float, flows)),
                "three_year_discounted_ceiling_rub": round(float(ceiling), 2),
            }
        )
    return results


def validate(data, deck):
    doc = Document(FINAL / "Решение.docx")
    body = [p.text for p in doc.paragraphs]
    body += [cell.text for table in doc.tables for row in table.rows for cell in row.cells]
    # Conservative: spaces, paragraph/cell separators, header/footer repeated on every PDF page.
    count_body = len("\n".join(body))
    pdf = PdfReader(FINAL / "Решение.pdf")
    pdf_text = "\n".join(page.extract_text() for page in pdf.pages)
    count_docx = (
        count_body
        + sum(
            len(p.text) + 1
            for section in doc.sections
            for part in [section.header, section.footer]
            for p in part.paragraphs
        )
        * len(pdf.pages)
        + len(pdf.pages) * 2
    )
    conservative = max(count_docx, len(pdf_text))
    assert conservative <= 15000, f"Solution exceeds 15000: {conservative}"
    assert len(pdf.pages) == 5, f"Expected 5 pages, got {len(pdf.pages)}"
    assert len(deck.slides) == len(PdfReader(FINAL / "Презентация.pdf").pages) == 10
    assert len(Presentation(FINAL / "Презентация.pptx").slides) == 10
    assert len([p for p in doc.paragraphs if p.style.name == "Heading 1"]) == 5
    assert [page["title"][0] for page in data["pages"]] == ["1", "2", "3", "4", "6"]
    main_texts = "\n".join(body + deck.texts)
    urls = re.findall(r"https?://[^\s)]+", main_texts)
    assert all(url == "https://www.cbr.ru/dkp/" for url in urls), urls
    metrics = (FINAL / "Метрики.md").read_text(encoding="utf-8")
    assert metrics.startswith("# 5.")
    result = {
        "date": "2026-11-13",
        "case_pages": 7,
        "source": "User-supplied original PDF, read directly; source PDF not published",
        "solution": {
            "body_characters_with_spaces": count_body,
            "conservative_characters_with_spaces": conservative,
            "limit": 15000,
            "pdf_pages": len(pdf.pages),
            "body_font_pt": 12,
            "question_order": [1, 2, 3, 4, 6],
            "formats": ["docx", "pdf"],
        },
        "metrics": {"encoding": "UTF-8", "question": 5, "characters": len(metrics)},
        "presentation": {"slides": 10, "formats": ["pptx", "pdf"], "native_editable_shapes": True},
        "external_links_in_solution_and_slides": sorted(set(urls)),
        "finance": {"status": "assumptions, not observed company effects", "scenarios": finance_snapshot()},
        "visual_review": "Visual review recorded separately in Соответствие_кейсу.md; repeat after content changes",
        "not_verified": [
            "Company model quality, real media throughput and 24-hour SLO",
            "Company baseline, realized monetary effect and approved investment budget",
            "Organizer upload rules beyond the supplied PDF; oral defense readiness",
        ],
    }
    (ROOT / "submission/checks.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


def main():
    register_fonts()
    FINAL.mkdir(parents=True, exist_ok=True)
    data = json.loads((SOURCE / "solution.json").read_text(encoding="utf-8"))
    check_glyphs(json.dumps(data, ensure_ascii=False))
    build_solution_pdf(data)
    build_solution_docx(data)
    deck = build_deck()
    validate(data, deck)
    archive = ROOT / "submission/OpenCaseKillers_submission.zip"
    with ZipFile(archive, "w", compression=ZIP_DEFLATED) as package:
        for name in ["Решение.pdf", "Метрики.md", "Презентация.pptx"]:
            package.write(FINAL / name, arcname=name)
    with ZipFile(archive) as package:
        assert package.testzip() is None
        assert len(package.namelist()) == 3


if __name__ == "__main__":
    main()

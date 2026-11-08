"""Build the Russian project report with embedded Cyrillic fonts and selectable text."""

import argparse
import re
from html import escape
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from reportlab.platypus import (
    Flowable,
    PageBreak,
    Paragraph,
    Preformatted,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

ROOT = Path(__file__).resolve().parents[1]
NAVY = colors.HexColor("#142C46")
BLUE = colors.HexColor("#1769B5")
TEAL = colors.HexColor("#0E8A89")
INK = colors.HexColor("#233449")
MUTED = colors.HexColor("#5D6E80")
PALE = colors.HexColor("#EEF4FA")
BORDER = colors.HexColor("#D8E3ED")
WIDTH = A4[0] - 88


def register_fonts(font_dir=None):
    paths = [Path(font_dir)] if font_dir else []
    paths += [
        Path("/usr/share/fonts/truetype/dejavu"),
        Path("/usr/share/fonts/dejavu-sans-fonts"),
        Path("/opt/anaconda3/lib/python3.12/site-packages/matplotlib/mpl-data/fonts/ttf"),
        Path("/Library/Fonts"),
        Path.home() / "Library/Fonts",
    ]
    for path in paths:
        filenames = ["DejaVuSans.ttf", "DejaVuSans-Bold.ttf", "DejaVuSansMono.ttf"]
        if all((path / name).is_file() for name in filenames):
            for name, filename in zip(["Report", "Report-Bold", "Report-Mono"], filenames):
                pdfmetrics.registerFont(TTFont(name, str(path / filename)))
            pdfmetrics.registerFontFamily(
                "Report", normal="Report", bold="Report-Bold", italic="Report", boldItalic="Report-Bold"
            )
            return
    raise SystemExit("Install DejaVu Sans or pass --font-dir with regular, bold and mono TTF files")


def rich(text):
    text = escape(text)
    text = re.sub(r"`([^`]+)`", r'<font name="Report-Mono" size="8.1">\1</font>', text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", text)
    text = re.sub(
        r'(?<![="\w])(https?://[^\s<>]+)',
        lambda m: f'<link href="{m.group(1)}" color="#1769B5">{m.group(1)}</link>',
        text,
    )
    return text


class Pipeline(Flowable):
    def __init__(self):
        super().__init__()
        self.width, self.height = WIDTH, 110

    def draw(self):
        c, gap = self.canv, 9
        width = (self.width - gap * 5) / 6
        labels = ["Обращения", "Нормализация", "Потребности", "Группы", "Карточки", "Согласование"]
        for i, label in enumerate(labels):
            x = i * (width + gap)
            c.setFillColor(NAVY if i == 5 else PALE)
            c.setStrokeColor(BORDER)
            c.roundRect(x, 35, width, 57, 7, fill=1, stroke=1)
            c.setFont("Report-Bold", 8)
            c.setFillColor(colors.white if i == 5 else BLUE)
            c.drawCentredString(x + width / 2, 72, f"{i + 1:02}")
            c.setFont("Report", 7.3)
            c.drawCentredString(x + width / 2, 48, label)
            if i < 5:
                c.setStrokeColor(TEAL)
                c.line(x + width + 1, 63, x + width + gap - 1, 63)
                c.line(x + width + gap - 4, 66, x + width + gap - 1, 63)
                c.line(x + width + gap - 4, 60, x + width + gap - 1, 63)
        c.setFont("Report", 8)
        c.setFillColor(MUTED)
        c.drawString(0, 12, "Источники и версия → проверенные факты → решение владельца → outbox")


class ResultTiles(Flowable):
    def __init__(self):
        super().__init__()
        self.width, self.height = WIDTH, 83

    def draw(self):
        c = self.canv
        gap, width = 14, (self.width - 28) / 3
        for i, (value, label) in enumerate(
            [("40", "синтетических обращений"), ("6", "карточек"), ("2", "локальные доставки")]
        ):
            x = i * (width + gap)
            c.setFillColor(PALE)
            c.roundRect(x, 6, width, 72, 7, fill=1, stroke=0)
            c.setFillColor(BLUE)
            c.setFont("Report-Bold", 25)
            c.drawString(x + 13, 39, value)
            c.setFillColor(INK)
            c.setFont("Report", 7.7)
            c.drawString(x + 13, 20, label)


class PagedCanvas(canvas.Canvas):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.pages = []

    def showPage(self):
        self.pages.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        total = len(self.pages)
        for state in self.pages:
            self.__dict__.update(state)
            self.setStrokeColor(BORDER)
            self.setLineWidth(0.5)
            self.line(44, 39, A4[0] - 44, 39)
            self.setFillColor(MUTED)
            self.setFont("Report", 7.5)
            self.drawString(44, 24, "OpenCaseKillers • Технический отчёт • 08.11.2026")
            self.drawRightString(A4[0] - 44, 24, f"{self._pageNumber} / {total}")
            super().showPage()
        super().save()


def header(c, doc):
    c.saveState()
    if doc.page == 1:
        c.setFillColor(NAVY)
        c.rect(0, A4[1] - 16, A4[0], 16, fill=1, stroke=0)
    else:
        c.setFillColor(BLUE)
        c.setFont("Report-Bold", 8)
        c.drawString(44, A4[1] - 34, "OPENCASEKILLERS")
        c.setFont("Report", 7.5)
        c.setFillColor(MUTED)
        c.drawRightString(A4[0] - 44, A4[1] - 34, "Устройство • Технологии • Запуск")
        c.setStrokeColor(BORDER)
        c.line(44, A4[1] - 43, A4[0] - 44, A4[1] - 43)
    c.restoreState()


def styles():
    base = dict(
        fontName="Report",
        fontSize=9.3,
        leading=13.5,
        textColor=INK,
        spaceAfter=7,
        alignment=TA_LEFT,
        splitLongWords=True,
    )
    return {
        "body": ParagraphStyle("body", **base),
        "title": ParagraphStyle(
            "title",
            fontName="Report-Bold",
            fontSize=33,
            leading=40,
            textColor=NAVY,
            spaceBefore=20,
            spaceAfter=15,
        ),
        "subtitle": ParagraphStyle(
            "subtitle", fontName="Report-Bold", fontSize=18, leading=25, textColor=BLUE, spaceAfter=20
        ),
        "h2": ParagraphStyle(
            "h2",
            fontName="Report-Bold",
            fontSize=17,
            leading=23,
            textColor=NAVY,
            spaceAfter=13,
            keepWithNext=True,
        ),
        "h3": ParagraphStyle(
            "h3",
            fontName="Report-Bold",
            fontSize=10.7,
            leading=15,
            textColor=BLUE,
            spaceBefore=8,
            spaceAfter=6,
            keepWithNext=True,
        ),
        "cell": ParagraphStyle(
            "cell",
            fontName="Report",
            fontSize=8.1,
            leading=11.1,
            textColor=INK,
            spaceAfter=0,
            splitLongWords=True,
        ),
        "thead": ParagraphStyle(
            "thead", fontName="Report-Bold", fontSize=8.1, leading=11.1, textColor=colors.white, spaceAfter=0
        ),
        "code": ParagraphStyle(
            "code",
            fontName="Report-Mono",
            fontSize=7.3,
            leading=10.6,
            textColor=NAVY,
            backColor=PALE,
            borderPadding=10,
            spaceBefore=4,
            spaceAfter=13,
        ),
        "list": ParagraphStyle("list", **(base | {"leftIndent": 9, "spaceAfter": 5})),
    }


def make_table(lines, st):
    rows = [[cell.strip() for cell in line.strip().strip("|").split("|")] for line in lines]
    rows = [row for row in rows if not all(re.fullmatch(r":?-+:?", cell) for cell in row)]
    count = len(rows[0])
    widths = [WIDTH * 0.31, WIDTH * 0.69] if count == 2 else [WIDTH * 0.16, WIDTH * 0.51, WIDTH * 0.33]
    if count == 2 and rows[0][0] == "HTTP API":
        widths = [WIDTH * 0.54, WIDTH * 0.46]
    data = [
        [Paragraph(rich(cell), st["thead"] if i == 0 else st["cell"]) for cell in row]
        for i, row in enumerate(rows)
    ]
    table = Table(data, colWidths=widths, repeatRows=1, hAlign="LEFT")
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), NAVY),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, PALE]),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                ("TOPPADDING", (0, 0), (-1, -1), 7),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
                ("LINEBELOW", (0, 0), (-1, 0), 0.7, NAVY),
                ("LINEBELOW", (0, 1), (-1, -1), 0.3, BORDER),
            ]
        )
    )
    return [table, Spacer(1, 11)]


def parse_section(section, st, cover=False):
    lines, story, i = section.strip().splitlines(), [], 0
    while i < len(lines):
        line = lines[i].strip()
        if not line:
            i += 1
            continue
        if line.startswith("```"):
            language, code = line[3:], []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code.append(lines[i])
                i += 1
            if language == "mermaid":
                story.extend([Spacer(1, 7), Pipeline(), ResultTiles(), Spacer(1, 14)])
            else:
                longest = max((pdfmetrics.stringWidth(row, "Report-Mono", 7.3) for row in code), default=0)
                if longest > WIDTH - 22:
                    raise ValueError(f"Code line exceeds PDF width: {longest:.1f} pt")
                story.append(Preformatted("\n".join(code), st["code"]))
            i += 1
            continue
        if line.startswith("|"):
            table = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                table.append(lines[i])
                i += 1
            story.extend(make_table(table, st))
            continue
        heading = re.match(r"^(#{1,3})\s+(.+)$", line)
        if heading:
            level, text = len(heading.group(1)), heading.group(2)
            name = "title" if level == 1 else ("subtitle" if cover and level == 2 else f"h{level}")
            story.append(Paragraph(rich(text), st[name]))
            i += 1
            continue
        if re.match(r"^\d+\.\s|^-\s", line):
            story.append(Paragraph(rich(line), st["list"]))
            i += 1
            continue
        paragraph = [line]
        i += 1
        while (
            i < len(lines) and lines[i].strip() and not re.match(r"^(#|\||```|\d+\.\s|-\s)", lines[i].strip())
        ):
            paragraph.append(lines[i].strip())
            i += 1
        story.append(Paragraph(rich(" ".join(paragraph)), st["body"]))
    return story


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--font-dir", type=Path)
    parser.add_argument("--source", type=Path, default=ROOT / "docs/report_ru.md")
    parser.add_argument("--output", type=Path, default=ROOT / "docs/OpenCaseKillers_Report_RU.pdf")
    args = parser.parse_args()
    register_fonts(args.font_dir)
    sections = args.source.read_text().split("\n---\n")
    st, story = styles(), []
    for index, section in enumerate(sections):
        if index:
            story.append(PageBreak())
        story.extend(parse_section(section, st, cover=index == 0))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    doc = SimpleDocTemplate(
        str(args.output),
        pagesize=A4,
        leftMargin=44,
        rightMargin=44,
        topMargin=64,
        bottomMargin=54,
        title="OpenCaseKillers — технический отчёт",
        author="OpenCaseKillers",
        subject="Архитектура, технологии и запуск аналитического ИИ-агента",
        pageCompression=1,
    )
    doc.build(story, onFirstPage=header, onLaterPages=header, canvasmaker=PagedCanvas)
    print(
        f"PDF created: {args.output.relative_to(ROOT) if args.output.is_relative_to(ROOT) else args.output}"
    )


if __name__ == "__main__":
    main()

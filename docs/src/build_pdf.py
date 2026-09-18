#!/usr/bin/env python3
"""Render the project's formal documentation sources (a Markdown subset) to PDF.

Usage:
    python docs/src/build_pdf.py

Produces ``docs/TECHNICAL_DOCUMENTATION.pdf`` and ``docs/USER_MANUAL.pdf`` from the
Markdown sources in this directory. The supported subset is deliberately small and
fully deterministic: headings (levels 1 to 4), paragraphs, bullet and numbered lists,
pipe tables, fenced code blocks, block quotes (rendered as call-outs), horizontal
rules and the directives ``[[pagebreak]]`` and ``[[keep:NN]]``.

Requires reportlab 4 or newer (``pip install reportlab``).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, StyleSheet1
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    BaseDocTemplate,
    CondPageBreak,
    Flowable,
    Frame,
    KeepTogether,
    NextPageTemplate,
    PageBreak,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
    XPreformatted,
)
from reportlab.platypus.doctemplate import IndexingFlowable
from reportlab.platypus.tableofcontents import TableOfContents

# ---------------------------------------------------------------------------------------
# House style
# ---------------------------------------------------------------------------------------

ACCENT = colors.HexColor("#1F3864")
ACCENT_LIGHT = colors.HexColor("#E8ECF4")
INK = colors.HexColor("#1A1A1A")
MUTED = colors.HexColor("#5A6274")
RULE = colors.HexColor("#C9CEDA")
ZEBRA = colors.HexColor("#F6F7FA")
CODE_BG = colors.HexColor("#F3F4F7")
NOTE_BG = colors.HexColor("#EEF3FB")

PAGE_SIZE = A4
MARGIN_L = 22 * mm
MARGIN_R = 20 * mm
MARGIN_T = 24 * mm
MARGIN_B = 22 * mm
# Usable height of the body frame. A flowable taller than this cannot be placed whole, and
# reportlab raises LayoutError rather than splitting a table row, so oversized blocks opt in to
# splitting. Blocks that fit are left alone, which keeps pagination exactly as laid out.
FRAME_HEIGHT = PAGE_SIZE[1] - MARGIN_T - MARGIN_B - 5 * mm

FONT_DIRS = ("/System/Library/Fonts/Supplemental", "/Library/Fonts", "/usr/share/fonts/truetype/dejavu")
FONT_CANDIDATES = {
    "Body": ("Arial.ttf", "DejaVuSans.ttf", "LiberationSans-Regular.ttf"),
    "Body-Bold": ("Arial Bold.ttf", "DejaVuSans-Bold.ttf", "LiberationSans-Bold.ttf"),
    "Body-Italic": ("Arial Italic.ttf", "DejaVuSans-Oblique.ttf", "LiberationSans-Italic.ttf"),
    "Body-BoldItalic": ("Arial Bold Italic.ttf", "DejaVuSans-BoldOblique.ttf", "LiberationSans-BoldItalic.ttf"),
    "Mono": ("Courier New.ttf", "DejaVuSansMono.ttf", "LiberationMono-Regular.ttf"),
    "Mono-Bold": ("Courier New Bold.ttf", "DejaVuSansMono-Bold.ttf", "LiberationMono-Bold.ttf"),
}
FALLBACK = {
    "Body": "Helvetica",
    "Body-Bold": "Helvetica-Bold",
    "Body-Italic": "Helvetica-Oblique",
    "Body-BoldItalic": "Helvetica-BoldOblique",
    "Mono": "Courier",
    "Mono-Bold": "Courier-Bold",
}


def register_fonts() -> dict[str, str]:
    """Register the preferred TrueType faces, falling back to the built-in Type 1 fonts."""
    resolved: dict[str, str] = {}
    for name, candidates in FONT_CANDIDATES.items():
        path = next((p for c in candidates for p in (Path(d) / c for d in FONT_DIRS) if p.exists()), None)
        if path is None:
            resolved[name] = FALLBACK[name]
            continue
        try:
            pdfmetrics.registerFont(TTFont(name, str(path)))
            resolved[name] = name
        except Exception:  # noqa: BLE001 - a broken font file must not break the build
            resolved[name] = FALLBACK[name]
    pdfmetrics.registerFontFamily(
        resolved["Body"],
        normal=resolved["Body"],
        bold=resolved["Body-Bold"],
        italic=resolved["Body-Italic"],
        boldItalic=resolved["Body-BoldItalic"],
    )
    return resolved


FONTS = register_fonts()
F_BODY, F_BOLD, F_ITALIC = FONTS["Body"], FONTS["Body-Bold"], FONTS["Body-Italic"]
F_MONO = FONTS["Mono"]


def stylesheet() -> StyleSheet1:
    ss = StyleSheet1()
    ss.add(ParagraphStyle("Body", fontName=F_BODY, fontSize=9.6, leading=14.2, textColor=INK, spaceAfter=7, alignment=TA_LEFT))
    ss.add(ParagraphStyle("H1", fontName=F_BOLD, fontSize=17, leading=21, textColor=ACCENT, spaceBefore=0, spaceAfter=3))
    ss.add(ParagraphStyle("H2", fontName=F_BOLD, fontSize=12.4, leading=16, textColor=ACCENT, spaceBefore=15, spaceAfter=5))
    ss.add(ParagraphStyle("H3", fontName=F_BOLD, fontSize=10.6, leading=14, textColor=colors.HexColor("#2C3A52"), spaceBefore=11, spaceAfter=3))
    ss.add(ParagraphStyle("H4", fontName=F_ITALIC, fontSize=9.8, leading=13, textColor=colors.HexColor("#2C3A52"), spaceBefore=9, spaceAfter=2))
    ss.add(ParagraphStyle("Bullet", parent=ss["Body"], spaceAfter=3, leading=13.6))
    ss.add(
        ParagraphStyle(
            "Bullet1",
            parent=ss["Body"],
            leading=13.8,
            spaceAfter=3.5,
            leftIndent=15,
            bulletIndent=3,
            bulletFontName=F_BODY,
            bulletFontSize=9.6,
            bulletColor=ACCENT,
        )
    )
    ss.add(ParagraphStyle("Bullet2", parent=ss["Bullet1"], leftIndent=29, bulletIndent=17, fontSize=9.3, leading=13.2))
    ss.add(ParagraphStyle("Number1", parent=ss["Bullet1"], bulletFontName=F_BOLD, bulletFontSize=9.4, leftIndent=18, bulletIndent=3))
    ss.add(ParagraphStyle("Code", fontName=F_MONO, fontSize=8.2, leading=11.4, textColor=colors.HexColor("#22252B")))
    ss.add(ParagraphStyle("TableCell", fontName=F_BODY, fontSize=8.6, leading=11.6, textColor=INK))
    ss.add(ParagraphStyle("TableHead", fontName=F_BOLD, fontSize=8.6, leading=11.6, textColor=colors.white))
    ss.add(ParagraphStyle("Note", parent=ss["Body"], fontSize=9.3, leading=13.4, spaceAfter=0))
    ss.add(ParagraphStyle("CoverTitle", fontName=F_BOLD, fontSize=29, leading=34, textColor=ACCENT, alignment=TA_LEFT))
    ss.add(ParagraphStyle("CoverSub", fontName=F_BODY, fontSize=13, leading=18.5, textColor=MUTED, alignment=TA_LEFT))
    ss.add(ParagraphStyle("CoverMeta", fontName=F_BODY, fontSize=9.2, leading=13, textColor=INK))
    ss.add(ParagraphStyle("CoverMetaKey", fontName=F_BOLD, fontSize=9.2, leading=13, textColor=ACCENT))
    ss.add(ParagraphStyle("TOCTitle", parent=ss["H1"], spaceAfter=10))
    ss.add(ParagraphStyle("TOC0", fontName=F_BOLD, fontSize=10, leading=14, leftIndent=0, spaceBefore=5, textColor=ACCENT))
    ss.add(ParagraphStyle("TOC1", fontName=F_BODY, fontSize=9, leading=11.8, leftIndent=15, spaceBefore=0, textColor=INK))
    ss.add(ParagraphStyle("TOC2", fontName=F_BODY, fontSize=8.6, leading=11.2, leftIndent=32, spaceBefore=0, textColor=MUTED))
    return ss


STYLES = stylesheet()

# ---------------------------------------------------------------------------------------
# Inline formatting
# ---------------------------------------------------------------------------------------

# A line that begins a different block: a list must end here rather than swallow it as a
# continuation line, which would silently drop the heading, table or code block that follows.
_BLOCK_START_RE = re.compile(r"^(#{1,6}\s|```|\||>\s|---\s*$|\[\[)")
_CODE_RE = re.compile(r"`([^`]+)`")
_BOLD_RE = re.compile(r"\*\*([^*]+)\*\*")
_ITALIC_RE = re.compile(r"(?<![\*\w])\*([^*\n]+)\*(?!\*)")


def inline(text: str) -> str:
    """Convert the supported inline Markdown to reportlab's mini-HTML."""
    out = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    out = _CODE_RE.sub(lambda m: f'<font face="{F_MONO}" size="8.4" color="#333A47">{m.group(1)}</font>', out)
    out = _BOLD_RE.sub(r"<b>\1</b>", out)
    out = _ITALIC_RE.sub(r"<i>\1</i>", out)
    return out


# ---------------------------------------------------------------------------------------
# Flowables
# ---------------------------------------------------------------------------------------


class Heading(Paragraph):
    """A heading that registers itself with the table of contents and the PDF outline."""

    def __init__(self, text: str, level: int, number: str, key: str):
        style = STYLES[f"H{min(level, 4)}"]
        super().__init__(f"{number}&nbsp;&nbsp;{inline(text)}" if number else inline(text), style)
        self.toc_level = level
        self.toc_text = f"{number}  {text}" if number else text
        self.toc_key = key


class HRule(Flowable):
    def __init__(self, width_ratio: float = 1.0, thickness: float = 0.7, color=RULE, space_after: float = 8):
        super().__init__()
        self.width_ratio, self.thickness, self.color, self.space_after = width_ratio, thickness, color, space_after

    def wrap(self, avail_w, avail_h):
        self.width = avail_w * self.width_ratio
        return self.width, self.thickness + self.space_after

    def draw(self):
        self.canv.setStrokeColor(self.color)
        self.canv.setLineWidth(self.thickness)
        self.canv.line(0, self.space_after, self.width, self.space_after)


def _split_if_taller_than_page(content_height: float) -> int:
    """1 when a single-cell block must be allowed to break across pages, else 0."""
    return 1 if content_height > FRAME_HEIGHT else 0


def note_block(lines: list[str]) -> Table:
    """A call-out box: tinted background with an accent bar on the left."""
    inner = Table([[Paragraph(inline(line), STYLES["Note"])] for line in lines], colWidths=["100%"])
    inner.setStyle(
        TableStyle(
            [
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 1),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
            ]
        )
    )
    box = Table(
        [[inner]],
        colWidths=["100%"],
        splitInRow=_split_if_taller_than_page(len(lines) * STYLES["Note"].leading + 14),
    )
    box.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), NOTE_BG),
                ("LINEBEFORE", (0, 0), (0, -1), 2.4, ACCENT),
                ("LEFTPADDING", (0, 0), (-1, -1), 9),
                ("RIGHTPADDING", (0, 0), (-1, -1), 9),
                ("TOPPADDING", (0, 0), (-1, -1), 7),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]
        )
    )
    box.spaceBefore, box.spaceAfter = 6, 9
    return box


def code_block(lines: list[str]) -> Table:
    text = "\n".join(lines) if lines else " "
    pre = XPreformatted(text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"), STYLES["Code"])
    box = Table(
        [[pre]],
        colWidths=["100%"],
        splitInRow=_split_if_taller_than_page(len(lines) * STYLES["Code"].leading + 14),
    )
    box.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), CODE_BG),
                ("BOX", (0, 0), (-1, -1), 0.5, RULE),
                ("LEFTPADDING", (0, 0), (-1, -1), 9),
                ("RIGHTPADDING", (0, 0), (-1, -1), 9),
                ("TOPPADDING", (0, 0), (-1, -1), 7),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]
        )
    )
    box.spaceBefore, box.spaceAfter = 5, 9
    return box


def data_table(rows: list[list[str]], widths: list[float] | None, avail: float) -> Table:
    header, body = rows[0], rows[1:]
    ncols = len(header)
    if widths and len(widths) == ncols:
        total = sum(widths)
        col_widths = [avail * w / total for w in widths]
    else:
        weights = []
        for c in range(ncols):
            longest = max((len(r[c]) for r in rows if c < len(r)), default=1)
            weights.append(max(7.0, min(float(longest), 60.0)))
        total = sum(weights)
        col_widths = [avail * w / total for w in weights]
    data = [[Paragraph(inline(c), STYLES["TableHead"]) for c in header]]
    for row in body:
        padded = list(row) + [""] * (ncols - len(row))
        data.append([Paragraph(inline(c), STYLES["TableCell"]) for c in padded[:ncols]])
    table = Table(data, colWidths=col_widths, repeatRows=1, hAlign="LEFT")
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), ACCENT),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("GRID", (0, 0), (-1, -1), 0.4, RULE),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]
    for i in range(1, len(data)):
        if i % 2 == 0:
            style.append(("BACKGROUND", (0, i), (-1, i), ZEBRA))
    table.setStyle(TableStyle(style))
    table.spaceBefore, table.spaceAfter = 6, 10
    return table


# ---------------------------------------------------------------------------------------
# Markdown subset parser
# ---------------------------------------------------------------------------------------


@dataclass
class DocumentMeta:
    title: str
    subtitle: str
    system: str
    version: str
    date: str
    classification: str
    audience: str
    owner: str
    prepared_by: str
    status: str
    fields: list[tuple[str, str]] = field(default_factory=list)


class Parser:
    """Turn the Markdown subset into reportlab flowables and number the headings."""

    def __init__(self, avail_width: float):
        self.avail = avail_width
        self.counters = [0, 0, 0]
        self.key = 0

    def _number(self, level: int) -> str:
        if level > 3:
            return ""
        self.counters[level - 1] += 1
        for i in range(level, 3):
            self.counters[i] = 0
        return ".".join(str(self.counters[i]) for i in range(level))

    def parse(self, text: str) -> list:
        flow: list = []
        lines = text.replace("\r\n", "\n").split("\n")
        i, n = 0, len(lines)
        para: list[str] = []
        first_heading = True

        def flush() -> None:
            if para:
                flow.append(Paragraph(inline(" ".join(para)), STYLES["Body"]))
                para.clear()

        while i < n:
            line = lines[i]
            stripped = line.strip()

            if not stripped:
                flush()
                i += 1
                continue

            if stripped == "[[pagebreak]]":
                flush()
                flow.append(PageBreak())
                i += 1
                continue

            if stripped.startswith("[[keep:"):
                flush()
                flow.append(CondPageBreak(float(stripped[7:-2]) * mm))
                i += 1
                continue

            if stripped.startswith("```"):
                flush()
                i += 1
                block: list[str] = []
                while i < n and not lines[i].strip().startswith("```"):
                    block.append(lines[i])
                    i += 1
                i += 1
                flow.append(code_block(block))
                continue

            if stripped.startswith("#"):
                flush()
                level = len(stripped) - len(stripped.lstrip("#"))
                title = stripped[level:].strip()
                unnumbered = title.startswith("!")
                if unnumbered:
                    title = title[1:].strip()
                number = "" if unnumbered else self._number(level)
                self.key += 1
                heading = Heading(title, level, number, f"h{self.key}")
                if level == 1:
                    if not first_heading:
                        flow.append(PageBreak())
                    first_heading = False
                    flow.append(heading)
                    flow.append(HRule(thickness=1.1, color=ACCENT, space_after=2))
                    flow.append(Spacer(1, 6))
                else:
                    flow.append(KeepTogether([heading, Spacer(1, 1)]))
                i += 1
                continue

            if stripped.startswith("> "):
                flush()
                quote: list[str] = []
                while i < n and lines[i].strip().startswith("> "):
                    quote.append(lines[i].strip()[2:])
                    i += 1
                flow.append(note_block(quote))
                continue

            if stripped.startswith("|"):
                flush()
                widths = None
                rows: list[list[str]] = []
                while i < n and lines[i].strip().startswith("|"):
                    cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                    if all(c and set(c) <= set("-: ") for c in cells):
                        i += 1
                        continue
                    rows.append(cells)
                    i += 1
                if i < n and lines[i].strip().startswith("{widths:"):
                    widths = [float(x) for x in lines[i].strip()[8:-1].split(",")]
                    i += 1
                if rows:
                    flow.append(data_table(rows, widths, self.avail))
                continue

            if stripped == "---":
                flush()
                flow.append(HRule())
                i += 1
                continue

            if re.match(r"^([-*])\s+", stripped) or re.match(r"^\d+\.\s+", stripped):
                flush()
                items, i = self._collect_list(lines, i)
                flow.extend(items)
                continue

            para.append(stripped)
            i += 1

        flush()
        return flow

    def _collect_list(self, lines: list[str], start: int):
        """Collect one list block into paragraphs carrying their own bullet or number."""
        ordered = bool(re.match(r"^\d+\.\s+", lines[start].strip()))
        flow: list = []
        i = start
        current: list[str] = []
        pending_bullet = ""

        def close_item() -> None:
            nonlocal current, pending_bullet
            if not current:
                return
            style = STYLES["Number1"] if ordered else STYLES["Bullet1"]
            flow.append(Paragraph(inline(" ".join(current)), style, bulletText=pending_bullet))
            current = []
            pending_bullet = ""

        while i < len(lines):
            raw = lines[i]
            stripped = raw.strip()
            if not stripped:
                if i + 1 < len(lines) and re.match(r"^\s+([-*])\s+", lines[i + 1]):
                    i += 1
                    continue
                break
            indent = len(raw) - len(raw.lstrip())
            m_bullet = re.match(r"^([-*])\s+(.*)$", stripped)
            m_num = re.match(r"^(\d+)\.\s+(.*)$", stripped)
            if indent >= 2 and m_bullet:
                close_item()
                flow.append(Paragraph(inline(m_bullet.group(2)), STYLES["Bullet2"], bulletText="\u25e6"))
            elif m_bullet and not ordered:
                close_item()
                pending_bullet = "\u2022"
                current.append(m_bullet.group(2))
            elif m_num and ordered:
                # The number written in the source wins, so a list may continue after an
                # embedded screen sample or table without restarting at one.
                close_item()
                pending_bullet = f"{m_num.group(1)}."
                current.append(m_num.group(2))
            elif (m_bullet and ordered) or (m_num and not ordered):
                break
            elif _BLOCK_START_RE.match(stripped):
                break  # a heading, table, code block or quote ends the list
            else:
                current.append(stripped)  # continuation line of the current item
            i += 1
        close_item()
        if flow:
            flow.insert(0, Spacer(1, 2))
            flow.append(Spacer(1, 5))
        return flow, i


# ---------------------------------------------------------------------------------------
# Document template
# ---------------------------------------------------------------------------------------


def _roman(n: int) -> str:
    out, remaining = "", max(n, 1)
    for value, sym in ((10, "x"), (9, "ix"), (5, "v"), (4, "iv"), (1, "i")):
        while remaining >= value:
            out += sym
            remaining -= value
    return out


class BodyPageCounter(IndexingFlowable):
    """Zero-size story item that lets the body footer print "Page X of Y".

    The total is only known once the document has been laid out, but the footer is drawn during
    layout, so the two have to be reconciled across passes. ``multiBuild`` already repeats the
    build until every indexing flowable reports satisfaction (that is what resolves the table of
    contents), so this flowable joins that loop: it publishes the total it observed and stays
    unsatisfied until the total it published matches the total actually rendered. Two passes is
    the normal outcome.

    The obvious alternative, deferring ``showPage`` and stamping the total during a replay, is a
    trap: ``Canvas.showPage`` is what registers a page with the document, so every bookmark made
    while no page has been registered binds to page one, and silently breaks the whole table of
    contents and outline.
    """

    def __init__(self) -> None:
        super().__init__()
        self.total: int | None = None  # published to the footer for the current pass
        self._seen = 0                 # highest body page number rendered during the current pass
        self._satisfied = False

    # Flowable protocol: occupies no space, draws nothing.
    def wrap(self, avail_w, avail_h):
        return 0, 0

    def draw(self) -> None:
        pass

    def record(self, body_page_number: int) -> None:
        self._seen = max(self._seen, body_page_number)

    # IndexingFlowable protocol.
    def beforeBuild(self) -> None:
        self._seen = 0

    def afterBuild(self) -> None:
        self._satisfied = not self._seen or self._seen == self.total
        self.total = self._seen or self.total

    def isSatisfied(self) -> bool:
        return self._satisfied


class DocTemplate(BaseDocTemplate):
    """Three page templates: cover, front matter (roman numerals) and body (arabic)."""

    def __init__(self, path: str, meta: DocumentMeta, **kw):
        super().__init__(
            path,
            pagesize=PAGE_SIZE,
            leftMargin=MARGIN_L,
            rightMargin=MARGIN_R,
            topMargin=MARGIN_T,
            bottomMargin=MARGIN_B,
            title=meta.title,
            subject=meta.subtitle,
            author=meta.prepared_by,
            creator=meta.system,
            **kw,
        )
        self.meta = meta
        self.body_offset: int | None = None
        self.counter = BodyPageCounter()
        cover_frame = Frame(MARGIN_L, MARGIN_B, self.width, self.height, id="cover")
        body_frame = Frame(MARGIN_L, MARGIN_B, self.width, self.height - 5 * mm, id="body")
        self.addPageTemplates(
            [
                PageTemplate(id="Cover", frames=[cover_frame], onPage=self._cover_page),
                PageTemplate(id="Front", frames=[body_frame], onPage=self._front_page),
                PageTemplate(id="Body", frames=[body_frame], onPage=self._body_page),
            ]
        )

    def beforeDocument(self) -> None:
        # multiBuild runs several passes and the front matter grows between them, so the
        # offset that turns an absolute page number into a body page number must be
        # recomputed on every pass rather than kept from the first one.
        self.body_offset = None
        self._outline_level = -1
        self._front_labelled = False

    def _cover_page(self, canvas, doc) -> None:
        # Page labels make a viewer's page box read what the footers print (Cover, i, ii, 1, 2...)
        # instead of a raw 1..N that is off by the length of the front matter.
        canvas.addPageLabel(canvas.getPageNumber() - 1, style=None, prefix="Cover")
        canvas.showOutline()  # open the bookmark panel: this is a long document meant to be navigated
        canvas.saveState()
        canvas.setFillColor(ACCENT)
        canvas.rect(0, PAGE_SIZE[1] - 14 * mm, PAGE_SIZE[0], 14 * mm, stroke=0, fill=1)
        canvas.setFillColor(ACCENT_LIGHT)
        canvas.rect(0, 0, PAGE_SIZE[0], 9 * mm, stroke=0, fill=1)
        canvas.setFillColor(colors.white)
        canvas.setFont(F_BOLD, 9)
        canvas.drawString(MARGIN_L, PAGE_SIZE[1] - 9.4 * mm, self.meta.system.upper())
        canvas.setFillColor(MUTED)
        canvas.setFont(F_BODY, 8.4)
        canvas.drawString(MARGIN_L, 3.4 * mm, self.meta.classification)
        canvas.drawRightString(PAGE_SIZE[0] - MARGIN_R, 3.4 * mm, f"Version {self.meta.version}  |  {self.meta.date}")
        canvas.restoreState()

    def _chrome(self, canvas) -> None:
        canvas.saveState()
        canvas.setFont(F_BODY, 7.8)
        canvas.setFillColor(MUTED)
        canvas.drawString(MARGIN_L, PAGE_SIZE[1] - 15 * mm, self.meta.system)
        canvas.drawRightString(PAGE_SIZE[0] - MARGIN_R, PAGE_SIZE[1] - 15 * mm, self.meta.title)
        canvas.setStrokeColor(RULE)
        canvas.setLineWidth(0.5)
        canvas.line(MARGIN_L, PAGE_SIZE[1] - 17 * mm, PAGE_SIZE[0] - MARGIN_R, PAGE_SIZE[1] - 17 * mm)
        canvas.line(MARGIN_L, MARGIN_B - 6 * mm, PAGE_SIZE[0] - MARGIN_R, MARGIN_B - 6 * mm)
        canvas.drawString(MARGIN_L, MARGIN_B - 10.5 * mm, f"{self.meta.classification}  |  Version {self.meta.version}")
        canvas.restoreState()

    def _front_page(self, canvas, doc) -> None:
        if not self._front_labelled:
            self._front_labelled = True
            # reportlab wants the NAME of the style constant here, not its value: it tests
            # `style.upper() in __convertible__`, and __convertible__ is a string, so the value
            # "r" matches as a substring and then fails on getattr(self, "R").
            canvas.addPageLabel(canvas.getPageNumber() - 1, style="ROMAN_LOWER", start=1)
        self._chrome(canvas)
        canvas.saveState()
        canvas.setFont(F_BODY, 7.8)
        canvas.setFillColor(MUTED)
        canvas.drawRightString(PAGE_SIZE[0] - MARGIN_R, MARGIN_B - 10.5 * mm, _roman(canvas.getPageNumber() - 1))
        canvas.restoreState()

    def _body_page(self, canvas, doc) -> None:
        self._chrome(canvas)
        if self.body_offset is None:
            self.body_offset = canvas.getPageNumber() - 1
            canvas.addPageLabel(canvas.getPageNumber() - 1, style="ARABIC", start=1)
        number = canvas.getPageNumber() - self.body_offset
        self.counter.record(number)
        total = self.counter.total
        canvas.saveState()
        canvas.setFont(F_BODY, 7.8)
        canvas.setFillColor(MUTED)
        canvas.drawRightString(
            PAGE_SIZE[0] - MARGIN_R,
            MARGIN_B - 10.5 * mm,
            f"Page {number} of {total}" if total else f"Page {number}",
        )
        canvas.restoreState()

    def afterFlowable(self, flowable) -> None:
        if isinstance(flowable, Heading):
            self.canv.bookmarkPage(flowable.toc_key)
            # PDF outlines may not skip a level, so clamp to one deeper than the previous entry.
            level = min(flowable.toc_level - 1, getattr(self, "_outline_level", -1) + 1, 3)
            self._outline_level = level
            self.canv.addOutlineEntry(flowable.toc_text, flowable.toc_key, level=level, closed=level > 0)
            if flowable.toc_level <= 3:
                page = self.page - (self.body_offset or 0)  # body pages are numbered from 1
                self.notify("TOCEntry", (flowable.toc_level - 1, flowable.toc_text, page, flowable.toc_key))


# ---------------------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------------------


def build_cover(meta: DocumentMeta, width: float) -> list:
    flow: list = [
        Spacer(1, 50 * mm),
        Paragraph(meta.title, STYLES["CoverTitle"]),
        Spacer(1, 5 * mm),
        HRule(width_ratio=0.3, thickness=2.6, color=ACCENT, space_after=0),
        Spacer(1, 7 * mm),
        Paragraph(meta.subtitle, STYLES["CoverSub"]),
        Spacer(1, 38 * mm),
    ]
    rows = [
        ("System", meta.system),
        ("Document version", meta.version),
        ("Status", meta.status),
        ("Date of issue", meta.date),
        ("Intended audience", meta.audience),
        ("Document owner", meta.owner),
        ("Prepared by", meta.prepared_by),
        ("Classification", meta.classification),
    ] + list(meta.fields)
    table = Table(
        [[Paragraph(k, STYLES["CoverMetaKey"]), Paragraph(v, STYLES["CoverMeta"])] for k, v in rows],
        colWidths=[width * 0.3, width * 0.7],
        hAlign="LEFT",
    )
    table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LINEABOVE", (0, 0), (-1, 0), 0.6, RULE),
                ("LINEBELOW", (0, 0), (-1, -1), 0.6, RULE),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )
    flow.append(table)
    return flow


def build_toc() -> TableOfContents:
    toc = TableOfContents()
    toc.levelStyles = [STYLES["TOC0"], STYLES["TOC1"], STYLES["TOC2"]]
    toc.dotsMinLevel = 0
    return toc


def render(source: Path, out: Path, meta: DocumentMeta) -> None:
    doc = DocTemplate(str(out), meta)
    story: list = [doc.counter]  # an indexing flowable has to be a top-level story item
    story += build_cover(meta, doc.width)
    story.append(NextPageTemplate("Front"))
    story.append(PageBreak())
    story.append(Paragraph("Contents", STYLES["TOCTitle"]))
    story.append(HRule(thickness=1.1, color=ACCENT, space_after=8))
    story.append(build_toc())
    story.append(NextPageTemplate("Body"))
    story.append(PageBreak())
    story += Parser(doc.width).parse(source.read_text(encoding="utf-8"))
    doc.multiBuild(story)
    print(f"wrote {out}")


SYSTEM = "MG Archive Bot"
VERSION = "1.0.0"
ISSUE_DATE = "18 September 2026"
OWNER = "Motion Graphics Ministry, Team Lead Group"
PREPARED_BY = "Motion Graphics Archive Development Team"

DOCUMENTS = [
    (
        "technical_documentation.md",
        "TECHNICAL_DOCUMENTATION.pdf",
        DocumentMeta(
            title="Technical Documentation",
            subtitle="Architecture, data model, subsystems, security and operations for the Motion Graphics Archive Management System",
            system=SYSTEM,
            version=VERSION,
            date=ISSUE_DATE,
            classification="Internal use only",
            audience="Engineering, technical reviewers and system administrators",
            owner=OWNER,
            prepared_by=PREPARED_BY,
            status="Released for review",
        ),
    ),
    (
        "user_manual.md",
        "USER_MANUAL.pdf",
        DocumentMeta(
            title="User Manual",
            subtitle="A step by step guide to using the Motion Graphics archive assistant on Telegram",
            system=SYSTEM,
            version=VERSION,
            date=ISSUE_DATE,
            classification="Internal use only",
            audience="Designers, Team Leads and the Super Admin",
            owner=OWNER,
            prepared_by=PREPARED_BY,
            status="Released for review",
        ),
    ),
]


def main() -> int:
    here = Path(__file__).resolve().parent
    out_dir = here.parent
    for source_name, out_name, meta in DOCUMENTS:
        render(here / source_name, out_dir / out_name, meta)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

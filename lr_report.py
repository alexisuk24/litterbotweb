"""Per-cat vet report as a PDF: summary, weight trend, visits per day by waste type,
anomalies against the cat's own normal, a daily table and the owner's notes.

web.py gathers the numbers; this module only lays them out."""
from __future__ import annotations

import io
from pathlib import Path

from reportlab.graphics.charts.barcharts import VerticalBarChart
from reportlab.graphics.charts.lineplots import LinePlot
from reportlab.graphics.shapes import Drawing, Rect, String
from reportlab.graphics.widgets.markers import makeMarker
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (Image, KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table,
                                TableStyle)

INK = colors.HexColor("#1f2430")
MUTED = colors.HexColor("#6b7280")
RULE = colors.HexColor("#d7dbe2")
SHADE = colors.HexColor("#f3f4f7")
WARN = colors.HexColor("#b26a00")
URINE = colors.HexColor("#d9a400")
FECES = colors.HexColor("#8a5a2b")
OTHER = colors.HexColor("#a0a7b4")
LINE = colors.HexColor("#3f6fd8")

_ss = getSampleStyleSheet()
H1 = ParagraphStyle("h1", parent=_ss["Title"], fontSize=18, leading=22, alignment=0, textColor=INK, spaceAfter=2)
H2 = ParagraphStyle("h2", parent=_ss["Heading2"], fontSize=12, leading=15, textColor=INK, spaceBefore=10, spaceAfter=4)
BODY = ParagraphStyle("body", parent=_ss["BodyText"], fontSize=9, leading=12.5, textColor=INK)
SMALL = ParagraphStyle("small", parent=BODY, fontSize=8, leading=10.5, textColor=MUTED)
BULLET = ParagraphStyle("bullet", parent=BODY, leftIndent=10, bulletIndent=0)


def fmt_dur(s: float | None) -> str:
    if s is None:
        return "—"
    s = round(s)
    return f"{s // 60}m {s % 60:02d}s" if s >= 60 else f"{s}s"


def _esc(t: str) -> str:
    return str(t).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _table(rows, widths, header=True, align_right=()):
    t = Table(rows, colWidths=widths, repeatRows=1 if header else 0)
    style = [
        ("FONT", (0, 0), (-1, -1), "Helvetica", 8.5),
        ("TEXTCOLOR", (0, 0), (-1, -1), INK),
        ("LINEBELOW", (0, 0), (-1, -1), 0.4, RULE),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]
    if header:
        style += [("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 8), ("TEXTCOLOR", (0, 0), (-1, 0), MUTED),
                  ("LINEBELOW", (0, 0), (-1, 0), 0.8, MUTED)]
    for c in align_right:
        style.append(("ALIGN", (c, 0), (c, -1), "RIGHT"))
    t.setStyle(TableStyle(style))
    return t


def _no_data(width, height, text):
    d = Drawing(width, height)
    d.add(String(width / 2, height / 2, text, fontName="Helvetica", fontSize=9, fillColor=MUTED, textAnchor="middle"))
    return d


def _weight_chart(r, width, height=55 * mm):
    daily = [(i, w) for i, w in enumerate(r["daily_weight"]) if w is not None]
    readings = r["weight_readings"]
    if not daily and not readings:
        return _no_data(width, height, "No weight readings in this period")
    d = Drawing(width, height)
    lp = LinePlot()
    lp.x, lp.y, lp.width, lp.height = 34, 22, width - 44, height - 32
    series = [daily or [(0, None)], readings or [(0, None)]]
    lp.data = [[p for p in s if p[1] is not None] or [(0, 0)] for s in series]
    ys = [w for _, w in daily] + [w for _, w in readings]
    lo, hi = min(ys), max(ys)
    if r.get("weight_band"):
        lo, hi = min(lo, r["weight_band"][0]), max(hi, r["weight_band"][1])
    pad = max((hi - lo) * 0.15, 0.05)
    lp.yValueAxis.valueMin, lp.yValueAxis.valueMax = lo - pad, hi + pad
    lp.yValueAxis.labelTextFormat = "%.2f"
    lp.yValueAxis.labels.fontSize = 7
    lp.xValueAxis.valueMin, lp.xValueAxis.valueMax = -0.5, len(r["days"]) - 0.5
    step = max(1, round(len(r["days"]) / 8))
    lp.xValueAxis.valueSteps = list(range(0, len(r["days"]), step))
    lp.xValueAxis.labelTextFormat = lambda i: r["days"][int(i)].strftime("%-d %b") if 0 <= int(i) < len(r["days"]) else ""
    lp.xValueAxis.labels.fontSize = 7
    lp.lines[0].strokeColor = LINE
    lp.lines[0].strokeWidth = 1.6
    lp.lines[1].strokeColor = None
    lp.lines[1].symbol = makeMarker("Circle", size=2.2, fillColor=colors.HexColor("#9db3e8"), strokeColor=None)
    d.add(lp)
    return d


def _visits_chart(r, width, height=55 * mm):
    days = r["days"]
    if not any(r["daily"][i]["visits"] for i in range(len(days))):
        return _no_data(width, height, "No visits in this period")
    d = Drawing(width, height)
    bc = VerticalBarChart()
    bc.x, bc.y, bc.width, bc.height = 28, 22, width - 38, height - 32
    bc.data = [[row[k] for row in r["daily"]] for k in ("urine", "feces", "other")]
    bc.categoryAxis.style = "stacked"
    step = max(1, round(len(days) / 8))
    bc.categoryAxis.categoryNames = [dd.strftime("%-d %b") if i % step == 0 else "" for i, dd in enumerate(days)]
    bc.categoryAxis.labels.fontSize = 7
    bc.valueAxis.valueMin = 0
    bc.valueAxis.labels.fontSize = 7
    top = max(row["visits"] for row in r["daily"])
    bc.valueAxis.valueStep = max(1, round(top / 4))
    bc.valueAxis.valueMax = max(top, 1)
    bc.barSpacing = 0
    bc.groupSpacing = 1 if len(days) > 45 else 2
    for i, c in enumerate((URINE, FECES, OTHER)):
        bc.bars[i].fillColor = c
        bc.bars[i].strokeColor = None
    d.add(bc)
    x = width - 150
    for label, c in (("Urine", URINE), ("Stool", FECES), ("No type", OTHER)):
        d.add(String(x + 9, height - 8, label, fontName="Helvetica", fontSize=7, fillColor=MUTED))
        d.add(Rect(x, height - 9, 6, 6, fillColor=c, strokeColor=None))
        x += 48
    return d


def build_pdf(r: dict) -> bytes:
    """Render the report. `r` is built by web.vet_report_data."""
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=16 * mm, rightMargin=16 * mm,
                            topMargin=14 * mm, bottomMargin=16 * mm,
                            title=f"Litter box report: {r['name']}", author="Litter Robot")
    width = A4[0] - 32 * mm
    story = []

    # header: photo, name, period
    title = [Paragraph(f"Litter box report: {_esc(r['name'])}", H1),
             Paragraph(f"{r['start']:%-d %b %Y} – {r['end']:%-d %b %Y} ({len(r['days'])} days) · "
                       f"generated {r['generated']:%-d %b %Y %H:%M} ({_esc(r['tz'])})", SMALL)]
    photo = r.get("photo")
    if photo and Path(photo).suffix.lower() in (".jpg", ".jpeg", ".png"):
        try:
            img = Image(photo, width=18 * mm, height=18 * mm)
            story.append(Table([[img, title]], colWidths=[22 * mm, width - 22 * mm],
                               style=[("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("LEFTPADDING", (0, 0), (-1, -1), 0)]))
        except Exception:
            story += title
    else:
        story += title
    story.append(Spacer(1, 4 * mm))

    # summary
    s = r["summary"]
    rows = [
        ["Visits", f"{s['visits']} ({s['per_day']:.1f} per day)", "Usual", s["usual_visits"]],
        ["Urine / stool / no type", s["waste_split"], "Longest gap without stool", s["stool_gap"]],
        ["Typical visit (median)", fmt_dur(s["median_duration"]), "Usual length", s["usual_duration"]],
        ["Weight, first week", s["weight_first"], "Weight, last week", s["weight_last"]],
        ["Weight change", s["weight_change"], "Usual weight", s["usual_weight"]],
    ]
    t = Table(rows, colWidths=[38 * mm, width / 2 - 38 * mm, 40 * mm, width / 2 - 40 * mm])
    t.setStyle(TableStyle([
        ("FONT", (0, 0), (-1, -1), "Helvetica", 9),
        ("FONT", (0, 0), (0, -1), "Helvetica", 8), ("FONT", (2, 0), (2, -1), "Helvetica", 8),
        ("TEXTCOLOR", (0, 0), (0, -1), MUTED), ("TEXTCOLOR", (2, 0), (2, -1), MUTED),
        ("BACKGROUND", (0, 0), (-1, -1), SHADE),
        ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    story.append(t)
    story.append(Paragraph(
        "Usual ranges are this cat's own normal, learned from the period and up to 30 days before it. "
        "Visits are as assigned in the app, including the owner's corrections.", SMALL))

    # anomalies
    story.append(Paragraph("Things to note", H2))
    if r["anomalies"]:
        for a in r["anomalies"]:
            story.append(Paragraph(_esc(a), BULLET, bulletText="•"))
    else:
        story.append(Paragraph("Nothing outside this cat's usual range in this period.", BODY))

    story.append(KeepTogether([Paragraph(f"Weight ({r['unit']})", H2),
                               Paragraph("Line: daily median of visit weights. Dots: each visit's reading.", SMALL),
                               _weight_chart(r, width)]))
    story.append(KeepTogether([Paragraph("Visits per day", H2),
                               Paragraph("Each bar is one day, split by the waste type the robot detected.", SMALL),
                               _visits_chart(r, width)]))

    if r["long_visits"]:
        rows = [["When", "Length", "Waste detected", "Robot"]] + [
            [v["when"], fmt_dur(v["duration"]), v["waste"] or "none", v["robot"] or ""] for v in r["long_visits"]]
        story.append(KeepTogether([
            Paragraph("Unusually long visits", H2),
            Paragraph(f"Longer than {fmt_dur(r['long_over'])}; this cat's typical visit is {fmt_dur(r['typical'])}.", SMALL),
            _table(rows, [45 * mm, 22 * mm, 35 * mm, width - 102 * mm])]))

    story.append(Paragraph("Day by day", H2))
    rows = [["Date", "Visits", "Urine", "Stool", "No type", f"Weight ({r['unit']})", "Longest visit"]]
    for dd, row in zip(r["days"], r["daily"]):
        rows.append([dd.strftime("%a %-d %b"), row["visits"], row["urine"], row["feces"], row["other"],
                     f"{row['weight']:.2f}" if row["weight"] is not None else "—", fmt_dur(row["longest"])])
    story.append(_table(rows, [30 * mm, 18 * mm, 18 * mm, 18 * mm, 18 * mm, 28 * mm, width - 130 * mm],
                        align_right=(1, 2, 3, 4, 5, 6)))

    if r["notes"]:
        story.append(Paragraph("Owner's notes", H2))
        story.append(_table([["When", "Note"]] + [[n["when"], Paragraph(_esc(n["note"]), BODY)] for n in r["notes"]],
                            [45 * mm, width - 45 * mm]))

    def footer(canvas, doc_):
        canvas.saveState()
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(MUTED)
        canvas.drawString(16 * mm, 9 * mm, f"{r['name']} · Litter-Robot data · not a diagnosis")
        canvas.drawRightString(A4[0] - 16 * mm, 9 * mm, f"Page {doc_.page}")
        canvas.restoreState()

    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return buf.getvalue()

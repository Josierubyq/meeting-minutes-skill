#!/usr/bin/env python3
"""把 transcript.json + minutes.json(+ translations/*.json)排版成 Word 和 PDF。

用法:
  python build_minutes.py WORK_DIR [--outdir ~/Documents/会议记录] [--formats docx,pdf,html] [--draft]

WORK_DIR 里需要:
  transcript.json        transcribe.py 的输出
  minutes.json           会议信息、发言人名字、会议重点、下一步(格式见 references/minutes_format.md)
  translations/*.json    (可选)逐段译文,可以分多个文件写;也可以直接写在 minutes.json 的 "utterances" 里

文件名:年月日_会议主题_参会人1、参会人2.docx/.pdf(网页版加 _可点录音.html);同名文件已存在时加 _v2、_v3,不覆盖。
"""
import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from docx import Document
from docx.enum.section import WD_SECTION
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

LATIN, EA = "Calibri", "DengXian"  # 等线:Office 自带,Mac / Windows 打开都一致
INK = RGBColor(0x1F, 0x23, 0x28)
GRAY = RGBColor(0x6B, 0x72, 0x80)
LIGHT = RGBColor(0x9C, 0xA3, 0xAF)
ACCENT = RGBColor(0x1F, 0x4E, 0x79)
MIX = RGBColor(0x1D, 0x6F, 0xB8)  # 原话里另一种语言的词
UNSURE = RGBColor(0xB4, 0x53, 0x09)  # 没能从录音确认的词后面的问号
MARKS = re.compile(r"\[\?\]|\[听不清\]|\[inaudible\]")
HEX = lambda c: "%02X%02X%02X" % (c[0], c[1], c[2])
CJK = "一-鿿㐀-䶿"
LATIN_RUN = re.compile(r"[A-Za-z][A-Za-z0-9\-+.'’/&_]*(?:[ ]+[A-Za-z0-9][A-Za-z0-9\-+.'’/&_]*)*")
CJK_RUN = re.compile(rf"[{CJK}][{CJK}　-〿＀-￯]*")
WEEK_ZH = "一二三四五六日"
# 交付文件默认存到这里;想换地方就设环境变量 MEETING_MINUTES_OUT,或每次用 --outdir
DEFAULT_OUT = Path(os.environ.get("MEETING_MINUTES_OUT", Path.home() / "Documents/会议记录")).expanduser()


# ---------------------------------------------------------------- data
def load(work: Path):
    tr = json.loads((work / "transcript.json").read_text(encoding="utf-8"))
    mi = json.loads((work / "minutes.json").read_text(encoding="utf-8"))
    entries = {}
    for k, v in (mi.get("utterances") or {}).items():
        entries.setdefault(str(k), {}).update(v)
    tdir = work / "translations"
    if tdir.exists():
        for f in sorted(tdir.glob("*.json")):
            for k, v in json.loads(f.read_text(encoding="utf-8")).items():
                entries.setdefault(str(k), {}).update(v)
    return tr, mi, entries


def fmt_ts(sec):
    sec = int(round(sec))
    return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}" if sec >= 3600 else f"{sec // 60:02d}:{sec % 60:02d}"


def cjk_ratio(t):
    letters = re.findall(rf"[{CJK}A-Za-z]", t)
    return len(re.findall(rf"[{CJK}]", t)) / len(letters) if letters else 0.0


def build_rows(tr, mi, entries, draft):
    names = mi.get("speakers", {})
    rows, missing = [], []
    for u in tr["utterances"]:
        e = entries.get(str(u["id"]), {})
        if e.get("drop"):
            continue
        text = e.get("fix", u["text"]).strip()
        spk = e.get("speaker", u["speaker"])
        orig = e.get("orig") or u.get("lang")
        if orig not in ("zh", "en"):
            orig = "zh" if cjk_ratio(text) >= 0.3 else "en"
        # 只给了一种译文时,原话自然在另一栏(转写时语言标错的常见情况:中文里夹了很多英文词)
        if "orig" not in e and orig == "en" and "en" in e and "zh" not in e:
            orig = "zh"
        elif "orig" not in e and orig == "zh" and "zh" in e and "en" not in e:
            orig = "en"
        if orig == "zh":
            zh, en = text, e.get("en", "")
        else:
            zh, en = e.get("zh", ""), text
        if orig not in ("zh", "en") or (u.get("lang") not in ("zh", "en") and "zh" in e and "en" in e):
            zh, en = e.get("zh", zh), e.get("en", en)
        if not (zh and en):
            missing.append(u["id"])
        rows.append({"id": u["id"], "start": u["start"], "speaker": names.get(spk, f"说话人 {spk[1:]}"),
                     "orig": orig, "zh": zh, "en": en})
    if missing and not draft:
        sys.exit(f"缺译文的段落 {len(missing)} 个:{missing[:50]}{' ...' if len(missing) > 50 else ''}\n"
                 "补齐后重跑;只想先看排版可加 --draft。")
    unknown = sorted({u["speaker"] for u in tr["utterances"]} - set(names))
    if unknown:
        print(f"提醒:这些说话人没有对应名字,文档里显示为『说话人 N』:{unknown}", file=sys.stderr)
    return merge_rows(rows)


def merge_rows(rows, gap=20.0, max_len=220):
    """同一人连续的短句合成一行(间隔 ≤20 秒、原话在同一栏、合并后不超过 220 字),时间用第一句的。"""
    def join(a, b, cjk):
        if not a:
            return b
        if cjk and re.search(rf"[{CJK}，。？！：；、…）]$", a):
            return a + b
        return a + ("" if cjk and re.match(rf"[{CJK}]", b) else " ") + b

    out = []
    for r in rows:
        p = out[-1] if out else None
        if (p and p["speaker"] == r["speaker"] and p["orig"] == r["orig"] and r["start"] - p["last"] <= gap
                and len(p["zh"]) + len(r["zh"]) <= max_len):
            p["zh"] = join(p["zh"], r["zh"], True)
            p["en"] = join(p["en"], r["en"], False)
            p["last"] = r["start"]
        else:
            out.append({**r, "last": r["start"]})
    return out


# ---------------------------------------------------------------- docx helpers
def set_run_fonts(rpr_parent, latin=LATIN, ea=EA):
    rpr = rpr_parent.get_or_add_rPr() if hasattr(rpr_parent, "get_or_add_rPr") else rpr_parent
    rf = rpr.find(qn("w:rFonts"))
    if rf is None:
        rf = OxmlElement("w:rFonts")
        rpr.insert(0, rf)
    for a in ("w:asciiTheme", "w:hAnsiTheme", "w:eastAsiaTheme", "w:cstheme"):
        if rf.get(qn(a)) is not None:
            del rf.attrib[qn(a)]
    rf.set(qn("w:ascii"), latin)
    rf.set(qn("w:hAnsi"), latin)
    rf.set(qn("w:eastAsia"), ea)
    rf.set(qn("w:cs"), latin)


def run(p, text, size=10.5, bold=False, color=INK, italic=False):
    """普通文字。文字里的 [?] 渲染成上标问号,[听不清]/[inaudible] 渲染成浅灰。"""
    if MARKS.search(text):
        pos, last = 0, None
        for m in MARKS.finditer(text):
            if m.start() > pos:
                last = run(p, text[pos:m.start()], size, bold, color, italic)
            if m.group() == "[?]":
                last = _plain(p, "?", size * 0.85, True, UNSURE, False)
                last.font.superscript = True
            else:
                last = _plain(p, m.group(), size * 0.9, False, LIGHT, False)
            pos = m.end()
        if pos < len(text):
            last = run(p, text[pos:], size, bold, color, italic)
        return last
    return _plain(p, text, size, bold, color, italic)


def _plain(p, text, size, bold, color, italic):
    r = p.add_run(text)
    r.font.size = Pt(size)
    r.font.bold = bold
    r.font.italic = italic
    r.font.color.rgb = color
    set_run_fonts(r._element)
    return r


def rich(p, text, size, color, mark):
    """原话:另一种语言的词用 MIX 颜色标出。mark='latin' 标中文里的英文;'cjk' 标英文里的中文。"""
    pat = LATIN_RUN if mark == "latin" else CJK_RUN
    pos = 0
    text = re.sub(r"\[inaudible\]", "[听不清]", text) if mark == "latin" else text
    for m in pat.finditer(text):
        if MARKS.match(text, max(m.start() - 1, 0)) or text[max(m.start() - 1, 0)] == "[":
            continue  # [inaudible] 这类标记不当作英文词标蓝
        if m.start() > pos:
            run(p, text[pos:m.start()], size, color=color)
        run(p, m.group(), size, color=MIX)
        pos = m.end()
    if pos < len(text):
        run(p, text[pos:], size, color=color)


def para(container, before=0, after=4, line=1.25, align=None):
    p = container.add_paragraph()
    pf = p.paragraph_format
    pf.space_before, pf.space_after, pf.line_spacing = Pt(before), Pt(after), line
    if align is not None:
        p.alignment = align
    return p


def border(el_pr, tag, edges, sz=4, color="D9DDE3", val="single"):
    b = el_pr.find(qn(tag))
    if b is None:
        b = OxmlElement(tag)
        el_pr.append(b)
    for edge in edges:
        e = b.find(qn(f"w:{edge}"))
        if e is None:
            e = OxmlElement(f"w:{edge}")
            b.append(e)
        e.set(qn("w:val"), val)
        e.set(qn("w:sz"), str(sz))
        e.set(qn("w:space"), "0")
        e.set(qn("w:color"), color)


def shade(cell, fill):
    tcpr = cell._tc.get_or_add_tcPr()
    s = OxmlElement("w:shd")
    s.set(qn("w:val"), "clear")
    s.set(qn("w:color"), "auto")
    s.set(qn("w:fill"), fill)
    tcpr.append(s)


def make_table(doc, widths_cm, header=None):
    t = doc.add_table(rows=0, cols=len(widths_cm))
    t.alignment = WD_TABLE_ALIGNMENT.CENTER
    t.autofit = False
    tblpr = t._tbl.tblPr
    lay = OxmlElement("w:tblLayout")
    lay.set(qn("w:type"), "fixed")
    tblpr.append(lay)
    mar = OxmlElement("w:tblCellMar")
    for side, v in (("top", 70), ("bottom", 70), ("left", 90), ("right", 90)):
        e = OxmlElement(f"w:{side}")
        e.set(qn("w:w"), str(v))
        e.set(qn("w:type"), "dxa")
        mar.append(e)
    tblpr.append(mar)
    border(tblpr, "w:tblBorders", ["top", "left", "bottom", "right", "insideV"], val="nil")
    border(tblpr, "w:tblBorders", ["insideH", "bottom"], sz=4, color="E3E6EA")
    grid = t._tbl.tblGrid
    for gc, w in zip(grid.findall(qn("w:gridCol")), widths_cm):
        gc.set(qn("w:w"), str(int(w * 567)))
    t._widths = widths_cm
    if header:
        cells = add_row(t)
        tr = t.rows[-1]._tr
        trpr = tr.get_or_add_trPr()
        h = OxmlElement("w:tblHeader")
        trpr.append(h)
        for c, (zh, en) in zip(cells, header):
            shade(c, "F2F4F7")
            p = c.paragraphs[0]
            p.paragraph_format.space_after = Pt(0)
            run(p, zh, 9.5, bold=True, color=INK)
            if en:
                run(p, f"  {en}", 8.5, color=GRAY)
            border(c._tc.get_or_add_tcPr(), "w:tcBorders", ["bottom"], sz=8, color="9CA3AF")
    return t


def add_row(t):
    row = t.add_row()
    # 一行不拆到两页(行比整页还高时 Word 仍会拆)
    row._tr.get_or_add_trPr().append(OxmlElement("w:cantSplit"))
    cells = row.cells
    for c, w in zip(cells, t._widths):
        c.width = Cm(w)
    return cells


def cell_para(cell, first=True, after=2):
    p = cell.paragraphs[0] if first else cell.add_paragraph()
    p.paragraph_format.space_after = Pt(after)
    p.paragraph_format.space_before = Pt(0)
    p.paragraph_format.line_spacing = 1.2
    return p


def field(p, instr, size=8, color=LIGHT):
    r1 = run(p, "", size, color=color)
    b = OxmlElement("w:fldChar")
    b.set(qn("w:fldCharType"), "begin")
    r1._element.append(b)
    r2 = run(p, "", size, color=color)
    it = OxmlElement("w:instrText")
    it.set(qn("xml:space"), "preserve")
    it.text = instr
    r2._element.append(it)
    sep = OxmlElement("w:fldChar")
    sep.set(qn("w:fldCharType"), "separate")
    run(p, "", size, color=color)._element.append(sep)
    r3 = run(p, "1", size, color=color)
    r4 = run(p, "", size, color=color)
    e = OxmlElement("w:fldChar")
    e.set(qn("w:fldCharType"), "end")
    r4._element.append(e)


def heading(doc, num, zh, en=""):
    p = doc.add_paragraph(style="Heading 1")
    pf = p.paragraph_format
    pf.space_before, pf.space_after, pf.keep_with_next = Pt(20), Pt(8), True
    run(p, f"{num}　{zh}", 14, bold=True, color=ACCENT)
    if en:
        run(p, f"   {en}", 10.5, color=GRAY)
    border(p._p.get_or_add_pPr(), "w:pBdr", ["bottom"], sz=6, color="C9D6E3")
    return p


# ---------------------------------------------------------------- render
def keep_english(text):
    """中文版里英文原话是否照原样放:夹了中文,或只是几个英文词(术语、附和),就不翻译。"""
    return bool(re.search(rf"[{CJK}]", text)) or len(re.findall(r"[A-Za-z]+", text)) <= 8


def render_docx(path, tr, mi, rows, recording_name, lang="both"):
    """lang='both' 中英对照;lang='zh' 只有中文(中文里夹的英文词保持原样)。"""
    zh_only = lang == "zh"
    E = (lambda s: "") if zh_only else (lambda s: s)  # 中文版里去掉所有英文标签
    doc = Document()
    for sname in ("Normal", "Heading 1"):
        st = doc.styles[sname]
        st.font.name = LATIN
        rpr = st.element.get_or_add_rPr()
        set_run_fonts(rpr)
        # 标成中文排版,Word 才会套用避头尾规则(句号、右括号不出现在行首)
        lang = rpr.find(qn("w:lang"))
        if lang is None:
            lang = OxmlElement("w:lang")
            rpr.append(lang)
        lang.set(qn("w:val"), "en-US")
        lang.set(qn("w:eastAsia"), "zh-CN")
        st.font.color.rgb = INK
    doc.styles["Normal"].font.size = Pt(10.5)
    h1 = doc.styles["Heading 1"]
    h1.font.size, h1.font.bold = Pt(14), True

    sec = doc.sections[0]
    sec.page_width, sec.page_height = Cm(21.0), Cm(29.7)
    sec.left_margin = sec.right_margin = Cm(2.0)
    sec.top_margin, sec.bottom_margin = Cm(2.2), Cm(2.0)
    sec.header_distance = sec.footer_distance = Cm(1.0)
    sec.different_first_page_header_footer = True

    date = dt.date.fromisoformat(mi["date"])
    date_zh = f"{date.year}年{date.month}月{date.day}日（周{WEEK_ZH[date.weekday()]}）"
    date_en = date.strftime("%a, %d %b %Y")
    topic, topic_en = mi["topic"], mi.get("topic_en", "")

    # 页眉页脚
    hp = sec.header.paragraphs[0]
    hp.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    run(hp, f"{topic}  ·  {date.isoformat()}", 8, color=LIGHT)
    for footer in (sec.footer, sec.first_page_footer):
        fp = footer.paragraphs[0]
        fp.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run(fp, "第 ", 8, color=LIGHT)
        field(fp, "PAGE")
        run(fp, " 页 / 共 ", 8, color=LIGHT)
        field(fp, "NUMPAGES")
        run(fp, " 页", 8, color=LIGHT)

    # 标题区
    p = para(doc, after=2)
    run(p, "会议记录  ", 9, bold=True, color=ACCENT)
    if not zh_only:
        run(p, "MEETING MINUTES", 8.5, color=GRAY)
    p = para(doc, before=2, after=2 if topic_en and not zh_only else 10, line=1.1)
    run(p, topic, 22, bold=True)
    if topic_en and not zh_only:
        p = para(doc, after=10)
        run(p, topic_en, 12, color=GRAY)

    dur = round(tr["duration_sec"] / 60)
    when_zh = date_zh + (f"  {mi['time']}" if mi.get("time") else "")
    meta = [("日期", "Date", when_zh, date_en)]
    meta.append(("时长", "Duration", f"约 {dur} 分钟", f"about {dur} min"))
    meta.append(("参会人", "Participants", "、".join(mi["participants"]), ""))
    if mi.get("place"):
        meta.append(("地点", "Venue", mi["place"], ""))
    meta.append(("录音", "Recording", recording_name, ""))
    t = make_table(doc, [2.4 if zh_only else 3.2, 14.6 if zh_only else 13.8])
    for zh, en, v1, v2 in meta:
        c1, c2 = add_row(t)
        p = cell_para(c1, after=0)
        run(p, zh, 9.5, bold=True, color=GRAY)
        if E(en):
            run(p, f" {en}", 8, color=LIGHT)
        p = cell_para(c2, after=0)
        run(p, v1, 10)
        if E(v2):
            run(p, f"   {v2}", 9, color=GRAY)

    # 一、发言记录
    heading(doc, "一", "发言记录", E("Transcript"))
    p = para(doc, after=6)
    if zh_only:
        # 英文原话:夹了中文或只有几个词(术语、附和)的照原样放;完整的英文句子换成中文译文并标「译」
        translated = any(r["orig"] == "en" and not keep_english(r["en"]) for r in rows)
        run(p, "按时间顺序。原话里的英文词保持原样，用", 8.5, color=GRAY)
        run(p, "蓝色", 8.5, color=MIX)
        run(p, "标出。", 8.5, color=GRAY)
        if translated:
            run(p, "标「译」的是英文原话的中文译文。", 8.5, color=GRAY)
    else:
        run(p, "按时间顺序。黑字为原话，灰字为译文；原话中夹带的另一种语言的词用", 8.5, color=GRAY)
        run(p, "蓝色", 8.5, color=MIX)
        run(p, "标出。", 8.5, color=GRAY)
    if any(MARKS.search(r["zh"] + r["en"]) for r in rows):
        run(p, "带", 8.5, color=GRAY)
        q = _plain(p, "?", 7.5, True, UNSURE, False)
        q.font.superscript = True
        run(p, "的词没能从录音确认；[听不清] 表示录音里听不出来。", 8.5, color=GRAY)
    if zh_only:
        t = make_table(doc, [2.6, 14.4], header=[("发言人", ""), ("内容", "")])
    else:
        t = make_table(doc, [2.6, 7.2, 7.2], header=[("发言人", "Speaker"), ("中文", "Chinese"), ("英文", "English")])
    for r in rows:
        cells = add_row(t)
        c0 = cells[0]
        p = cell_para(c0, after=0)
        run(p, r["speaker"], 9.5, bold=True)
        p = cell_para(c0, first=False, after=0)
        run(p, fmt_ts(r["start"]), 8, color=LIGHT)
        if zh_only:
            pz = cell_para(cells[1], after=0)
            if r["orig"] == "zh":
                rich(pz, r["zh"], 10, INK, "latin")
            elif keep_english(r["en"]):
                rich(pz, r["en"], 10, INK, "latin")
            else:
                run(pz, "译  ", 8, bold=True, color=LIGHT)
                rich(pz, r["zh"], 10, GRAY, "latin")
            continue
        pz, pe = cell_para(cells[1], after=0), cell_para(cells[2], after=0)
        if r["orig"] == "zh":
            rich(pz, r["zh"], 10, INK, "latin")
            run(pe, r["en"], 9.5, color=GRAY)
        else:
            run(pz, r["zh"], 10, color=GRAY)
            rich(pe, r["en"], 9.5, INK, "cjk")

    # 二、会议重点
    by_id = {u["id"]: u for u in tr["utterances"]}

    def ref_text(refs):
        ts = [fmt_ts(by_id[i]["start"]) for i in (refs or []) if i in by_id]
        return "  " + " · ".join(ts) if ts else ""

    heading(doc, "二", "会议重点", E("Key Points"))
    # 编号和正文分两栏的无边框表格:比制表位稳,标题以英文开头时也不会错位
    t = make_table(doc, [0.8, 16.2])
    border(t._tbl.tblPr, "w:tblBorders", ["insideH", "bottom"], val="nil")
    for i, k in enumerate(mi.get("key_points", []), 1):
        c0, c1 = add_row(t)
        p = cell_para(c0, after=0)
        run(p, f"{i}.", 10.5, bold=True, color=ACCENT)
        p = cell_para(c1, after=2)
        p.paragraph_format.line_spacing = 1.3
        if k.get("title"):
            run(p, k["title"] + "：", 10.5, bold=True)
        run(p, k["zh"], 10.5)
        rt = ref_text(k.get("refs"))
        if rt:
            run(p, rt, 8, color=LIGHT)
        if zh_only:
            p.paragraph_format.space_after = Pt(6)
        else:
            p = cell_para(c1, first=False, after=4)
            run(p, k.get("en", ""), 9.5, color=GRAY)

    # 三、下一步
    heading(doc, "三", "下一步行动", E("Next Steps"))
    me = mi.get("me", "")
    steps = sorted(mi.get("next_steps", []), key=lambda s: 0 if s.get("owner") == me else 1)
    t = make_table(doc, [0.7, 2.3, 9.6, 2.0, 2.4] if zh_only else [0.7, 2.5, 8.4, 2.8, 2.6],
                   header=[("#", ""), ("负责人", E("Owner")), ("事项", E("Action")), ("期限", E("Due")), ("出处", E("Ref"))])
    for i, s in enumerate(steps, 1):
        cells = add_row(t)
        if s.get("owner") == me:
            for c in cells:
                shade(c, "F5F9FD")
        p = cell_para(cells[0], after=0)
        run(p, str(i), 9.5, bold=True, color=ACCENT)
        p = cell_para(cells[1], after=0)
        run(p, s.get("owner", "—"), 9.5, bold=True)
        p = cell_para(cells[2], after=0 if zh_only else 1)
        run(p, s["zh"], 10)
        if not zh_only:
            p = cell_para(cells[2], first=False, after=0)
            run(p, s.get("en", ""), 9, color=GRAY)
        p = cell_para(cells[3], after=0)
        run(p, s.get("due") or "—", 9.5)
        p = cell_para(cells[4], after=0)
        run(p, ref_text(s.get("refs")).strip().replace(" · ", "\n") or "—", 8, color=GRAY)

    doc.core_properties.title = topic
    doc.core_properties.author = me
    doc.save(path)


# ---------------------------------------------------------------- pdf
WORD_SCRIPT = r'''
on run argv
  set inPath to item 1 of argv
  set outPath to item 2 of argv
  set docName to item 3 of argv
  set outHFS to (POSIX file outPath) as string
  tell application "Microsoft Word"
    open (POSIX file inPath)
    set ok to false
    set lastErr to ""
    -- 刚打开时文档可能还没准备好,稍等重试
    repeat 40 times
      try
        set d to active document
        if (name of d) is docName then
          save as d file name outHFS file format format PDF
          set ok to true
          exit repeat
        end if
      on error e
        set lastErr to e
      end try
      delay 0.5
    end repeat
    try
      close document docName saving no
    end try
    if not ok then error "导出失败:" & lastErr
  end tell
end run
'''


def pdf_via_word(docx: Path, pdf: Path):
    word = Path("/Applications/Microsoft Word.app")
    if not word.exists():
        raise RuntimeError("没有安装 Microsoft Word")
    running = subprocess.run(["pgrep", "-x", "Microsoft Word"], capture_output=True).returncode == 0
    # Word 是沙盒应用:在它自己的容器目录里读写,避免弹出“授权访问文件”窗口
    box = Path.home() / "Library/Containers/com.microsoft.Word/Data/tmp/meeting-minutes"
    box.mkdir(parents=True, exist_ok=True)
    stem = f"mm_{int(time.time() * 1000)}"
    src, out = box / f"{stem}.docx", box / f"{stem}.pdf"
    shutil.copy(docx, src)
    try:
        r = subprocess.run(["osascript", "-e", WORD_SCRIPT, str(src), str(out), src.name],
                           capture_output=True, text=True, timeout=180)
        if r.returncode != 0 or not out.exists():
            raise RuntimeError(f"Word 导出 PDF 失败:{r.stderr.strip() or r.stdout.strip()}")
        shutil.move(str(out), pdf)
    finally:
        src.unlink(missing_ok=True)
        if not running:
            subprocess.run(["osascript", "-e", 'tell application "Microsoft Word" to quit saving no'], capture_output=True)


def safe(s):
    return re.sub(r'[\\/:*?"<>|\n\r\t]+', "-", s).strip(" .-")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("work")
    ap.add_argument("--outdir", default=str(DEFAULT_OUT))
    ap.add_argument("--no-pdf", action="store_true")
    ap.add_argument("--draft", action="store_true", help="允许缺译文,只用来看排版")
    ap.add_argument("--name", help="手动指定文件名(不含扩展名)")
    ap.add_argument("--lang", choices=["both", "zh"], default="both",
                    help="both=中英对照(默认);zh=只有中文,中文里夹的英文词保持原样,文件名加 _中文版")
    ap.add_argument("--formats", default="docx,pdf",
                    help="要交哪几种,逗号分隔:docx(Word)、pdf、html(可点录音的网页版)。默认 docx,pdf")
    ap.add_argument("--html", action="store_true", help="等于在 --formats 里加 html")
    args = ap.parse_args()

    work = Path(args.work).expanduser()
    tr, mi, entries = load(work)
    for key in ("date", "topic", "participants"):
        if not mi.get(key):
            sys.exit(f"minutes.json 缺少 {key}")
    rows = build_rows(tr, mi, entries, args.draft)

    outdir = Path(args.outdir).expanduser()
    outdir.mkdir(parents=True, exist_ok=True)
    base = args.name or safe(f"{mi['date'].replace('-', '')}_{mi['topic']}_{'、'.join(mi['participants'])}")[:150]
    if args.lang == "zh" and not args.name:
        base += "_中文版"
    fmts = {f.strip().lower() for f in args.formats.split(",") if f.strip()}
    fmts = {"docx" if f in ("word", "doc") else f for f in fmts}
    if args.html:
        fmts.add("html")
    if args.no_pdf:
        fmts.discard("pdf")
    if not fmts or fmts - {"docx", "pdf", "html"}:
        sys.exit(f"--formats 只能是 docx、pdf、html 的组合,收到:{args.formats}")
    if "html" in fmts and args.lang == "zh":
        print("提醒:可点录音的网页版只有中英对照版,--lang zh 时不出。", file=sys.stderr)
        fmts.discard("html")
        if not fmts:
            sys.exit("没有要生成的文件。")

    from build_player import SUFFIX as HTML_SUFFIX
    stem, v = base, 1
    while any((outdir / n).exists() for n in (f"{stem}.docx", f"{stem}.pdf", f"{stem}{HTML_SUFFIX}.html")):
        v += 1
        stem = f"{base}_v{v}"
    result = {"rows": len(rows)}
    if fmts & {"docx", "pdf"}:
        # 只要 PDF 时,Word 文件只当中间文件,留在工作目录里
        docx = outdir / f"{stem}.docx" if "docx" in fmts else work / "drafts" / "for_pdf" / f"{stem}.docx"
        docx.parent.mkdir(parents=True, exist_ok=True)
        render_docx(docx, tr, mi, rows, Path(tr["source"]).name, args.lang)
        if "docx" in fmts:
            result["docx"] = str(docx)
        if "pdf" in fmts:
            pdf = outdir / f"{stem}.pdf"
            try:
                pdf_via_word(docx, pdf)
                result["pdf"] = str(pdf)
            except Exception as e:  # noqa: BLE001
                result["pdf_error"] = str(e)
    if "html" in fmts:
        from build_player import build as build_html
        result["html"] = str(build_html(work, outdir, stem, rows, tr, mi))
    (work / "output.json").write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()

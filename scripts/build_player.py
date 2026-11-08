#!/usr/bin/env python3
"""生成可点录音的网页版会议记录:点任何一句的时间,就从那一句开始播放录音。

用法:
  python build_player.py WORK_DIR [--outdir ~/Documents/会议记录] [--name 文件名(不含扩展名)] [--audio 录音路径]

- 内容和 Word 版一样(发言记录 → 会议重点 → 下一步,中英对照),会议重点和下一步的出处时间也能点。
- 录音压成 AAC 64 kbps 后嵌进网页:一个 .html 文件,双击用浏览器打开就能用,不联网、不上传。
  (压缩用 macOS 自带的 afconvert;没有它就嵌原始录音,文件会大一些。)
- 文件名:和 Word 版同名,末尾加 _可点录音;同名已存在时加 _v2,不覆盖。
- build_minutes.py 加 --html 时会顺手调用这里,和 Word 版用同一个文件名。
"""
import argparse
import base64
import datetime as dt
import html
import json
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_minutes import (CJK_RUN, DEFAULT_OUT, LATIN_RUN, MARKS, WEEK_ZH,  # noqa: E402
                           build_rows, fmt_ts, load, safe)

SUFFIX = "_可点录音"
MIME = {".m4a": "audio/mp4", ".mp4": "audio/mp4", ".aac": "audio/aac", ".mp3": "audio/mpeg",
        ".wav": "audio/wav", ".ogg": "audio/ogg", ".webm": "audio/webm", ".mov": "audio/mp4"}


# ---------------------------------------------------------------- audio
def compact_audio(src: Path, work: Path):
    """压成 64 kbps AAC(讲话足够清楚,体积约为手机原录音的一半;afconvert 转单声道会报错,所以保留双声道);结果缓存在工作目录。"""
    dst = work / "audio_for_html.m4a"
    if dst.exists() and dst.stat().st_mtime >= src.stat().st_mtime:
        return dst, "audio/mp4"
    if shutil.which("afconvert"):
        tmp = dst.with_suffix(".tmp.m4a")
        r = subprocess.run(["afconvert", "-f", "m4af", "-d", "aac", "-b", "64000", str(src), str(tmp)],
                           capture_output=True, text=True)
        if r.returncode == 0 and tmp.exists() and tmp.stat().st_size > 0:
            tmp.replace(dst)
            return dst, "audio/mp4"
        tmp.unlink(missing_ok=True)
        print(f"提醒:录音压缩失败,改嵌原始录音。{r.stderr.strip()[:200]}", file=sys.stderr)
    return src, MIME.get(src.suffix.lower(), "audio/mp4")


# ---------------------------------------------------------------- text
def marks_html(text):
    """[?] → 上标问号;[听不清]/[inaudible] → 浅灰。其余文字转义。"""
    out, pos = [], 0
    for m in MARKS.finditer(text):
        out.append(html.escape(text[pos:m.start()]))
        out.append('<sup class="q">?</sup>' if m.group() == "[?]" else f'<span class="ina">{html.escape(m.group())}</span>')
        pos = m.end()
    out.append(html.escape(text[pos:]))
    return "".join(out)


def rich_html(text, mark):
    """原话:另一种语言的词标蓝(mark='latin' 标中文里的英文,'cjk' 标英文里的中文),与 Word 版一致。"""
    if mark == "latin":
        text = text.replace("[inaudible]", "[听不清]")
    pat = LATIN_RUN if mark == "latin" else CJK_RUN
    out, pos = [], 0
    for m in pat.finditer(text):
        before = text[max(m.start() - 1, 0)]
        if m.start() > 0 and before == "[":
            continue  # [inaudible] 这类标记不当作英文词标蓝
        out.append(marks_html(text[pos:m.start()]))
        out.append(f'<span class="mix">{marks_html(m.group())}</span>')
        pos = m.end()
    out.append(marks_html(text[pos:]))
    return "".join(out)


def ts_button(sec, label=None):
    return f'<button class="ts" data-t="{sec:.2f}" title="从这里播放">{html.escape(label or fmt_ts(sec))}</button>'


# ---------------------------------------------------------------- page
CSS = r"""
:root{--ink:#1F2328;--gray:#6B7280;--light:#9CA3AF;--accent:#1F4E79;--mix:#1D6FB8;--unsure:#B45309;
--line:#E5E7EB;--soft:#F6F8FA;--me:#F5F9FD;--hl:#FFF7E0;--hlb:#F0B429;--bg:#FFFFFF}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.65 -apple-system,BlinkMacSystemFont,"PingFang SC","Hiragino Sans GB","Microsoft YaHei","Segoe UI",sans-serif}
.wrap{max-width:1040px;margin:0 auto;padding:0 24px 80px}
.bar{position:sticky;top:0;z-index:10;background:rgba(255,255,255,.96);backdrop-filter:blur(6px);border-bottom:1px solid var(--line)}
.bar .in{max-width:1040px;margin:0 auto;padding:8px 24px;display:flex;flex-wrap:wrap;align-items:center;gap:8px 12px}
.bar audio{flex:1 1 360px;min-width:240px;height:36px}
.bar .ctl{display:flex;align-items:center;gap:6px;font-size:13px;color:var(--gray)}
.bar button.b{border:1px solid var(--line);background:#fff;border-radius:6px;padding:4px 9px;font:inherit;font-size:13px;color:var(--ink);cursor:pointer}
.bar button.b:hover{border-color:var(--accent);color:var(--accent)}
.bar select{font:inherit;font-size:13px;padding:3px 4px;border:1px solid var(--line);border-radius:6px;background:#fff}
.bar .now{font-size:13px;color:var(--gray);cursor:pointer;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:100%}
.bar .now b{color:var(--ink)}
.bar .loading{font-size:13px;color:var(--gray)}
.kicker{margin-top:28px;font-size:12px;font-weight:700;color:var(--accent);letter-spacing:.04em}
.kicker span{font-weight:400;color:var(--gray);margin-left:6px}
h1{font-size:30px;line-height:1.25;margin:6px 0 4px}
.sub{color:var(--gray);font-size:16px;margin-bottom:18px}
table.meta{border-collapse:collapse;width:100%;margin-bottom:8px}
table.meta td{border-bottom:1px solid var(--line);padding:7px 4px;vertical-align:top}
table.meta td:first-child{width:150px;color:var(--gray);font-weight:600;font-size:13px}
table.meta td:first-child span{font-weight:400;color:var(--light);font-size:11px;margin-left:4px}
table.meta .en{color:var(--gray);font-size:13px;margin-left:10px}
h2{font-size:20px;margin:38px 0 8px;padding-bottom:6px;border-bottom:1px solid var(--line);color:var(--accent)}
h2 .n{margin-right:12px}
h2 .en{font-size:13px;font-weight:400;color:var(--gray);margin-left:8px}
.legend{font-size:12.5px;color:var(--gray);margin:0 0 10px}
.mix{color:var(--mix)}
sup.q{color:var(--unsure);font-weight:700;font-size:.72em;margin-left:1px}
.ina{color:var(--light);font-size:.92em}
.tr-head,.row{display:grid;grid-template-columns:118px 1fr 1fr;gap:0 18px}
.tr-head{background:var(--soft);font-size:13px;font-weight:700;padding:7px 10px;border-bottom:2px solid var(--line)}
.tr-head span{font-weight:400;color:var(--light);font-size:11px;margin-left:4px}
.row{padding:9px 10px;border-bottom:1px solid var(--line);scroll-margin-top:90px;border-left:3px solid transparent}
.row.playing{background:var(--hl);border-left-color:var(--hlb)}
.who b{display:block;font-size:14px}
.zh,.en2{font-size:14.5px}
.row .t{color:var(--gray)}
.row .o{color:var(--ink)}
.en2{font-size:14px}
button.ts{border:0;background:none;padding:1px 6px 1px 0;font:inherit;font-size:12px;color:var(--light);cursor:pointer;font-variant-numeric:tabular-nums;border-radius:4px}
button.ts::before{content:"▶";font-size:9px;margin-right:4px;color:var(--light);vertical-align:1px}
button.ts:hover,button.ts:hover::before{color:var(--accent)}
.row.playing button.ts,.row.playing button.ts::before{color:var(--hlb)}
.kp{display:grid;grid-template-columns:30px 1fr;gap:0 6px;margin:14px 0}
.kp .num{font-weight:700;color:var(--accent)}
.kp .zh{line-height:1.75}
.kp .en3{color:var(--gray);font-size:13.5px;margin-top:4px}
.refs{display:inline;margin-left:6px}
.refs button.ts{font-size:11.5px;padding-right:2px}
table.ns{border-collapse:collapse;width:100%;font-size:14px}
table.ns th{background:var(--soft);text-align:left;padding:7px 8px;font-size:13px;border-bottom:2px solid var(--line);white-space:nowrap}table.ns th span{font-weight:400;color:var(--light);font-size:11px;margin-left:3px}
table.ns td{padding:9px 8px;border-bottom:1px solid var(--line);vertical-align:top}
table.ns tr.me td{background:var(--me)}
table.ns .idx{color:var(--accent);font-weight:700;width:28px}
table.ns .own{font-weight:700;white-space:nowrap}
table.ns .en3{color:var(--gray);font-size:12.5px;margin-top:3px}
table.ns .ref button.ts{display:block}
.foot{margin-top:40px;font-size:12px;color:var(--light);text-align:center}
@media (max-width:760px){
 .wrap,.bar .in{padding-left:16px;padding-right:16px}
 h1{font-size:24px}
 table.meta td:first-child{width:96px}
 .tr-head{display:none}
 .row{grid-template-columns:1fr;gap:4px}
 .who{display:flex;align-items:baseline;gap:8px}
 .who b{display:inline}
 table.ns,table.ns tbody,table.ns tr,table.ns td{display:block;width:auto}
 table.ns thead{display:none}
 table.ns tr{border-bottom:1px solid var(--line);padding:6px 0}
 table.ns td{border:0;padding:3px 8px}
 table.ns .ref button.ts{display:inline}
}
@media print{.bar{display:none}.row{break-inside:avoid}button.ts::before{content:""}}
"""

JS = r"""
(function(){
  const audio = document.getElementById('audio');
  const rows = Array.from(document.querySelectorAll('.row[data-t]'));
  const starts = rows.map(r => parseFloat(r.dataset.t));
  const nowEl = document.getElementById('now');
  const follow = document.getElementById('follow');
  const LEAD = 0.6;  // 往前多放一点,免得切掉句首
  let ready = false, pending = null, cur = -1;

  function load(){
    try{
      // 录音数据要整段读完才能解码:脚本放在数据后面,并核对字节数(只解出一部分时,往后跳会直接跳到结尾)
      const b64 = document.getElementById('audio-data').textContent.trim();
      const bin = atob(b64), n = bin.length, u8 = new Uint8Array(n);
      if (n !== parseInt(audio.dataset.bytes, 10)) throw new Error('录音数据不完整(' + n + ' / ' + audio.dataset.bytes + ' 字节)');
      for (let i = 0; i < n; i++) u8[i] = bin.charCodeAt(i);
      audio.src = URL.createObjectURL(new Blob([u8], {type: audio.dataset.mime}));
      audio.addEventListener('loadedmetadata', () => {
        ready = true;
        document.getElementById('loading').remove();
        if (pending !== null) { seek(pending); pending = null; }
      }, {once: true});
    }catch(e){
      document.getElementById('loading').textContent = '录音加载失败：' + e.message;
    }
  }
  function seek(t){
    if (!ready) { pending = t; return; }
    audio.currentTime = Math.max(0, t - LEAD);
    const p = audio.play(); if (p && p.catch) p.catch(() => {});
  }
  function fmt(s){
    s = Math.max(0, Math.round(s));
    const h = Math.floor(s/3600), m = Math.floor(s%3600/60), x = s%60;
    const mm = String(m).padStart(2,'0'), ss = String(x).padStart(2,'0');
    return h ? `${String(h).padStart(2,'0')}:${mm}:${ss}` : `${mm}:${ss}`;
  }
  function locate(t){
    let lo = 0, hi = starts.length - 1, ans = -1;
    while (lo <= hi){ const mid = (lo + hi) >> 1; if (starts[mid] <= t + LEAD + 0.05) { ans = mid; lo = mid + 1; } else hi = mid - 1; }
    return ans;
  }
  function mark(i, scroll){
    if (i === cur) return;
    if (cur >= 0) rows[cur].classList.remove('playing');
    cur = i;
    if (i < 0) { nowEl.innerHTML = ''; return; }
    rows[i].classList.add('playing');
    const b = document.createElement('b'); b.textContent = rows[i].dataset.who;
    nowEl.replaceChildren('正在播放：', b, ' ' + fmt(starts[i]));
    if (scroll) rows[i].scrollIntoView({block: 'center', behavior: 'smooth'});
  }
  document.addEventListener('click', e => {
    const b = e.target.closest('button.ts');
    if (!b) return;
    b.blur();  // 让空格键回到"暂停 / 继续"
    const t = parseFloat(b.dataset.t);
    seek(t);
    const i = locate(t);
    mark(i, false);
  });
  audio.addEventListener('timeupdate', () => mark(locate(audio.currentTime), follow.checked && !audio.paused));
  document.getElementById('back5').onclick = () => { audio.currentTime = Math.max(0, audio.currentTime - 5); };
  document.getElementById('speed').onchange = e => { audio.playbackRate = parseFloat(e.target.value); };
  nowEl.onclick = () => { if (cur >= 0) rows[cur].scrollIntoView({block: 'center', behavior: 'smooth'}); };
  try { follow.checked = localStorage.getItem('mm-follow') === '1'; } catch(e) {}
  follow.onchange = () => { try { localStorage.setItem('mm-follow', follow.checked ? '1' : '0'); } catch(e) {} };
  // 空格键:播放/暂停(在输入框里不拦)
  document.addEventListener('keydown', e => {
    if (e.code === 'Space' && !/INPUT|SELECT|TEXTAREA|BUTTON|AUDIO/.test(document.activeElement.tagName)) {
      e.preventDefault(); audio.paused ? audio.play() : audio.pause();
    }
  });
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', load); else load();
})();
"""


def render_html(path: Path, tr, mi, rows, audio: Path, mime: str, recording_name: str):
    date = dt.date.fromisoformat(mi["date"])
    date_zh = f"{date.year}年{date.month}月{date.day}日（周{WEEK_ZH[date.weekday()]}）"
    date_en = date.strftime("%a, %d %b %Y")
    topic, topic_en = mi["topic"], mi.get("topic_en", "")
    dur = round(tr["duration_sec"] / 60)
    by_id = {u["id"]: u for u in tr["utterances"]}
    me = mi.get("me", "")
    E = html.escape

    def refs_html(refs, cls="refs"):
        btns = [ts_button(by_id[i]["start"]) for i in (refs or []) if i in by_id]
        return f'<span class="{cls}">{"".join(btns)}</span>' if btns else ""

    h = []
    h.append(f'<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
             f'<meta name="viewport" content="width=device-width,initial-scale=1">'
             f'<title>{E(topic)} · {date.isoformat()}</title><style>{CSS}</style></head><body>')
    # 播放条
    h.append('<div class="bar"><div class="in">'
             f'<audio id="audio" controls preload="auto" data-mime="{E(mime)}" data-bytes="{audio.stat().st_size}"></audio>'
             '<span class="ctl"><button class="b" id="back5" title="倒回 5 秒">↺ 5 秒</button>'
             '<select id="speed" title="播放速度"><option value="0.75">0.75×</option><option value="1" selected>1×</option>'
             '<option value="1.25">1.25×</option><option value="1.5">1.5×</option><option value="2">2×</option></select>'
             '<label title="播放时自动滚到正在说的那一句"><input type="checkbox" id="follow"> 跟着滚动</label></span>'
             '<span class="loading" id="loading">录音加载中…</span>'
             '<span class="now" id="now"></span>'
             '</div></div>')
    h.append('<div class="wrap">')
    h.append('<div class="kicker">会议记录<span>MEETING MINUTES</span></div>')
    h.append(f'<h1>{E(topic)}</h1>')
    if topic_en:
        h.append(f'<div class="sub">{E(topic_en)}</div>')
    when = date_zh + (f"  {mi['time']}" if mi.get("time") else "")
    meta = [("日期", "Date", marks_html(when), date_en),
            ("时长", "Duration", f"约 {dur} 分钟", f"about {dur} min"),
            ("参会人", "Participants", E("、".join(mi["participants"])), "")]
    if mi.get("place"):
        meta.append(("地点", "Venue", marks_html(mi["place"]), ""))
    meta.append(("录音", "Recording", E(recording_name), ""))
    h.append('<table class="meta">')
    for zh, en, v1, v2 in meta:
        h.append(f'<tr><td>{zh}<span>{en}</span></td><td>{v1}{f"<span class=en>{E(v2)}</span>" if v2 else ""}</td></tr>')
    h.append('</table>')

    # 一、发言记录
    h.append('<h2><span class="n">一</span>发言记录<span class="en">Transcript</span></h2>')
    legend = ('按时间顺序。<b>点时间就从那一句开始播放录音</b>（会往前多放半秒）。黑字为原话，灰字为译文；'
              '原话中夹带的另一种语言的词用<span class="mix">蓝色</span>标出。')
    if any(MARKS.search(r["zh"] + r["en"]) for r in rows):
        legend += '带<sup class="q">?</sup>的词没能从录音确认；<span class="ina">[听不清]</span> 表示录音里听不出来。'
    legend += '空格键暂停 / 继续。'
    h.append(f'<p class="legend">{legend}</p>')
    h.append('<div class="tr-head"><div>发言人<span>Speaker</span></div><div>中文<span>Chinese</span></div>'
             '<div>英文<span>English</span></div></div>')
    for r in rows:
        if r["orig"] == "zh":
            zh = f'<div class="zh o">{rich_html(r["zh"], "latin")}</div>'
            en = f'<div class="en2 t">{marks_html(r["en"])}</div>'
        else:
            zh = f'<div class="zh t">{marks_html(r["zh"])}</div>'
            en = f'<div class="en2 o">{rich_html(r["en"], "cjk")}</div>'
        h.append(f'<div class="row" data-t="{r["start"]:.2f}" data-who="{E(r["speaker"])}">'
                 f'<div class="who"><b>{E(r["speaker"])}</b>{ts_button(r["start"])}</div>{zh}{en}</div>')

    # 二、会议重点
    h.append('<h2><span class="n">二</span>会议重点<span class="en">Key Points</span></h2>')
    for i, k in enumerate(mi.get("key_points", []), 1):
        title = f'<b>{marks_html(k["title"])}：</b>' if k.get("title") else ""
        h.append(f'<div class="kp"><div class="num">{i}.</div><div>'
                 f'<div class="zh">{title}{marks_html(k["zh"])}{refs_html(k.get("refs"))}</div>'
                 f'<div class="en3">{marks_html(k.get("en", ""))}</div></div></div>')

    # 三、下一步
    h.append('<h2><span class="n">三</span>下一步行动<span class="en">Next Steps</span></h2>')
    steps = sorted(mi.get("next_steps", []), key=lambda s: 0 if s.get("owner") == me else 1)
    h.append('<table class="ns"><thead><tr><th>#</th><th>负责人<span>Owner</span></th><th>事项<span>Action</span></th>'
             '<th>期限<span>Due</span></th><th>出处<span>Ref</span></th></tr></thead><tbody>')
    for i, s in enumerate(steps, 1):
        cls = ' class="me"' if s.get("owner") == me else ""
        h.append(f'<tr{cls}><td class="idx">{i}</td><td class="own">{E(s.get("owner", "—"))}</td>'
                 f'<td>{marks_html(s["zh"])}<div class="en3">{marks_html(s.get("en", ""))}</div></td>'
                 f'<td>{marks_html(s.get("due") or "—")}</td><td>{refs_html(s.get("refs"), "ref") or "—"}</td></tr>')
    h.append('</tbody></table>')
    h.append(f'<div class="foot">录音只保存在这个文件里，不联网。{E(topic)} · {date.isoformat()}</div>')
    h.append('</div>')
    # 录音放在正文后面(页面先显示出来),脚本放在录音后面(读完整段录音才解码)
    h.append('<script type="application/octet-stream" id="audio-data">')
    with open(path, "w", encoding="utf-8") as f:
        f.write("".join(h))
        with open(audio, "rb") as a:
            f.write(base64.b64encode(a.read()).decode("ascii"))
        f.write(f"</script><script>{JS}</script></body></html>\n")


def unique(outdir: Path, stem: str):
    p, v = outdir / f"{stem}{SUFFIX}.html", 1
    while p.exists():
        v += 1
        p = outdir / f"{stem}{SUFFIX}_v{v}.html"
    return p


def build(work: Path, outdir: Path, stem: str, rows=None, tr=None, mi=None, audio_src=None):
    if tr is None:
        tr, mi, entries = load(work)
        rows = build_rows(tr, mi, entries, draft=False)
    src = Path(audio_src or tr["source"]).expanduser()
    if not src.exists():
        sys.exit(f"找不到录音:{src}。用 --audio 指定录音路径。")
    audio, mime = compact_audio(src, work)
    path = unique(outdir, stem)
    render_html(path, tr, mi, rows, audio, mime, Path(tr["source"]).name)
    return path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("work")
    ap.add_argument("--outdir", default=str(DEFAULT_OUT))
    ap.add_argument("--name", help="文件名(不含扩展名和 _可点录音),默认和 Word 版同名")
    ap.add_argument("--audio", help="录音路径,默认用转写时的那个文件")
    args = ap.parse_args()
    work = Path(args.work).expanduser()
    tr, mi, entries = load(work)
    rows = build_rows(tr, mi, entries, draft=False)
    outdir = Path(args.outdir).expanduser()
    outdir.mkdir(parents=True, exist_ok=True)
    stem = args.name or safe(f"{mi['date'].replace('-', '')}_{mi['topic']}_{'、'.join(mi['participants'])}")[:150]
    path = build(work, outdir, stem, rows, tr, mi, args.audio)
    size = path.stat().st_size / 1e6
    print(json.dumps({"html": str(path), "rows": len(rows), "mb": round(size, 1)}, ensure_ascii=False))


if __name__ == "__main__":
    main()

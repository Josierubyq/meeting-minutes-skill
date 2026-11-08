#!/usr/bin/env python3
"""会议录音 -> 带说话人标签的转写草稿(transcript.json + transcript.md)。

全部在本机运行:
  - 区分说话人:sherpa-onnx(pyannote segmentation 3.0 + CAM++ 中英声纹模型)
  - 转写:mlx-whisper large-v3-turbo(Apple Silicon)
  - 中英夹杂:每段单独判断语言;夹在中文里的英文词保持英文原样;
    判断不准的段落中英各转一遍,取平均对数概率更高的结果。
  - 第二套识别 SenseVoice 每段都跑一遍(很快)。两套结果不一致的段落在 transcript.md 里
    附上 alt 行,由 Claude 结合上下文和术语表定稿。在 40 句真人中英夹杂录音(ASCEND)上,
    逐句取两者中较好的,英文词召回从 61% 升到 76%。

用法:
  python transcribe.py AUDIO [--work DIR] [--speakers N] [--language auto|zh|en]
                             [--glossary "术语1, 术语2"] [--threshold 0.5]
"""
import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

import numpy as np

HOME = Path(os.environ.get("MEETING_MINUTES_HOME", Path.home() / ".local/share/meeting-minutes"))
MODELS = HOME / "models"
WHISPER_DIR = MODELS / "whisper-large-v3-turbo"
SEG_MODEL = MODELS / "diarization/sherpa-onnx-pyannote-segmentation-3-0/model.onnx"
EMB_MODEL = MODELS / "diarization/3dspeaker_speech_campplus_sv_zh_en_16k-common_advanced.onnx"
SV_DIR = MODELS / "sensevoice"  # 第二套识别:中英夹杂时用来对照 whisper 的结果
SKILL_DIR = Path(__file__).resolve().parent.parent
SR = 16000

CJK = "一-鿿㐀-䶿"
ZH_EXAMPLE = "比如这个 baseline 的结果，我们下周再 check 一下。"
# 字幕网站常见的幻听句:整段只有这些内容时直接丢弃(记录在 dropped 里)
SPAM = [
    r"请不吝点赞", r"订阅.{0,6}(频道|转发|打赏)", r"字幕(由|制作|提供|志愿者)", r"^谢谢(大家)?(观看|收看)[。!！]*$",
    r"明镜与点点", r"Amara\.org", r"(?i)^thank(s| you) for watching[.!]*$", r"(?i)subtitles? by", r"(?i)please subscribe",
]


def log(msg):
    print(msg, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- audio
def load_audio(path: Path):
    import av

    container = av.open(str(path))
    stream = next(s for s in container.streams if s.type == "audio")
    meta = {**{k: v for k, v in container.metadata.items()}, **{k: v for k, v in stream.metadata.items()}}
    resampler = av.AudioResampler(format="flt", layout="mono", rate=SR)
    parts = []
    for frame in container.decode(stream):
        for f in resampler.resample(frame):
            parts.append(f.to_ndarray().reshape(-1))
    for f in resampler.resample(None):
        parts.append(f.to_ndarray().reshape(-1))
    container.close()
    audio = np.concatenate(parts).astype(np.float32) if parts else np.zeros(0, np.float32)
    return audio, meta


def detect_date(path: Path, meta: dict, duration: float):
    """返回 (录音开始时间 ISO 字符串, 来源, 所有候选)。优先文件名里的日期(通常是录音软件或本人起的),
    其次录音文件元数据,最后用文件修改时间倒推。"""
    cands = {}
    m = re.search(r"(20\d{2})[-_.年 ]?(0[1-9]|1[0-2])[-_.月 ]?(0[1-9]|[12]\d|3[01])", path.name)
    if m:
        t = re.search(r"(?<!\d)([01]\d|2[0-3])[.:_ -]?([0-5]\d)(?:[.:_ -]?[0-5]\d)?(?!\d)", path.name[m.end():])
        hh, mm = (t.group(1), t.group(2)) if t else ("00", "00")
        cands["filename"] = f"{m.group(1)}-{m.group(2)}-{m.group(3)}T{hh}:{mm}"
    for key in ("creation_time", "com.apple.quicktime.creationdate", "date"):
        v = meta.get(key)
        if not v:
            continue
        try:
            d = dt.datetime.fromisoformat(v.replace("Z", "+00:00"))
            if d.tzinfo:
                d = d.astimezone().replace(tzinfo=None)
            if d.year >= 2000:
                cands.setdefault("metadata", d.isoformat(timespec="minutes"))
        except ValueError:
            pass
    st = path.stat()
    # 录音软件通常在结束时写完文件:开始时间 ≈ 修改时间 - 时长
    cands["file_time"] = dt.datetime.fromtimestamp(st.st_mtime - duration).isoformat(timespec="minutes")
    src = next(k for k in ("filename", "metadata", "file_time") if k in cands)
    return cands[src], src, cands


# ---------------------------------------------------------------- diarization
def diarize(audio, num_speakers, threshold):
    import sherpa_onnx

    cfg = sherpa_onnx.OfflineSpeakerDiarizationConfig(
        segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
            pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(model=str(SEG_MODEL)),
            num_threads=4,
        ),
        embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(EMB_MODEL), num_threads=4),
        # 知道人数时先多分 3 组(只用来找换人的位置),再由 recluster() 按声纹归成 N 人。
        # 直接分成 N 组时,常见毛病是一组被几段杂音占掉、两个真人被并成一个。
        clustering=sherpa_onnx.FastClusteringConfig(num_clusters=num_speakers + 3 if num_speakers else -1,
                                                    threshold=threshold),
        min_duration_on=0.3,
        min_duration_off=0.5,
    )
    sd = sherpa_onnx.OfflineSpeakerDiarization(cfg)
    assert sd.sample_rate == SR
    last = [0.0]

    def cb(done, total):
        now = time.time()
        if now - last[0] > 10 or done == total:
            log(f"  区分说话人 {done}/{total}")
            last[0] = now
        return 0

    result = sd.process(audio, callback=cb).sort_by_start_time()
    return [(float(r.start), float(r.end), int(r.speaker)) for r in result]


def pitch_hz(a, max_frames=40):
    """用 YIN 算法粗略估计一段话的音高中位数(Hz)。男声大多 85–180,女声大多 165–260。"""
    fr, hop = int(0.04 * SR), int(0.1 * SR)
    tmin, tmax = SR // 400, SR // 70
    w = fr - tmax
    rms_all = np.sqrt(np.mean(a ** 2)) + 1e-9
    vals = []
    for i in range(0, len(a) - fr, hop):
        x = a[i:i + fr].astype(np.float64)
        if np.sqrt(np.mean(x ** 2)) < 0.5 * rms_all:
            continue
        frames = np.lib.stride_tricks.sliding_window_view(x, w)[: tmax + 1]  # frames[t] = x[t:t+w]
        d = ((frames - frames[0]) ** 2).sum(1)
        cmnd = d[1:] * np.arange(1, tmax + 1) / (np.cumsum(d[1:]) + 1e-12)
        cmnd = np.concatenate([[1.0], cmnd])
        cand = np.where(cmnd[tmin:] < 0.25)[0]
        if len(cand) == 0:
            continue
        t = tmin + cand[0]
        while t + 1 <= tmax and cmnd[t + 1] < cmnd[t]:
            t += 1
        vals.append(SR / t)
        if len(vals) >= max_frames:
            break
    return float(np.median(vals)) if len(vals) >= 3 else None


def recluster(audio, segs, k):
    """把 sherpa 的结果按声纹重新归成 k 个人。

    1. 同一原标签、间隔 ≤0.3 秒的片段连成一个"话轮";
    2. 每个 ≥1.5 秒的话轮提一个声纹,做平均链接聚类(余弦距离);
    3. 从 k 组开始往上加,直到时长占比 ≥3% 的组凑够 k 个,取最大的 k 组当 k 个人
       (零星几段杂音自成小组,不会占掉一个人的位置);
    4. 长话轮按最近的人归类;短话轮跟着它原标签的多数归属走。
    """
    import sherpa_onnx
    from scipy.cluster.hierarchy import fcluster, linkage

    turns = []
    for s_, e_, spk in segs:
        if turns and turns[-1]["raw"] == spk and s_ - turns[-1]["end"] <= 0.3:
            turns[-1]["end"] = max(turns[-1]["end"], e_)
        else:
            turns.append({"start": s_, "end": e_, "raw": spk})
    ext = sherpa_onnx.SpeakerEmbeddingExtractor(sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(EMB_MODEL), num_threads=4))
    long_ids, embs = [], []
    for i, t in enumerate(turns):
        if t["end"] - t["start"] < 1.5:
            continue
        st = ext.create_stream()
        st.accept_waveform(SR, audio[int(t["start"] * SR): int(t["end"] * SR)])
        st.input_finished()
        e = np.array(ext.compute(st))
        long_ids.append(i)
        embs.append(e / (np.linalg.norm(e) + 1e-9))
    if len(long_ids) < k * 2:
        return segs, {}
    E = np.array(embs)
    Z = linkage(E, method="average", metric="cosine")
    dur = np.array([turns[i]["end"] - turns[i]["start"] for i in long_ids])
    total = dur.sum()
    for kk in range(k, k + 8):
        lab = fcluster(Z, kk, criterion="maxclust")
        sizes = {c: dur[lab == c].sum() for c in set(lab)}
        big = [c for c, d in sorted(sizes.items(), key=lambda x: -x[1]) if d >= 0.03 * total]
        if len(big) >= k:
            break
    big = big[:k]
    cent = np.array([E[lab == c].mean(0) for c in big])
    cent /= np.linalg.norm(cent, axis=1, keepdims=True)
    new = {}
    for j, i in enumerate(long_ids):
        new[i] = int(np.argmax(cent @ E[j]))
    from collections import Counter

    raw_major = {}
    for i, c in new.items():
        raw_major.setdefault(turns[i]["raw"], Counter())[c] += turns[i]["end"] - turns[i]["start"]
    raw_major = {r: c.most_common(1)[0][0] for r, c in raw_major.items()}
    out = []
    for i, t in enumerate(turns):
        c = new.get(i, raw_major.get(t["raw"]))
        if c is None:  # 这个原标签一段长话都没有:找时间上最近的长话轮
            j = min(new, key=lambda x: abs(turns[x]["start"] - t["start"]))
            c = new[j]
        out.append((t["start"], t["end"], c))
    # 每人的音高,帮助对应男声/女声
    pitch = {}
    for c in range(k):
        mine = sorted([i for i in long_ids if new[i] == c], key=lambda i: -(turns[i]["end"] - turns[i]["start"]))[:60]
        ps = [pitch_hz(audio[int(turns[i]["start"] * SR): int(turns[i]["end"] * SR)]) for i in mine]
        ps = [x for x in ps if x]
        pitch[c] = round(float(np.median(ps))) if ps else None
    return out, pitch


def quietest_cut(audio, lo, hi, win=0.3):
    """在 [lo, hi] 秒内找能量最低的位置,用于切开过长的发言。"""
    a, b = int(lo * SR), int(hi * SR)
    seg = audio[a:b]
    w = int(win * SR)
    if len(seg) <= w:
        return (lo + hi) / 2
    hop = int(0.05 * SR)
    energies = [float(np.mean(seg[i:i + w] ** 2)) for i in range(0, len(seg) - w, hop)]
    i = int(np.argmin(energies))
    return lo + (i * hop + w / 2) / SR


def build_chunks(segs, audio, max_len=28.0, gap=0.8):
    merged = []
    for s, e, spk in segs:
        if merged and merged[-1]["spk"] == spk and s - merged[-1]["end"] <= gap and e - merged[-1]["start"] <= max_len:
            merged[-1]["end"] = max(merged[-1]["end"], e)
        else:
            merged.append({"start": s, "end": e, "spk": spk})
    out = []
    for c in merged:
        s, e = c["start"], c["end"]
        while e - s > max_len:
            cut = quietest_cut(audio, s + max_len * 0.6, s + max_len)
            out.append({"start": s, "end": cut, "spk": c["spk"]})
            s = cut
        out.append({"start": s, "end": e, "spk": c["spk"]})
    return [c for c in out if c["end"] - c["start"] >= 0.25]


# ---------------------------------------------------------------- ASR
class ASR:
    def __init__(self, glossary_terms):
        import mlx.core as mx
        import mlx_whisper
        from mlx_whisper.audio import N_FRAMES, N_SAMPLES, log_mel_spectrogram, pad_or_trim
        from mlx_whisper.transcribe import ModelHolder

        self.mx, self.mlx_whisper = mx, mlx_whisper
        self._mel = lambda a: pad_or_trim(log_mel_spectrogram(a, n_mels=self.model.dims.n_mels, padding=N_SAMPLES), N_FRAMES, axis=-2)
        self.model = ModelHolder.get_model(str(WHISPER_DIR), mx.float16)
        import sherpa_onnx

        self.sv = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=str(SV_DIR / "model.int8.onnx"), tokens=str(SV_DIR / "tokens.txt"), num_threads=4, use_itn=True)
        self.set_terms(glossary_terms)

    def set_terms(self, terms):
        g = "、".join(terms[:40])
        self.prompts = {
            "zh": "以下是一段中英文夹杂的会议发言，英文单词保持英文原样。" + ZH_EXAMPLE + (f"常用词：{g}。" if g else ""),
            "en": "The following is part of an academic meeting." + (f" Terms: {', '.join(terms[:40])}." if g else ""),
        }
        if len(terms) >= 2:  # 复核时会临时清空术语表,这时保留原来的检查词
            t = terms[:40]
            TERM_PAIRS[:] = [f"{a}{sep}{b}" for a, b in zip(t, t[1:]) for sep in (", ", "、")]

    def lang_probs(self, a):
        mel = self._mel(a).astype(self.mx.float16)
        _, probs = self.model.detect_language(mel)
        return probs

    def alt(self, a):
        st = self.sv.create_stream()
        st.accept_waveform(SR, a)
        self.sv.decode_stream(st)
        t = st.result.text.strip()
        # SenseVoice 把英文写成全大写,统一改成小写,方便和 whisper 对照
        return re.sub(r"[A-Z][A-Z']*", lambda m: m.group().lower(), t)

    def run(self, a, lang, prompt=True):
        r = self.mlx_whisper.transcribe(
            a, path_or_hf_repo=str(WHISPER_DIR), language=lang, initial_prompt=self.prompts.get(lang) if prompt else None,
            # 只在输出重复(压缩比高)时升温重算;不因置信度低而重算,省一半以上时间
            temperature=(0.0, 0.5), logprob_threshold=None, condition_on_previous_text=False, verbose=None,
        )
        segs = r.get("segments", [])
        text = "".join(s["text"] for s in segs).strip() if segs else r.get("text", "").strip()
        ntok = sum(max(len(s.get("tokens", [])), 1) for s in segs) or 1
        lp = sum(s.get("avg_logprob", -5) * max(len(s.get("tokens", [])), 1) for s in segs) / ntok if segs else -5.0
        nsp = max((s.get("no_speech_prob", 0) for s in segs), default=1.0)
        return {"text": text, "logprob": lp, "no_speech": nsp}


# 录音含糊时 whisper 会把提示语原样吐出来,盖掉真正的话(一场录音糊的会里有 25 段)。去掉提示重听一遍就好。
ECHO = re.compile(r"The following is|以下是一段|中英文夹杂|常用词|Terms:")
TERM_PAIRS = []  # 术语表里相邻两个词连写("A, B"),出现在结果里说明是术语表被吐了出来;由 ASR.set_terms 填


def echo_like(text):
    return bool(ECHO.search(text)) or any(p in text for p in TERM_PAIRS) or not text.strip(" 。.,，…")


def looks_stuck(text, alt_text):
    """whisper 偶尔会卡在一个字上无限重复,或者编出比实际长得多的话。"""
    if re.search(r"(.{1,6}?)\1{5,}", text):
        return True
    return len(text) > 3 * len(alt_text) + 30


def toks(t):
    return re.findall(rf"[{CJK}]|[a-z0-9']+", t.lower())


def disagree(a, b):
    """两套结果是否值得让 Claude 对照:英文词不一致,或整体差异超过 15%。"""
    ta, tb = toks(a), toks(b)
    ea, eb = {w for w in ta if w[0].isalpha() and w.isascii()}, {w for w in tb if w[0].isalpha() and w.isascii()}
    if ea != eb:
        return True
    n = max(len(ta), len(tb), 1)
    sa, sb = set(ta), set(tb)
    return len(sa ^ sb) / n > 0.15


def en_share_of(t):
    """按"一个汉字 = 一个英文词"数,英文占多少。用 SenseVoice 的结果估计这一段主要说的是什么语言。"""
    zh = len(re.findall(rf"[{CJK}]", t))
    en = len(re.findall(r"[A-Za-z]+", t))
    return en / (zh + en) if zh + en else None


def script_lang(t):
    if re.search(r"[\u3040-\u30ff]", t):
        return "ja"
    if re.search(r"[\uac00-\ud7af]", t):
        return "ko"
    return None


def cjk_ratio(t):
    letters = re.findall(rf"[{CJK}A-Za-z]", t)
    if not letters:
        return 0.0
    return len(re.findall(rf"[{CJK}]", t)) / len(letters)


def tidy(t, cc):
    t = t.strip()
    if re.search(rf"[{CJK}]", t):
        t = cc.convert(t)  # 繁体 -> 简体
        t = re.sub(rf"([{CJK}])([A-Za-z])", r"\1 \2", t)
        t = re.sub(rf"([A-Za-z])([{CJK}])", r"\1 \2", t)
        if cjk_ratio(t) >= 0.3:  # 中文句子用全角标点;1,000 / 3:30 这种数字里的不动
            t = re.sub(r"(?<!\d),|,(?!\d)", "，", t)
            t = re.sub(r"(?<!\d):|:(?!\d)", "：", t)
            t = t.replace("?", "？").replace("!", "！").replace(";", "；")
            t = re.sub(r"\s*([，。？！：；、])\s*", r"\1", t)
    return re.sub(r"[ \t]+", " ", t).strip()


def short(t, n=70):
    """截到 n 字以内,尽量断在句号、逗号或空格处。"""
    if len(t) <= n:
        return t
    cut = max(t.rfind(ch, 0, n) for ch in "。，！？；.,!?; ")
    return t[: cut + 1 if cut > n // 2 else n].rstrip() + "…"


def fmt(sec):
    sec = int(round(sec))
    return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


# ---------------------------------------------------------------- recheck
def load_terms(extra):
    terms = []
    gfile = SKILL_DIR / "glossary.txt"
    if gfile.exists():
        terms += [l.strip() for l in gfile.read_text(encoding="utf-8").splitlines() if l.strip() and not l.startswith("#")]
    return [t.strip() for t in re.split(r"[,，、]", extra) if t.strip()] + terms


def rtoks(t):
    return re.findall(rf"[{CJK}]|[A-Za-z0-9'\-]+|[^\s]", t)


def recheck(work: Path, ids, candidates, language=None, t_from=None, t_to=None, suspects=""):
    """复核:同一段录音听两遍,一遍不给候选词(对照),一遍只给候选词,列出两次的差别。

    候选词把原来听错的那几个字「替换」掉,才算录音确认;只是「插入」到句子里、
    或落在别的位置,都不算。常用术语表不参与复核,免得把别的词塞进来。
    """
    import difflib

    tr = json.loads((work / "transcript.json").read_text(encoding="utf-8"))
    by_id = {u["id"]: u for u in tr["utterances"]}
    audio, _ = load_audio(Path(tr["source"]))
    from opencc import OpenCC

    cc = OpenCC("t2s")
    cands = [t.strip() for t in re.split(r"[,，、]", candidates) if t.strip()]
    sus = [t.strip() for t in re.split(r"[,，、]", suspects) if t.strip()]
    sus_of = dict(zip(cands, sus))  # 候选词 → 怀疑听错的那几个字(按顺序对应)
    asr = ASR([])
    spans = []
    if t_from is not None and t_to is not None:
        spans.append((f"{fmt(t_from)}–{fmt(t_to)}", t_from, t_to, None))
    for i in ids:
        u = by_id[i]
        spans.append((f"#{i}", u["start"], u["end"], u))
    log_path = work / "recheck.jsonl"
    for label, st, en, u in spans:
        a = audio[int(max(0, st - 0.1) * SR): int((en + 0.2) * SR)]
        alt = tidy(asr.alt(a), cc)
        lang = language or (u["lang"] if u and u["lang"] in ("zh", "en") else None)
        if lang is None:
            r = en_share_of(alt)
            lang = "en" if r is not None and r >= 0.85 else "zh"
        if lang == "en":
            # 英文提示句在含糊录音上会被原样吐出:对照不给提示,候选那遍只给候选词
            ctrl = tidy(asr.run(a, lang, prompt=False)["text"], cc)
            asr.prompts = {"en": ", ".join(cands) + "."}
        else:
            asr.set_terms([])
            ctrl = tidy(asr.run(a, lang)["text"], cc)
            asr.set_terms(cands)
        new = tidy(asr.run(a, lang)["text"], cc)
        if looks_stuck(new, alt):
            new = "(重听失败:输出卡住)"
        ta, tb = rtoks(ctrl), rtoks(new)
        sm = difflib.SequenceMatcher(a=[t.lower() for t in ta], b=[t.lower() for t in tb], autojunk=False)
        tag_of, src_of = {}, {}
        diffs = []
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            for j in range(j1, j2):
                tag_of[j] = tag
                src_of[j] = (i1, i2, j1)
            if tag != "equal":
                sep = lambda xs: re.sub(rf"(?<=[{CJK}]) (?=[{CJK}])", "", " ".join(xs))
                diffs.append(f"{ {'replace': '替换', 'insert': '插入', 'delete': '删除'}[tag] }: \"{sep(ta[i1:i2])}\" → \"{sep(tb[j1:j2])}\"")
        verdicts = {}
        low = [t.lower() for t in tb]
        for c in cands:
            ct = [t.lower() for t in rtoks(c)]
            hit = next((j for j in range(len(low) - len(ct) + 1) if low[j:j + len(ct)] == ct), None) if ct else None
            if hit is None:
                verdicts[c] = "未出现"
                continue
            js = range(hit, hit + len(ct))
            tags = {tag_of.get(j, "equal") for j in js}
            if "replace" not in tags:
                verdicts[c] = "插入(不算确认)" if "insert" in tags else "对照里本来就有"
                continue
            if c not in sus_of:
                verdicts[c] = "替换了一段文字;没给 --suspect,无法判断位置对不对(不算确认)"
                continue
            # 候选词在对照结果里占据的位置:被替换的那段 + 原样对上的字
            replaced = set()
            for j in js:
                i1, i2, j1 = src_of[j]
                if tag_of[j] == "replace":
                    replaced |= set(range(i1, i2))
                elif tag_of[j] == "equal":
                    replaced.add(i1 + (j - j1))
            st_ = [t.lower() for t in rtoks(sus_of[c])]
            la = [t.lower() for t in ta]
            pos = next((i for i in range(len(la) - len(st_) + 1) if la[i:i + len(st_)] == st_), None) if st_ else None
            if pos is None:
                verdicts[c] = f"对照结果里没有「{sus_of[c]}」这几个字;看上面的『替换』行,换掉的是不是听错的那处"
            elif set(range(pos, pos + len(st_))) <= replaced:
                verdicts[c] = f"确认:替换掉了「{sus_of[c]}」"
            else:
                verdicts[c] = f"替换的不是「{sus_of[c]}」,而是别处(不算确认)"
        print(f"[{label} {lang}]")
        if u:
            print(f"  转写稿: {u['text']}")
        print(f"  对照(不给候选词): {ctrl}")
        print(f"  给候选词 {cands}: {new}")
        for d in diffs or ["两次完全一样"]:
            print(f"    {d}")
        for c, v in verdicts.items():
            print(f"  ⇒ {c}: {v}")
        with log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"span": label, "lang": lang, "candidates": cands, "control": ctrl, "with_candidates": new,
                                "diffs": diffs, "verdicts": verdicts}, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("audio", nargs="?")
    ap.add_argument("--recheck", metavar="WORK", help="复核模式:对已转写的工作目录,用新术语表重听 --ids 指定的段落")
    ap.add_argument("--ids", default="", help="复核模式要重听的段落编号,逗号分隔(建议一次一段)")
    ap.add_argument("--suspect", default="", help="复核模式:怀疑听错的那几个字,和 --glossary 的候选词按顺序对应")
    ap.add_argument("--from", dest="t_from", type=float, help="复核模式:只重听录音里这一段的开始秒数")
    ap.add_argument("--to", dest="t_to", type=float, help="复核模式:只重听录音里这一段的结束秒数")
    ap.add_argument("--relabel", metavar="WORK", help="不重新识别文字,只按声纹把已有转写稿的说话人重新归成 --speakers 人")
    ap.add_argument("--repass", metavar="WORK", help="只把吐出提示语或结果为空的段落去掉提示重听(不重转整场)")
    ap.add_argument("--work", help="工作目录(默认 ~/.local/share/meeting-minutes/work/<录音名>)")
    ap.add_argument("--speakers", type=int, default=0, help="说话人数(知道就填,区分更准)")
    ap.add_argument("--language", default="auto", choices=["auto", "zh", "en"])
    ap.add_argument("--glossary", default="", help="本次会议的专有名词,逗号分隔")
    ap.add_argument("--threshold", type=float, default=0.5, help="不知道人数时的聚类阈值,越大人数越少")
    args = ap.parse_args()

    if args.recheck:
        ids = [int(x) for x in re.split(r"[,\s]+", args.ids) if x]
        if not args.glossary:
            ap.error("复核需要 --glossary 给出 1–2 个候选词")
        lang = None if args.language == "auto" else args.language
        return recheck(Path(args.recheck).expanduser(), ids, args.glossary, lang, args.t_from, args.t_to, args.suspect)
    if args.repass:
        return repass(Path(args.repass).expanduser())
    if args.relabel:
        if not args.speakers:
            ap.error("--relabel 需要 --speakers N")
        return relabel(Path(args.relabel).expanduser(), args.speakers)
    if not args.audio:
        ap.error("需要录音文件路径")
    src = Path(args.audio).expanduser().resolve()
    tag = hashlib.md5(str(src).encode()).hexdigest()[:6]
    work = Path(args.work).expanduser() if args.work else HOME / "work" / f"{src.stem}_{tag}"
    work.mkdir(parents=True, exist_ok=True)

    terms = load_terms(args.glossary)

    t0 = time.time()
    log(f"读取录音 {src.name}")
    audio, meta = load_audio(src)
    duration = len(audio) / SR
    if duration < 1:
        sys.exit("录音为空或无法解码")
    recorded_at, date_source, date_cands = detect_date(src, meta, duration)
    log(f"  时长 {fmt(duration)},录音时间 {recorded_at}(来源:{date_source})")

    log("区分说话人 ...")
    segs = diarize(audio, args.speakers, args.threshold)
    pitch = {}
    if args.speakers:
        log(f"  按声纹重新归成 {args.speakers} 人 ...")
        segs, pitch = recluster(audio, segs, args.speakers)
    chunks = build_chunks(segs, audio)
    log(f"  得到 {len(set(s[2] for s in segs))} 位说话人,{len(chunks)} 段")

    from opencc import OpenCC

    cc = OpenCC("t2s")
    asr = ASR(terms)

    def clip(c):
        a = int(max(0, c["start"] - 0.1) * SR)
        b = int(min(duration, c["end"] + 0.2) * SR)
        return audio[a:b]

    # 第一遍:SenseVoice 快速转一遍(每小时约 1 分钟),按中英字数估计每段语言,再判断整场是不是英文会议。
    # 只有 SenseVoice 没听出字的段落才用 whisper 的语言识别(每段约 1 秒)。
    log("第一遍快速识别,判断每段语言 ...")
    for c in chunks:
        a = clip(c)
        c["alt"] = tidy(asr.alt(a), cc)
        c["other_lang"] = script_lang(c["alt"])
        if args.language != "auto":
            c["r_en"] = 1.0 if args.language == "en" else 0.0
            continue
        r = en_share_of(c["alt"])
        if r is None:
            p = asr.lang_probs(a)
            r = p.get("en", 0.0) / (p.get("zh", 0.0) + p.get("en", 0.0) + 1e-9)
        c["r_en"] = r
    total = sum(c["end"] - c["start"] for c in chunks) or 1
    en_share = sum(c["end"] - c["start"] for c in chunks if c["r_en"] > 0.5) / total
    mode = args.language if args.language != "auto" else ("english" if en_share >= 0.8 else "mixed")
    log(f"  英文占比 {en_share:.0%},按{'英文会议' if mode == 'english' else '中英混合会议' if mode == 'mixed' else mode}处理")

    log("转写 ...")

    def pick_order(r_en):
        if mode == "english":
            return ["zh"] if r_en < 0.15 else ["en"] if r_en > 0.5 else ["en", "zh"]
        return ["en"] if r_en >= 0.85 else ["zh"] if r_en <= 0.3 else ["zh", "en"]

    def recognize(a, r_en, other_lang, depth=0, alt=None):
        """返回 dict(lang, text, logprob, no_speech, flags, alt)。"""
        flags = []
        if mode in ("zh", "en"):
            order = [mode]
        elif other_lang:
            order = [other_lang]
            flags.append(f"other_language:{other_lang}")
        else:
            order = pick_order(r_en)
            if len(order) > 1:
                flags.append("lang_checked_both")
        cands = [(lg, asr.run(a, lg)) for lg in order]
        if alt is None:
            alt = tidy(asr.alt(a), cc)
        ok = [x for x in cands if not looks_stuck(x[1]["text"], alt)]
        if not ok and mode not in ("zh", "en"):  # 卡住了:换另一种语言再试
            other = "en" if order[0] == "zh" else "zh"
            retry = (other, asr.run(a, other))
            ok = [retry] if not looks_stuck(retry[1]["text"], alt) else []
        if not ok and depth == 0 and len(a) > 3 * SR:
            # 常见于一句话前半中文、后半英文:从中间最安静处切开,两半分别识别
            cut = quietest_cut(a, len(a) / SR * 0.3, len(a) / SR * 0.7)
            parts = []
            for half in (a[: int(cut * SR)], a[int(cut * SR):]):
                h_alt = tidy(asr.alt(half), cc)
                re_ = en_share_of(h_alt)
                if re_ is None:
                    p = asr.lang_probs(half)
                    re_ = p.get("en", 0) / (p.get("en", 0) + p.get("zh", 0) + 1e-9)
                parts.append(recognize(half, re_, None, depth=1, alt=h_alt))
            if not any("whisper_stuck_used_alt" in x["flags"] for x in parts):
                text = tidy(" ".join(x["text"] for x in parts), cc)
                return {"lang": "zh" if cjk_ratio(text) >= 0.3 else "en", "text": text,
                        "logprob": min(x["logprob"] for x in parts), "no_speech": 0.0,
                        "flags": flags + ["split_mixed"], "alt": alt}
        if ok:
            lang, res = max(ok, key=lambda x: x[1]["logprob"])
            if echo_like(res["text"]):
                r2 = asr.run(a, lang, prompt=False)
                if not echo_like(r2["text"]):
                    res = r2
                    flags.append("prompt_echo_retried")
            text = tidy(res["text"], cc) or alt
            if not tidy(res["text"], cc):
                flags.append("whisper_empty_used_alt")
            return {"lang": lang, "text": text, "logprob": res["logprob"], "no_speech": res["no_speech"],
                    "flags": flags, "alt": alt}
        return {"lang": "zh" if cjk_ratio(alt) >= 0.3 else "en", "text": alt, "logprob": -9.0, "no_speech": 0.0,
                "flags": flags + ["whisper_stuck_used_alt"], "alt": alt}

    utts, dropped = [], []
    t1, last = time.time(), 0.0
    for i, c in enumerate(chunks):
        a = clip(c)
        R = recognize(a, c["r_en"], c.get("other_lang"), alt=c["alt"])
        text, alt, flags, lang = R["text"], R["alt"], R["flags"], R["lang"]
        if not text:
            continue
        if any(re.search(p, text) for p in SPAM) and len(text) < 40:
            dropped.append({"start": round(c["start"], 2), "text": text, "reason": "subtitle_spam"})
            continue
        if ZH_EXAMPLE[:8] in text or "中英文夹杂的会议发言" in text:
            flags.append("possible_prompt_echo")
        if R["logprob"] < -1.0 or R["no_speech"] > 0.6:
            flags.append("low_confidence")
        out_lang = lang if lang not in ("zh", "en") else ("zh" if cjk_ratio(text) >= 0.3 else "en")
        u = {"id": len(utts) + 1, "start": round(c["start"], 2), "end": round(c["end"], 2), "spk_raw": c["spk"],
             "lang": out_lang, "text": text, "logprob": round(R["logprob"], 3), "flags": flags}
        # SenseVoice 的价值在中英夹杂的中文段;纯英文段 whisper 明显更好,除非 whisper 出了问题才附上
        if alt and alt != text and (out_lang == "zh" or {"low_confidence", "whisper_stuck_used_alt"} & set(flags)) \
                and disagree(text, alt):
            u["alt"] = alt
        utts.append(u)
        now = time.time()
        if now - last > 15 or i == len(chunks) - 1:
            done = i + 1
            eta = (now - t1) / done * (len(chunks) - done)
            log(f"  {done}/{len(chunks)} 段,预计还需 {int(eta // 60)} 分 {int(eta % 60)} 秒")
            last = now

    for u in utts:
        u["speaker"] = u.pop("spk_raw")
    speakers = label_speakers(utts, pitch)

    out = {
        "source": str(src), "duration_sec": round(duration, 1), "recorded_at": recorded_at, "date_source": date_source,
        "date_candidates": date_cands, "language_mode": mode, "english_share": round(en_share, 3), "speakers": speakers,
        "utterances": utts, "dropped": dropped, "glossary_used": terms[:40],
        "elapsed_sec": round(time.time() - t0, 1),
    }
    save_outputs(work, out)

    log(f"完成,用时 {fmt(time.time() - t0)}")
    print(json.dumps({"work": str(work), "utterances": len(utts), "speakers": len(speakers),
                      "duration": fmt(duration), "recorded_at": recorded_at, "date_source": date_source,
                      "language_mode": mode}, ensure_ascii=False))


def label_speakers(utts, pitch=None):
    """把 utts 里的原始说话人标签按首次出场改成 S1, S2 ...,返回每人的统计(时长、段数、例句、音高)。"""
    pitch = pitch or {}
    order = []
    for u in utts:
        if u["speaker"] not in order:
            order.append(u["speaker"])
    label = {k: f"S{i + 1}" for i, k in enumerate(order)}
    speakers = {}
    for u in utts:
        raw = u["speaker"]
        u["speaker"] = label[raw]
        sp = speakers.setdefault(u["speaker"], {"seconds": 0.0, "turns": 0, "samples": [], "pitch_hz": pitch.get(raw)})
        sp["seconds"] += u["end"] - u["start"]
        sp["turns"] += 1
    for k, sp in speakers.items():
        mine = sorted(sorted([u for u in utts if u["speaker"] == k], key=lambda u: -len(u["text"]))[:3], key=lambda u: u["id"])
        sp["samples"] = [f"[#{u['id']} {fmt(u['start'])}] {short(u['text'])}" for u in mine]
        sp["seconds"] = round(sp["seconds"], 1)
    return speakers


def backup(work: Path, tag: str):
    for name in ("transcript.json", "transcript.md"):  # 旧稿另存,不覆盖已有备份
        stem, ext = name.split(".")
        dst, v = work / f"{stem}_before_{tag}.{ext}", 1
        while dst.exists():
            v += 1
            dst = work / f"{stem}_before_{tag}{v}.{ext}"
        shutil.copy(work / name, dst)


def repass(work: Path):
    """不重转整场:只把吐出提示语或结果为空的段落去掉提示重听一遍(老版本脚本转的稿子用)。"""
    from opencc import OpenCC

    tr = json.loads((work / "transcript.json").read_text(encoding="utf-8"))
    todo = [u for u in tr["utterances"] if echo_like(u["text"]) or "possible_prompt_echo" in u["flags"]]
    if not todo:
        print(json.dumps({"work": str(work), "redone": 0}, ensure_ascii=False))
        return
    backup(work, "repass")
    audio, _ = load_audio(Path(tr["source"]))
    asr, cc = ASR([]), OpenCC("t2s")
    fixed = []
    log(f"去掉提示重听 {len(todo)} 段 ...")
    for u in todo:
        a = audio[int(max(0, u["start"] - 0.1) * SR): int(min(len(audio) / SR, u["end"] + 0.2) * SR)]
        lang = u["lang"] if u["lang"] in ("zh", "en") else "en"
        r = asr.run(a, lang, prompt=False)
        new = tidy(r["text"], cc)
        if new and not echo_like(new):
            fixed.append({"id": u["id"], "old": u["text"], "new": new})
            u["text"] = new
            u["flags"] = [f for f in u["flags"] if f != "possible_prompt_echo"] + ["prompt_echo_retried"]
            if r["logprob"] < -1.0 and "low_confidence" not in u["flags"]:
                u["flags"].append("low_confidence")
    save_outputs(work, tr)
    log("完成")
    for f in fixed:
        print(f"#{f['id']}: {short(f['old'], 50)}  ->  {short(f['new'], 80)}")
    print(json.dumps({"work": str(work), "checked": len(todo), "redone": len(fixed)}, ensure_ascii=False))


def relabel(work: Path, k: int):
    """不重新识别文字,只按声纹把已有转写稿的说话人重新归成 k 人(自动分出的人数明显不对时用)。"""
    tr = json.loads((work / "transcript.json").read_text(encoding="utf-8"))
    n_old = len(tr["speakers"])
    backup(work, "relabel")
    log(f"读取录音 {Path(tr['source']).name}")
    audio, _ = load_audio(Path(tr["source"]))
    utts = sorted(tr["utterances"], key=lambda u: u["start"])
    log(f"按声纹把 {n_old} 位说话人重新归成 {k} 人 ...")
    segs = [(u["start"], u["end"], u["speaker"]) for u in utts]
    turns, pitch = recluster(audio, segs, k)
    if not pitch:
        sys.exit("超过 1.5 秒的发言太少,没法按声纹重新分组;请用 --speakers N 重新转写")
    for u in utts:
        mid = (u["start"] + u["end"]) / 2
        hit = [t for t in turns if t[0] - 0.01 <= mid <= t[1] + 0.01]
        t = hit[0] if hit else min(turns, key=lambda t: abs((t[0] + t[1]) / 2 - mid))
        u["speaker"] = t[2]
    tr["utterances"] = sorted(utts, key=lambda u: u["id"])
    tr["speakers"] = label_speakers(tr["utterances"], pitch)
    tr["relabeled"] = {"from_speakers": n_old, "to_speakers": k}
    save_outputs(work, tr)
    log("完成")
    print(json.dumps({"work": str(work), "speakers_before": n_old, "speakers": len(tr["speakers"]),
                      "minutes": {s: round(v["seconds"] / 60, 1) for s, v in tr["speakers"].items()},
                      "pitch_hz": {s: v["pitch_hz"] for s, v in tr["speakers"].items()}}, ensure_ascii=False))


def save_outputs(work: Path, out: dict):
    """写 transcript.json 和给人读的 transcript.md。"""
    (work / "transcript.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    src, duration, utts, speakers = Path(out["source"]), out["duration_sec"], out["utterances"], out["speakers"]
    recorded_at, date_source, date_cands = out["recorded_at"], out["date_source"], out["date_candidates"]
    mode, en_share, dropped = out["language_mode"], out["english_share"], out.get("dropped", [])
    md = [f"# 转写草稿:{src.name}", "",
          f"- 时长 {fmt(duration)};录音时间 {recorded_at}(来源 {date_source};全部候选 {date_cands});处理方式 {mode};英文占比 {en_share:.0%}", "",
          "## 说话人", ""]
    if out.get("relabeled"):
        md[3:3] = [f"- 说话人已按声纹从 {out['relabeled']['from_speakers']} 位重新归成 {out['relabeled']['to_speakers']} 位"]
    for k, sp in speakers.items():
        ph = sp.get("pitch_hz")
        voice = "" if not ph else f",音高约 {ph} Hz(" + ("多半是男声" if ph < 160 else "多半是女声" if ph > 180 else "男女难分") + ")"
        md.append(f"### {k}:发言 {sp['seconds'] / 60:.1f} 分钟,{sp['turns']} 段{voice}")
        md += [f"- {s}" for s in sp["samples"]]
        md.append("")
    n_alt = sum(1 for u in utts if "alt" in u)
    md += ["## 逐段转写", "",
           "格式:[#编号 开始时间 说话人 语言 {标记}] whisper 结果;下一行 `alt:` 是 SenseVoice 结果(两者不一致时才列出)。",
           f"共 {len(utts)} 段,其中 {n_alt} 段两套结果不一致。", ""]
    for u in utts:
        fl = (" {" + ",".join(u["flags"]) + "}") if u["flags"] else ""
        md.append(f"[#{u['id']} {fmt(u['start'])} {u['speaker']} {u['lang']}{fl}] {u['text']}")
        if "alt" in u:
            md.append(f"    alt: {u['alt']}")
    if dropped:
        md += ["", "## 已自动丢弃(字幕网站幻听句)", ""] + [f"- {fmt(d['start'])} {d['text']}" for d in dropped]
    (work / "transcript.md").write_text("\n".join(md) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()

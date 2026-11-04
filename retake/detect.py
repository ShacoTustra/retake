"""Find edit candidates (bad takes, stutters, long pauses, fillers) in a transcript.

Input : transcript JSON from transcribe.py (+ the source video for audio/screen analysis)
Output: edits JSON consumed by review.html / render.py

Every candidate is a *suggestion*. Language teachers repeat things on purpose
("repeat after me: こんにちは… こんにちは"), so the human makes the final call.
"""
import argparse
import json
import re
import subprocess
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from rapidfuzz import fuzz

SR = 16000

# ---------------------------------------------------------------- text utils

FILLER_RE = re.compile(r"^(ээ+|э-э+|э э+|эм+|ммм+|а-а+|хм+|ээм|えー+と?|えっと|あのー|うーん)$")
# phrases that signal "that was wrong, again"
RESTART_MARKERS = re.compile(
    r"\b(еще раз|заново|стоп|подожди|подождите|ой|тьфу|блин|черт)\b"
)
# discourse words that often precede a restarted sentence: "Так, давайте..." vs "Ну что, давайте..."
LEADING_FILLERS = {"ну", "так", "итак", "вот", "и", "а", "но", "что", "окей", "ок", "хорошо", "значит"}


def strip_leading(tokens: list[str]) -> list[str]:
    i = 0
    while i < len(tokens) - 1 and i < 3 and tokens[i] in LEADING_FILLERS:
        i += 1
    return tokens[i:]


MARKER_WORDS = {"ой", "блин", "тьфу", "черт", "стоп", "заново", "подожди", "подождите"}


def marker_word_index(p: "Phrase") -> int | None:
    """Index of the word where a restart marker ("ой", "блин", "ещё раз") begins."""
    toks = [w.n.split()[0] if w.n else "" for w in p.words]
    for k, t in enumerate(toks):
        if t in MARKER_WORDS:
            return k
        # "ещё раз" is also ordinary teacher talk ("прочитаем ещё раз"): only a short
        # stand-alone "Ещё раз." / "Окей, ещё раз." counts as a restart
        if t == "еще" and k + 1 < len(toks) and toks[k + 1].startswith("раз") and len(p.ntokens()) <= 3:
            return k
    return None


def is_marker_only(p: "Phrase") -> bool:
    """A phrase that opens with a marker: 'Ещё раз.', 'Тьфу, блин.', 'Блин, я запуталась…'."""
    k = marker_word_index(p)
    return k is not None and k <= 1 and len(p.ntokens()) <= 8


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s).lower().replace("ё", "е")
    s = re.sub(r"[^\w\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


@dataclass
class Word:
    start: float
    end: float
    text: str
    p: float

    @property
    def n(self) -> str:
        return norm(self.text)


@dataclass
class Phrase:
    words: list[Word] = field(default_factory=list)

    @property
    def start(self):
        return self.words[0].start

    @property
    def end(self):
        return self.words[-1].end

    @property
    def text(self):
        return "".join(w.text for w in self.words).strip()

    @property
    def n(self):
        return norm(self.text)

    def ntokens(self):
        return " ".join(w.n for w in self.words).split()


def load_words(tr: dict) -> list[Word]:
    words = []
    for s in tr["segments"]:
        for w in s["words"]:
            if w["word"].strip():
                words.append(Word(w["start"], w["end"], w["word"], w.get("p", 1.0)))
    words.sort(key=lambda w: w.start)
    return words


def split_phrases(words: list[Word], gap: float) -> list[Phrase]:
    phrases, cur = [], Phrase()
    for w in words:
        if cur.words:
            prev = cur.words[-1]
            pause = w.start - prev.end
            sentence_end = prev.text.rstrip().endswith((".", "?", "!", "。", "？", "！"))
            if pause > gap or sentence_end:
                phrases.append(cur)
                cur = Phrase()
        cur.words.append(w)
    if cur.words:
        phrases.append(cur)
    return phrases


# ---------------------------------------------------------------- audio utils

def load_audio(src: Path) -> np.ndarray:
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(src), "-vn", "-ac", "1", "-ar", str(SR), "-f", "s16le", "-"],
        check=True, capture_output=True,
    ).stdout
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def rms_envelope(audio: np.ndarray, hop: float = 0.01) -> np.ndarray:
    h = int(SR * hop)
    n = len(audio) // h
    frames = audio[: n * h].reshape(n, h)
    return 20 * np.log10(np.sqrt((frames**2).mean(axis=1)) + 1e-9)


def snap_quiet(env: np.ndarray, t: float, lo: float, hi: float, hop: float = 0.01) -> float:
    """Move a cut point to the quietest 10ms frame within [lo, hi] (closest to t on ties)."""
    a, b = max(0, int(lo / hop)), min(len(env) - 1, int(hi / hop))
    if b <= a:
        return t
    seg = env[a : b + 1]
    floor = seg.min()
    quiet = np.where(seg <= floor + 3.0)[0] + a  # within 3 dB of the minimum
    best = quiet[np.argmin(np.abs(quiet * hop - t))]
    return float(best * hop)


# ---------------------------------------------------------------- screen utils

def frozen_intervals(src: Path, start: float, duration: float | None) -> list[tuple[float, float]]:
    """Intervals where the screen does not change (ffmpeg freezedetect)."""
    cmd = ["ffmpeg", "-hide_banner", "-ss", str(start)]
    if duration:
        cmd += ["-t", str(duration)]
    cmd += ["-i", str(src), "-an", "-vf", "fps=5,scale=560:-2,freezedetect=n=0.001:d=0.5", "-f", "null", "-"]
    err = subprocess.run(cmd, capture_output=True, text=True).stderr
    out, s = [], None
    for line in err.splitlines():
        m = re.search(r"freeze_start: ([\d.]+)", line)
        if m:
            s = float(m.group(1)) + start
        m = re.search(r"freeze_end: ([\d.]+)", line)
        if m and s is not None:
            out.append((s, float(m.group(1)) + start))
            s = None
    if s is not None:
        out.append((s, start + (duration or 1e9)))
    return out


def screen_static(frozen, a: float, b: float) -> bool:
    return any(fs <= a + 0.25 and fe >= b - 0.25 for fs, fe in frozen)


# ---------------------------------------------------------------- detectors

def take_similarity(pi: Phrase, pj: Phrase) -> float:
    """How much phrase i looks like the opening of a later phrase j (0..100)."""
    a, b = strip_leading(pi.ntokens()), strip_leading(pj.ntokens())
    if len(a) < 3 or len(b) < 2:
        return 0.0
    # a retake starts the same way: parallel explanations ("показатель тематического падежа" /
    # "это показатель рематического падежа") are similar overall but open differently
    if fuzz.ratio(" ".join(a[:2]), " ".join(b[:2])) < 80:
        return 0.0
    sa, sb = " ".join(a), " ".join(b)
    # whole i ~ beginning of j: an abandoned or repeated sentence
    s_full = fuzz.ratio(sa, sb[: len(sa)]) if len(sa) >= 10 else 0.0
    # same opening words; two words ("Ян-сан, ...") are too weak a signal
    k = min(len(a), len(b), 5)
    s_pref = fuzz.ratio(" ".join(a[:k]), " ".join(b[:k])) if k >= 3 else 0.0
    return max(s_full, s_pref)


def detect_retakes(phrases: list[Phrase], window: float, threshold: float = 82) -> list[dict]:
    """'Last take wins': if phrase i is re-said as a later phrase j, cut from start(i) to start(j).

    Confidence:
      - a restart marker in between ("ещё раз", "тьфу", "ой")  -> cut
      - i is a cut-off / reworded version of j                 -> cut
      - i and j are the same complete sentence, no marker      -> probably intentional
        (a teacher reading an example twice), suggested but not pre-selected
    """
    cands = []
    for i, pi in enumerate(phrases):
        best_j, best = None, 0.0
        for j in range(i + 1, len(phrases)):
            pj = phrases[j]
            if pj.start - pi.end > window:
                break
            sc = take_similarity(pi, pj)
            if sc >= threshold and sc >= best:  # ">=": the latest matching take wins
                best_j, best = j, sc
        if best_j is None:
            continue
        between = phrases[i:best_j]
        pj = phrases[best_j]
        marker = any(RESTART_MARKERS.search(p.n) for p in between)
        la, lb = len(" ".join(strip_leading(pi.ntokens()))), len(" ".join(strip_leading(pj.ntokens())))
        removed_n = " ".join(strip_leading(" ".join(p.n for p in between).split()))
        kept_n = " ".join(strip_leading(pj.ntokens()))
        identical = fuzz.ratio(removed_n, kept_n) >= 90
        if marker:
            why, on = "есть «ещё раз / ой / тьфу»", True
        elif identical:
            why, on = "точный повтор — возможно, намеренный", False
        elif la < 0.8 * lb:
            why, on = "оборванная фраза, потом сказана целиком", True
        elif len(strip_leading(pi.ntokens())) <= 4:
            why, on = "короткая фраза перефразирована — возможно, вариант перевода", False
        else:
            why, on = "фраза переформулирована", True
        cands.append({
            "kind": "retake", "start": pi.start, "end": pj.start, "score": round(best),
            "marker": marker, "why": why, "on": on,
            "removed": " ‖ ".join(p.text for p in between), "kept": pj.text,
        })

    # resolve overlaps: a cut must never swallow the take another cut decided to keep
    cands.sort(key=lambda c: (not c["marker"], not c["on"], -c["score"], c["start"]))
    accepted = []
    for c in cands:
        ok = True
        for a in accepted:
            if a["start"] <= c["start"] and c["end"] <= a["end"]:
                ok = False  # already covered
            elif c["start"] <= a["end"] < c["end"] or a["start"] <= c["end"] < a["end"]:
                ok = False  # would cut the kept take of the other
            if not ok:
                break
        if ok:
            accepted.append(c)

    # stand-alone markers ("Окей, ещё раз") not explained by a matched retake:
    # cut the marker(s) plus the phrase right before them
    def covered(t):
        return any(a["start"] <= t < a["end"] for a in accepted)

    i = 0
    while i < len(phrases):
        p = phrases[i]
        if not (is_marker_only(p) and not covered(p.start)):
            i += 1
            continue
        # chain "ещё раз … ну, всё просто … ещё раз": short chatter between markers goes too
        j = i
        while True:
            nxt_marker = None
            for k in range(j + 1, min(j + 4, len(phrases))):
                if is_marker_only(phrases[k]):
                    nxt_marker = k
                    break
                if len(phrases[k].ntokens()) > 3:
                    break
            if nxt_marker is None or phrases[nxt_marker].start - phrases[j].end > 8:
                break
            j = nxt_marker
        # the failed attempt is the sentence right before the marker (walk back to its start)
        first = i
        if i > 0 and p.start - phrases[i - 1].end < 2.0 and not covered(phrases[i - 1].start):
            first = i - 1
            while (first > 0 and not covered(phrases[first - 1].start)
                   and phrases[first].start - phrases[first - 1].end < 1.0
                   and not phrases[first - 1].text.rstrip().endswith((".", "?", "!", "。"))):
                first -= 1
        start = phrases[first].start
        nxt = phrases[j + 1] if j + 1 < len(phrases) else None
        accepted.append({
            "kind": "retake", "start": start, "end": nxt.start if nxt else phrases[j].end,
            "score": None, "marker": True, "on": True,
            "why": "маркер без явного повтора — проверьте, откуда начать вырезку",
            "removed": " ‖ ".join(q.text for q in phrases[first: j + 1]),
            "kept": nxt.text if nxt else "",
        })
        i = j + 1

    # marker in the middle of a phrase: "…мы бы сказали «еще…» Ой, почему «еще?»"
    # cut the abandoned fragment + the marker clause, keep the beginning of the sentence
    for p in phrases:
        k = marker_word_index(p)
        if k is None or k <= 1 or covered(p.start) or covered(p.words[k].start):
            continue
        s_idx = 0
        prev = p.words[k - 1].text.strip().strip("«»\"'")
        if prev.endswith(("…", "...")):
            s_idx = k - 1  # the abandoned word itself
        else:
            for m in range(k - 1, 0, -1):
                if p.words[m].start - p.words[m - 1].end >= 0.3:
                    s_idx = m
                    break
        nxt = next((q for q in phrases if q.start >= p.end), None)
        accepted.append({
            "kind": "retake", "start": p.words[s_idx].start, "end": nxt.start if nxt else p.end,
            "score": None, "marker": True, "on": True,
            "why": "маркер посреди фразы — проверьте границы",
            "removed": "".join(w.text for w in p.words[s_idx:]).strip(),
            "kept": nxt.text if nxt else "",
        })
    return accepted


def detect_stutters(words: list[Word]) -> list[dict]:
    """Adjacent repeated n-grams: 'это это', 'в этом, в этом предложении'."""
    out = []
    toks = [w.n for w in words]
    i = 0
    while i < len(words):
        found = False
        for n in (3, 2, 1):
            if i + 2 * n > len(words):
                continue
            a, b = toks[i : i + n], toks[i + n : i + 2 * n]
            if not all(a) or a != b:
                continue
            if n == 1 and len(a[0]) < 2:
                continue
            if words[i + n].start - words[i + n - 1].end > 2.0:
                continue
            out.append({
                "kind": "stutter",
                "start": words[i].start,
                "end": words[i + n].start,
                "removed": "".join(w.text for w in words[i : i + n]).strip(),
                "kept": "".join(w.text for w in words[i + n : i + 2 * n]).strip(),
            })
            i += n
            found = True
            break
        if not found:
            i += 1
    return out


def detect_fillers(words: list[Word]) -> list[dict]:
    out = []
    for w in words:
        if "«" in w.text or '"' in w.text:  # quoted = being talked about, not a filler
            continue
        if FILLER_RE.match(w.n or "~"):
            out.append({"kind": "filler", "start": w.start, "end": w.end, "removed": w.text.strip()})
    return out


def silence_threshold(env: np.ndarray) -> float:
    """Noise floor + margin; capped so a noisy room still gets a sane value."""
    floor = float(np.percentile(env, 10))
    return min(floor + 15.0, -45.0)


def silent_runs(env: np.ndarray, a: float, b: float, thr: float, hop: float = 0.01,
                bridge: float = 0.15) -> list[tuple[float, float]]:
    """Truly silent stretches inside [a, b]. Blips shorter than `bridge` (mouse clicks) are ignored."""
    i0, i1 = max(0, int(a / hop)), min(len(env), int(b / hop))
    if i1 - i0 < 2:
        return []
    m = env[i0:i1] < thr
    # bridge short non-silent blips
    k, n = 0, len(m)
    while k < n:
        if not m[k]:
            j = k
            while j < n and not m[j]:
                j += 1
            if 0 < k and j < n and (j - k) * hop < bridge:
                m[k:j] = True
            k = j
        else:
            k += 1
    runs, k = [], 0
    while k < n:
        if m[k]:
            j = k
            while j < n and m[j]:
                j += 1
            runs.append(((i0 + k) * hop, (i0 + j) * hop))
            k = j
        else:
            k += 1
    return runs


def detect_pauses(words: list[Word], env: np.ndarray, min_pause: float, keep: float,
                  frozen, t0: float, t1: float) -> list[dict]:
    """Shorten long silences to `keep` seconds.

    Word gaps from Whisper only say *where to look*; the cut itself is placed inside
    measured silence, because Whisper word boundaries can be off by half a second.
    """
    thr = silence_threshold(env)
    out = []
    edges = [(t0, words[0].start)] + [(x.end, y.start) for x, y in zip(words, words[1:])] + [(words[-1].end, t1)]
    for k, (a, b) in enumerate(edges):
        if b - a < min_pause:
            continue
        for rs, re_ in silent_runs(env, a, b, thr):
            if re_ - rs < min_pause:
                continue
            head = 0.0 if (k == 0 and rs <= t0 + 0.05) else keep / 2
            tail = 0.0 if (k == len(edges) - 1 and re_ >= t1 - 0.05) else keep / 2
            cs, ce = rs + head, re_ - tail
            if ce - cs < 0.2:
                continue
            static = screen_static(frozen, rs, re_)
            out.append({
                "kind": "pause", "start": cs, "end": ce, "static": static,
                "removed": f"пауза {re_ - rs:.1f} с" + ("" if static else " (на экране движение)"),
            })
    return out


def snap_to_gap(env: np.ndarray, t: float, floor_thr: float, lo: float, hi: float) -> float:
    """Move a cut point into the nearest real gap between words.

    Whisper word times can be off by ~0.5 s in either direction, so we look both ways,
    prefer longer gaps, and cut in the middle of the gap (keeps a natural breath on both sides).
    """
    thr = min(max(floor_thr + 10.0, -55.0), -40.0)
    runs = [(a, b) for a, b in silent_runs(env, lo, hi, thr, bridge=0.03) if b - a >= 0.06]
    if not runs:
        return snap_quiet(env, t, max(lo, t - 0.3), min(hi, t + 0.1))

    def score(r):
        a, b = r
        dist = 0.0 if a <= t <= b else min(abs(t - a), abs(t - b))
        return min(b - a, 0.3) - 0.5 * dist

    a, b = max(runs, key=score)
    return (a + b) / 2


def phrase_index(phrases: list[Phrase]) -> list[dict]:
    out, k = [], 0
    for p in phrases:
        out.append({"start": round(p.start, 3), "end": round(p.end, 3), "w0": k, "w1": k + len(p.words)})
        k += len(p.words)
    return out


# ---------------------------------------------------------------- pipeline

@dataclass
class Options:
    phrase_gap: float = 0.5      # a pause longer than this splits phrases
    retake_window: float = 25.0  # how far ahead to look for the next take
    min_pause: float = 1.2       # silences longer than this are shortened ...
    keep_pause: float = 0.8      # ... to this


def analyze_media(src: Path, t0: float, t1: float | None, cache_dir: Path | None = None,
                  progress=None) -> tuple[np.ndarray, list, float]:
    """Loudness envelope + static-screen intervals (cached: both are slow-ish on long videos)."""
    progress = progress or (lambda f, m: None)
    cache = cache_dir / "media.npz" if cache_dir else None
    if cache and cache.exists():
        z = np.load(cache)
        if float(z["t0"]) == t0:
            return z["env"], [tuple(x) for x in z["frozen"]], float(z["t1"])
    progress(None, "анализ громкости")
    audio = load_audio(src)
    env = rms_envelope(audio)
    if t1 is None:
        t1 = len(audio) / SR
    progress(None, "анализ экрана")
    frozen = frozen_intervals(src, t0, t1 - t0)
    if cache:
        np.savez(cache, env=env, frozen=np.array(frozen, dtype=float).reshape(-1, 2), t0=t0, t1=t1)
    return env, frozen, t1


def make_snapper(env: np.ndarray, words: list[Word]):
    """Returns snap(t): move a cut at a word boundary into the real silence next to it."""
    thr_floor = silence_threshold(env)
    w_starts = np.array([w.start for w in words])

    def word_bounds(t: float) -> tuple[float, float]:
        """Search window for a cut at word start t: from mid previous word to mid this word."""
        i = int(np.argmin(np.abs(w_starts - t)))
        lo = (words[i - 1].start + words[i - 1].end) / 2 if i > 0 else t - 0.6
        hi = (words[i].start + words[i].end) / 2
        if abs(words[i].start - t) > 0.05:  # not a word start (e.g. end of file)
            lo, hi = t - 0.3, t + 0.3
        return max(lo, t - 0.6), max(min(hi, t + 0.7), t + 0.02)

    def snap(t: float) -> float:
        # Whisper word boundaries drift; move cuts into the real gap between words,
        # but never past the middle of the neighbouring words (that would clip them)
        return snap_to_gap(env, t, thr_floor, *word_bounds(t))

    return snap


def detect(tr: dict, opts: Options | None = None, cache_dir: Path | None = None, progress=None) -> dict:
    """Transcript -> edits document (suggested cuts with defaults)."""
    opts = opts or Options()
    progress = progress or (lambda f, m: None)
    src = Path(tr["source"])
    t0 = tr["offset"]
    t1 = t0 + tr["duration"] if tr.get("duration") else None

    words = load_words(tr)
    phrases = split_phrases(words, opts.phrase_gap)
    env, frozen, t1 = analyze_media(src, t0, t1, cache_dir, progress)

    progress(None, "поиск дублей и пауз")
    edits = (
        detect_retakes(phrases, opts.retake_window)
        + detect_stutters(words)
        + detect_fillers(words)
        + detect_pauses(words, env, opts.min_pause, opts.keep_pause, frozen, t0, t1)
    ) if words else []  # no speech recognised (music, a silent clip): nothing to suggest

    snap = make_snapper(env, words)
    for e in edits:
        if e["kind"] != "pause":  # pauses are already placed inside measured silence
            e["start"], e["end"] = snap(e["start"]), snap(e["end"])

    # defaults: what is pre-selected for cutting
    for e in edits:
        k = e["kind"]
        if k == "pause":
            e["on"] = True  # long silences are almost always prep work (scrolling, erasing notes)
        elif k == "filler":
            e["on"] = True
        elif k == "stutter":
            e["on"] = False
        e["rule_on"] = e["on"]
    edits = [e for e in edits if e["end"] - e["start"] > 0.08]
    edits.sort(key=lambda e: e["start"])
    for n, e in enumerate(edits):
        e["id"] = n
        e["start"], e["end"] = round(e["start"], 3), round(e["end"], 3)

    return {
        "source": str(src),
        "range": [t0, t1],
        "options": opts.__dict__,
        "words": [{"s": round(w.start, 3), "e": round(w.end, 3), "t": w.text} for w in words],
        "phrases": phrase_index(phrases),
        "edits": edits,
    }


def summary(doc: dict) -> str:
    t0, t1 = doc["range"]
    by, lines = {}, []
    for e in doc["edits"]:
        by.setdefault(e["kind"], []).append(e)
    for k, v in by.items():
        on = [e for e in v if e["on"]]
        lines.append(f"{k:8s} {len(v):4d} found, {len(on):4d} pre-selected, "
                     f"{sum(e['end'] - e['start'] for e in on):6.1f}s")
    total = sum(e["end"] - e["start"] for e in doc["edits"] if e["on"])
    lines.append(f"range {t1 - t0:.0f}s, would cut {total:.0f}s -> {t1 - t0 - total:.0f}s")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("transcript", type=Path)
    ap.add_argument("-o", "--out", type=Path, required=True)
    ap.add_argument("--phrase-gap", type=float, default=0.5)
    ap.add_argument("--retake-window", type=float, default=25.0)
    ap.add_argument("--min-pause", type=float, default=1.2)
    ap.add_argument("--keep-pause", type=float, default=0.8)
    args = ap.parse_args()

    tr = json.loads(args.transcript.read_text())
    opts = Options(args.phrase_gap, args.retake_window, args.min_pause, args.keep_pause)
    doc = detect(tr, opts, progress=lambda f, m: print(m + "…", file=sys.stderr))
    args.out.write_text(json.dumps(doc, ensure_ascii=False, indent=1))
    print(summary(doc))


if __name__ == "__main__":
    main()

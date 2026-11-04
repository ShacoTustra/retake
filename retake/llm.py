"""Optional LLM pass: a second opinion on suggested cuts + bad takes the rules missed.

Works with any OpenAI-compatible Chat Completions endpoint (proxyapi.ru, OpenAI, OpenRouter,
Ollama, LM Studio). The model only decides *what* to cut, by phrase number; exact cut points
are still placed into measured silence by detect.py, never taken from the model.

  llm.py edits.json -o edits_llm.json [--model anthropic/claude-haiku-5-5] [--base-url ...]
  llm.py edits.json --dry-run          # print the prompt and its size, no request
"""
import argparse
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from rapidfuzz import fuzz

sys.path.insert(0, str(Path(__file__).parent))
import config  # noqa: E402
from detect import Word, load_audio, make_snapper, norm, rms_envelope  # noqa: E402
import numpy as np  # noqa: E402

LLM_ID_BASE = 100000  # ids of cuts found by the model (rule-based ids are small)
# stutters ("знаем, знаем", "наника-наника") are nearly always intentional and off by default:
# asking about each of them costs tokens and buys nothing
JUDGED_KINDS = ("retake", "filler")


@dataclass
class LLMConfig:
    base_url: str
    model: str
    api_key: str = ""
    temperature: float | None = 0.0
    timeout: float = 300
    max_chars: int = 40000
    max_tokens: int = 16000
    reasoning_effort: str = ""  # "", "low", "medium", "high": for models that think before answering

    @classmethod
    def from_settings(cls, s: dict | None = None, **over) -> "LLMConfig":
        llm = (s or config.load())["llm"]
        kw = {k: llm[k] for k in ("base_url", "model", "api_key", "temperature", "timeout", "max_chars", "max_tokens",
                                       "reasoning_effort")
              if k in llm}
        kw.update({k: v for k, v in over.items() if v is not None})
        return cls(**kw)


class LLMError(RuntimeError):
    pass


# ---------------------------------------------------------------- HTTP client

def _ssl_context():
    try:  # use the OS trust store (corporate proxies, macOS keychain) when available
        import truststore
        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except Exception:  # noqa: BLE001
        return ssl.create_default_context()


def _request(cfg: LLMConfig, method: str, path: str, body: dict | None = None) -> dict:
    url = cfg.base_url.rstrip("/") + path
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if cfg.api_key:
        headers["Authorization"] = f"Bearer {cfg.api_key}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    handlers = [urllib.request.HTTPSHandler(context=_ssl_context())]
    if host in ("localhost", "127.0.0.1", "::1"):  # Ollama / LM Studio: never through an HTTP proxy
        handlers.append(urllib.request.ProxyHandler({}))
    try:
        with urllib.request.build_opener(*handlers).open(req, timeout=cfg.timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        text = e.read().decode(errors="replace")[:600]
        raise LLMError(f"HTTP {e.code}: {text}") from None
    except urllib.error.URLError as e:
        raise LLMError(f"нет соединения с {cfg.base_url}: {e.reason}") from None
    except TimeoutError:
        raise LLMError(f"нет ответа за {cfg.timeout:.0f} с") from None


def list_models(cfg: LLMConfig) -> list[str]:
    r = _request(cfg, "GET", "/models")
    return sorted(m["id"] for m in r.get("data", []) if "id" in m)


def chat(cfg: LLMConfig, messages: list[dict], json_mode: bool = True) -> tuple[str, dict]:
    """One chat completion. Drops optional parameters the server rejects and retries transient errors."""
    if not cfg.base_url or not cfg.model:
        raise LLMError("не указаны адрес API или модель")
    body = {"model": cfg.model, "messages": messages, "max_tokens": cfg.max_tokens}
    if cfg.temperature is not None:
        body["temperature"] = cfg.temperature
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    if cfg.reasoning_effort:
        body["reasoning_effort"] = cfg.reasoning_effort
    attempt = 0
    while True:
        attempt += 1
        try:
            r = _request(cfg, "POST", "/chat/completions", body)
            break
        except LLMError as e:
            msg = str(e)
            low = msg.lower()
            # some models/servers reject optional parameters: drop them and try again
            if msg.startswith("HTTP 400") and "response_format" in body and (
                    "response_format" in low or "json" in low):
                body.pop("response_format")
                continue
            if msg.startswith("HTTP 400") and "temperature" in body and "temperature" in low:
                body.pop("temperature")
                continue
            if msg.startswith("HTTP 400") and "reasoning_effort" in body and "reasoning" in low:
                body.pop("reasoning_effort")
                continue
            if msg.startswith("HTTP 400") and "max_tokens" in body and "max_completion_tokens" in low:
                body["max_completion_tokens"] = body.pop("max_tokens")  # newer OpenAI models
                continue
            transient = msg.startswith(("HTTP 429", "HTTP 5")) or "соединения" in msg or "нет ответа" in msg
            if transient and attempt < 3:
                time.sleep(3 * attempt)
                continue
            raise
    try:
        choice = r["choices"][0]
        text = choice["message"].get("content") or ""
    except (KeyError, IndexError, TypeError):
        raise LLMError(f"неожиданный ответ: {json.dumps(r)[:400]}") from None
    usage = r.get("usage") or {}
    if os.environ.get("RETAKE_LLM_DEBUG"):
        Path(os.environ["RETAKE_LLM_DEBUG"]).write_text(json.dumps(r, ensure_ascii=False, indent=1))
    if choice.get("finish_reason") == "length":
        thought = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")
        hint = ("модель потратила лимит на размышления — уменьшите reasoning_effort"
                if thought and not text.strip() else "увеличьте max_tokens")
        raise LLMError(f"ответ модели обрезан на {usage.get('completion_tokens', '?')} токенах; {hint}")
    return text, usage


def parse_json(text: str) -> dict:
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    try:
        return json.loads(t)
    except ValueError:
        a, b = t.find("{"), t.rfind("}")
        if a >= 0 and b > a:
            return json.loads(t[a:b + 1])
        raise LLMError(f"модель ответила не JSON: {text[:300]}") from None


# ---------------------------------------------------------------- prompt

SYSTEM = """Ты помогаешь монтировать видеоурок японского языка. Преподаватель записывает урок одним куском: на экране учебник, она объясняет по-русски и читает японские примеры. Когда она ошибается, она говорит фразу заново — неудачную попытку нужно вырезать. Тебе дают автоматическую расшифровку и места, которые уже нашёл простой алгоритм.

Расшифровка:
- [N] — номер фразы, затем время мм:сс; (пауза N с) — тишина между фразами;
- японские слова записаны кириллицей, часто заглавными (ТАБЕМАС = 食べます); распознавание местами ошибается и может исказить слово («блинка» вместо «блин»).

ВЫРЕЗАТЬ (cut=true):
- неудачный дубль: фраза оборвана или сказана с ошибкой и затем сказана заново — вырезаем первую попытку, оставляем последнюю;
- оговорка с поправкой: «в прошедшем… ой, в настоящем времени» — вырезаем ошибочный кусок вместе с маркером;
- маркеры ошибки: «ой», «блин», «тьфу», «чёрт», «стоп», «сбилась», «язык прикусила», отдельное «ещё раз», «так, заново». Вырезается маркер И неудачная попытка перед ним: from — начало неудачной попытки, а не сам маркер;
- служебная речь не для зрителя: про запись и технику («запись идёт?», «не знаю, слышно ли будет»), разговор с питомцем или с кем-то рядом, не относящийся к уроку. Вырезай такой эпизод целиком, от первой до последней его фразы.

НЕ вырезать (cut=false) — это нормальная часть урока:
- намеренные повторы: японский пример прочитан дважды (медленно, потом в темпе), «повторяйте за мной», повтор для запоминания;
- несколько вариантов перевода подряд («мне нравится блюдо, мне нравится еда»);
- объяснение разных вещей с похожим началом («показатель тематического… показатель рематического падежа»);
- «ещё раз» внутри объяснения («прочитаем ещё раз», «ещё раз обратите внимание»);
- «извините», «простите» как перевод японского (すみません); звуки в кавычках («м», «э») — о них идёт речь;
- рассуждение вслух при решении задания («думаю, подходит вариант три, потому что…»), если оно не оборвано и не повторено.

confidence: "high" — ошибка или служебная речь очевидна (есть маркер, явный повтор, явный разговор не по уроку); "medium" — вероятно; "low" — сомневаешься. Потерянный кусок объяснения не вернуть, поэтому при сомнении — "low".

Задачи:
1. verdicts: для КАЖДОГО места из списка (#id) реши, есть ли там что вырезать (cut=true) или это нормальная речь (cut=false). Неточные границы — не повод ставить false: их поправит человек.
2. new: найди неудачные дубли, оговорки и служебную речь, которых нет в списке. Если неудачный кусок шире места из списка, добавь его целиком — пересечение не страшно. Вырезается всё от начала фразы from до конца фразы to включительно; всё после to остаётся. Если резать надо не с начала фразы from — укажи from_text: точные первые 2–4 слова вырезаемого куска внутри этой фразы. Если резать надо не до конца фразы to — укажи to_text: точные последние 2–4 слова вырезаемого куска.

Причина (reason) — по-русски, до 15 слов, например: «сбилась на числе, повторила сразу», «разговор с кошкой, не по уроку».

Ответ — только JSON такого вида:
{"verdicts": [{"id": 5, "cut": true, "confidence": "high", "reason": "..."}],
 "new": [{"from": 120, "to": 121, "from_text": null, "to_text": null, "kind": "retake", "confidence": "medium", "reason": "..."}]}
kind: "retake" — дубль или оговорка, "aside" — служебная речь."""

KIND_RU = {"retake": "дубль", "stutter": "повтор слова", "filler": "мычание"}


def mmss(t: float) -> str:
    t = round(t, 1)
    return f"{int(t // 60):02d}:{t % 60:04.1f}"


def phrase_texts(doc: dict) -> list[str]:
    W = doc["words"]
    return ["".join(W[i]["t"] for i in range(p["w0"], p["w1"])).strip() for p in doc["phrases"]]


def phrase_at(doc: dict, t: float) -> int:
    """Index of the phrase that contains (or follows) time t."""
    ph = doc["phrases"]
    for i, p in enumerate(ph):
        if t < p["end"] - 0.01:
            return i
    return len(ph) - 1


def transcript_lines(doc: dict, a: int, b: int) -> list[str]:
    ph, texts, out = doc["phrases"], phrase_texts(doc), []
    for i in range(a, b):
        if i > a:
            gap = ph[i]["start"] - ph[i - 1]["end"]
            if gap >= 1.5:
                out.append(f"(пауза {gap:.0f} с)")
        out.append(f"[{i}] {mmss(ph[i]['start'])} {texts[i]}")
    return out


def candidate_lines(doc: dict, edits: list[dict]) -> list[str]:
    out = []
    for e in edits:
        a, b = phrase_at(doc, e["start"]), phrase_at(doc, e["end"] - 0.05)
        removed = (e.get("removed") or "").replace("\n", " ")
        if len(removed) > 220:
            removed = removed[:220] + "…"
        why = e.get("why") or KIND_RU.get(e["kind"], e["kind"])
        out.append(f"#{e['id']} фразы {a}–{b}, {KIND_RU.get(e['kind'], e['kind'])}: {why}; "
                   f"алгоритм предлагает {'вырезать' if e['rule_on'] else 'оставить'}; "
                   f"вырезается: «{removed}»")
    return out


def chunks(doc: dict, max_chars: int, overlap: int = 15) -> list[tuple[int, int]]:
    """Split phrases into windows of about max_chars characters (with some overlap for context)."""
    texts = phrase_texts(doc)
    out, a, n = [], 0, len(texts)
    while a < n:
        size, b = 0, a
        while b < n and (size + len(texts[b]) + 14 <= max_chars or b == a):
            size += len(texts[b]) + 14
            b += 1
        out.append((a, b))
        if b >= n:
            break
        a = max(b - overlap, a + 1)
    return out


def build_messages(doc: dict, a: int, b: int, cands: list[dict]) -> list[dict]:
    user = ("РАСШИФРОВКА:\n" + "\n".join(transcript_lines(doc, a, b))
            + "\n\nМЕСТА, НАЙДЕННЫЕ АЛГОРИТМОМ:\n" + ("\n".join(candidate_lines(doc, cands)) or "(нет)"))
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]


# ---------------------------------------------------------------- applying the answer

def locate(doc: dict, pi: int, quote: str | None, last: bool) -> int:
    """Word index inside phrase pi where a quoted fragment starts (or ends, if last)."""
    p = doc["phrases"][pi]
    idx = list(range(p["w0"], p["w1"]))
    if not quote:
        return idx[-1] if last else idx[0]
    q = norm(quote).split()
    toks = [norm(doc["words"][i]["t"]) for i in idx]
    if not q:
        return idx[-1] if last else idx[0]
    best, best_i = -1.0, None
    k = len(q)
    for s in range(0, max(1, len(toks) - k + 1)):
        sc = fuzz.ratio(" ".join(q), " ".join(toks[s:s + k]))
        if sc > best:
            best, best_i = sc, s
    if best < 70 or best_i is None:
        return idx[-1] if last else idx[0]
    return idx[min(len(idx) - 1, best_i + k - 1)] if last else idx[best_i]


def reset_llm(doc: dict) -> None:
    """Undo a previous LLM pass so a re-run starts from the rule-based suggestions."""
    doc["edits"] = [e for e in doc["edits"] if e.get("src") != "llm"]
    for e in doc["edits"]:
        e.pop("llm", None)
        e.pop("disputed", None)
        if "rule_on" in e:
            e["on"] = e["rule_on"]
    doc.pop("llm", None)


def apply_answer(doc: dict, ans: dict, snap, cand_ids: set[int], next_id: int,
                 auto_apply: str = "high") -> tuple[int, int]:
    """Merge the model's answer into doc. New cuts are pre-selected only from `auto_apply` confidence up."""
    accept = ("high",) if auto_apply == "high" else ("high", "medium")
    by_id = {e["id"]: e for e in doc["edits"]}
    nv = 0
    for v in ans.get("verdicts") or []:
        try:
            e = by_id[int(v["id"])]
        except (KeyError, ValueError, TypeError):
            continue
        if e["id"] not in cand_ids or "llm" in e:
            continue  # overlapping chunks: the first verdict wins
        cut = bool(v.get("cut"))
        conf = v.get("confidence") if v.get("confidence") in ("high", "medium", "low") else "low"
        e["llm"] = {"cut": cut, "confidence": conf, "reason": str(v.get("reason") or "")[:200]}
        if conf in ("high", "medium"):
            e["on"] = cut
        e["disputed"] = cut != e["rule_on"]
        nv += 1

    W, ph = doc["words"], doc["phrases"]
    nn = 0
    for c in ans.get("new") or []:
        try:
            a, b = int(c["from"]), int(c["to"])
        except (KeyError, ValueError, TypeError):
            continue
        if not (0 <= a <= b < len(ph)) or b - a > 12:
            continue  # out of range or implausibly long
        wa = locate(doc, a, c.get("from_text"), last=False)
        wb = locate(doc, b, c.get("to_text"), last=True)
        if wb < wa:
            continue
        start = W[wa]["s"]
        end = W[wb + 1]["s"] if wb + 1 < len(W) else W[wb]["e"] + 0.3
        start, end = snap(start), snap(end)
        if end - start < 0.15:
            continue
        # same place as an existing suggestion (rule-based or from an overlapping chunk)?
        conf = c.get("confidence") if c.get("confidence") in ("high", "medium", "low") else "low"
        kind = "aside" if c.get("kind") == "aside" else "retake"
        reason = str(c.get("reason") or "")[:200]
        # the same place as an existing suggestion: models sometimes answer "keep #73" and then
        # propose #73's span again as new -- read that as "cut #73". A bigger cut that swallows
        # a small suggestion is not a duplicate (render merges overlapping cuts).
        same = None
        for e in doc["edits"]:
            if e["kind"] == "pause":
                continue
            ov = min(e["end"], end) - max(e["start"], start)
            if ov > 0.6 * max(e["end"] - e["start"], end - start):
                same = e
                break
        if same is not None:
            if same["id"] in cand_ids:
                same["llm"] = {"cut": True, "confidence": conf, "reason": reason}
                if conf in ("high", "medium"):
                    same["on"] = True
                same["disputed"] = same["rule_on"] is False
            continue
        doc["edits"].append({
            "id": next_id, "kind": kind, "src": "llm",
            "start": round(start, 3), "end": round(end, 3),
            "on": conf in accept, "rule_on": None,
            "why": reason, "removed": "".join(w["t"] for w in W[wa:wb + 1]).strip(),
            "llm": {"cut": True, "confidence": conf, "reason": reason},
        })
        next_id += 1
        nn += 1
    return nv, nn


def review(doc: dict, cfg: LLMConfig, cache_dir: Path | None = None, progress=None,
           auto_apply: str = "high") -> dict:
    """Run the LLM pass on an edits document (in place) and return it."""
    progress = progress or (lambda f, m: None)
    reset_llm(doc)
    words = [Word(w["s"], w["e"], w["t"], 1.0) for w in doc["words"]]
    cache = cache_dir / "media.npz" if cache_dir else None
    if cache and cache.exists():
        env = np.load(cache)["env"]
    else:
        progress(None, "анализ громкости")
        env = rms_envelope(load_audio(Path(doc["source"])))
    snap = make_snapper(env, words)

    judged = [e for e in doc["edits"] if e["kind"] in JUDGED_KINDS]
    parts = chunks(doc, cfg.max_chars)
    usage_total: dict = {}
    next_id = LLM_ID_BASE
    n_v = n_n = 0
    t_start = time.time()
    for k, (a, b) in enumerate(parts):
        progress(k / len(parts), f"LLM: часть {k + 1} из {len(parts)}" if len(parts) > 1 else "LLM думает")
        lo, hi = doc["phrases"][a]["start"], doc["phrases"][b - 1]["end"]
        cands = [e for e in judged if lo - 0.01 <= e["start"] <= hi and "llm" not in e]
        text, usage = chat(cfg, build_messages(doc, a, b, cands))
        for key, val in usage.items():
            if isinstance(val, (int, float)):
                usage_total[key] = usage_total.get(key, 0) + val
        ans = parse_json(text)
        v, n = apply_answer(doc, ans, snap, {e["id"] for e in cands}, next_id, auto_apply)
        next_id += n
        n_v += v
        n_n += n
    doc["edits"].sort(key=lambda e: e["start"])
    doc["llm"] = {
        "model": cfg.model, "base_url": cfg.base_url, "at": time.strftime("%Y-%m-%d %H:%M"),
        "elapsed_sec": round(time.time() - t_start, 1), "parts": len(parts), "usage": usage_total,
        "judged": len(judged), "verdicts": n_v, "new": n_n,
    }
    progress(1.0, "LLM готово")
    return doc


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("edits", type=Path, help="output of detect.py")
    ap.add_argument("-o", "--out", type=Path)
    ap.add_argument("--base-url")
    ap.add_argument("--model")
    ap.add_argument("--api-key")
    ap.add_argument("--max-chars", type=int)
    ap.add_argument("--reasoning-effort", choices=["", "low", "medium", "high"])
    ap.add_argument("--dry-run", action="store_true", help="print the prompt, do not call the API")
    args = ap.parse_args()

    doc = json.loads(args.edits.read_text())
    cfg = LLMConfig.from_settings(base_url=args.base_url, model=args.model, api_key=args.api_key,
                                  max_chars=args.max_chars, reasoning_effort=args.reasoning_effort)
    if args.dry_run:
        reset_llm(doc)
        for a, b in chunks(doc, cfg.max_chars):
            lo, hi = doc["phrases"][a]["start"], doc["phrases"][b - 1]["end"]
            cands = [e for e in doc["edits"] if e["kind"] in JUDGED_KINDS and lo - 0.01 <= e["start"] <= hi]
            msgs = build_messages(doc, a, b, cands)
            print(msgs[1]["content"])
            n = sum(len(m["content"]) for m in msgs)
            print(f"\n--- phrases {a}..{b - 1}, {len(cands)} candidates, {n} chars", file=sys.stderr)
        return
    if not args.out:
        ap.error("-o is required (or --dry-run)")
    review(doc, cfg, progress=lambda f, m: print(m, file=sys.stderr))
    args.out.write_text(json.dumps(doc, ensure_ascii=False, indent=1))
    info = doc["llm"]
    print(f"{info['model']}: {info['verdicts']}/{info['judged']} verdicts, {info['new']} new, "
          f"{info['elapsed_sec']} s, usage {info['usage']}", file=sys.stderr)
    for e in doc["edits"]:
        if "llm" in e:
            L = e["llm"]
            flag = "NEW " if e.get("src") == "llm" else ("DIFF" if e.get("disputed") else "    ")
            print(f"{flag} #{e['id']:<6} {e['kind']:7s} {mmss(e['start'])}–{mmss(e['end'])} "
                  f"cut={L['cut']!s:5} {L['confidence']:6s} on={e['on']!s:5} {L['reason']} | {e['removed'][:70]}")


if __name__ == "__main__":
    main()

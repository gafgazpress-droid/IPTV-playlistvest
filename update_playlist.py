#!/usr/bin/env python3
"""
M3U yeniləyici: ölü linkləri tapır, mənbə m3u-larından eyni adlı kanalın işlək linki ilə əvəz edir.
Yalnız standart Python kitabxanası lazımdır (Python 3.9+).

İstifadə:
  python update_playlist.py --input base.m3u --sources sources.txt --output playlist.m3u
"""
import argparse
import json
import re
import sys
import unicodedata
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urljoin

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
URL_RE = re.compile(r"^(https?|rtmp|rtsp|udp)://", re.I)
QUALITY = {"hd", "sd", "fhd", "uhd", "4k", "1080p", "720p", "576p", "480p", "360p", "1080", "720"}
FOLD = str.maketrans({"ə": "e", "Ə": "e", "ı": "i", "İ": "i", "ö": "o", "Ö": "o",
                      "ü": "u", "Ü": "u", "ş": "s", "Ş": "s", "ç": "c", "Ç": "c",
                      "ğ": "g", "Ğ": "g"})


# ---------------------------------------------------------------- parsing
class Cand:
    def __init__(self, url, opts):
        self.url = url
        self.opts = opts  # #EXTVLCOPT və s.

    @property
    def ua(self):
        for o in self.opts:
            if o.lower().startswith("#extvlcopt:http-user-agent="):
                return o.split("=", 1)[1]
        return UA


class Entry:
    def __init__(self, extinf):
        self.extinf = extinf
        self.group_lines = []
        self.cands = []

    @property
    def name(self):
        return re.sub(r'"[^"]*"', "", self.extinf).split(",", 1)[-1].strip()

    @property
    def key(self):
        return self.cands[0].url


def parse(text):
    entries, cur, pending = [], None, []
    for raw in text.replace("\r", "").split("\n"):
        line = raw.strip()
        if not line or line.startswith("#EXTM3U"):
            continue
        if line.startswith("#EXTINF"):
            cur = Entry(line)
            entries.append(cur)
            pending = []
        elif line.startswith("#EXTGRP"):
            if cur:
                cur.group_lines.append(line)
        elif line.startswith("#"):
            pending.append(line)
        elif URL_RE.match(line) and cur is not None:
            cur.cands.append(Cand(line, pending))
            pending = []
    return [e for e in entries if e.cands]


def norm(name):
    s = unicodedata.normalize("NFKD", name.translate(FOLD)).lower()
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[\(\[].*?[\)\]]", " ", s)
    tokens = [t for t in re.split(r"[\W_]+", s) if t and t not in QUALITY]
    return "".join(tokens)


# ---------------------------------------------------------------- checking
def _get(url, ua, timeout, nbytes):
    req = urllib.request.Request(url, headers={"User-Agent": ua})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.geturl(), r.read(nbytes)


def is_alive(url, ua=UA, timeout=8):
    """HTTP(S) axınını yoxlayır: playlist -> media playlist -> ilk seqment."""
    if not url.lower().startswith(("http://", "https://")):
        return True  # rtmp/udp yoxlanmır, toxunmuruq
    try:
        status, final, body = _get(url, ua, timeout, 300_000)
        if status != 200:
            return False
        txt = body.decode("utf-8", "ignore")
        head = txt.lstrip()[:200].lower()
        if head.startswith(("<!doctype", "<html")):
            return False
        if not txt.lstrip().startswith("#EXTM3U"):
            return len(body) > 0  # birbaşa .ts / .mp4 axını

        for _ in range(3):
            nxt = next((l.strip() for l in txt.split("\n")
                        if l.strip() and not l.startswith("#")), None)
            if not nxt:
                return False
            nxt = urljoin(final, nxt)
            if ".m3u8" in nxt.lower():
                status, final, body = _get(nxt, ua, timeout, 300_000)
                txt = body.decode("utf-8", "ignore")
                if status != 200 or not txt.lstrip().startswith("#EXTM3U"):
                    return False
            else:
                status, _, seg = _get(nxt, ua, timeout, 2048)
                return status in (200, 206) and len(seg) > 0
        return False
    except Exception:
        return False


def fetch_text(url):
    try:
        return _get(url, UA, 20, 20_000_000)[2].decode("utf-8", "ignore")
    except Exception as e:
        print(f"  ! mənbə oxunmadı: {url} ({e})", file=sys.stderr)
        return ""


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True, help="əsas m3u (fayl və ya URL)")
    ap.add_argument("--sources", default="sources.txt")
    ap.add_argument("--aliases", default="aliases.json",
                    help='{"kanal adı": "mənbədəki ad"} (ixtiyari)')
    ap.add_argument("--output", default="playlist.m3u")
    ap.add_argument("--report", default="report.md")
    ap.add_argument("--locked", default="locked.txt",
                    help="toxunulmayacaq kanallar/qruplar/hostlar (ixtiyari)")
    ap.add_argument("--overrides", default="overrides.txt",
                    help='əl ilə verilən linklər: "Kanal adı | https://link" (ixtiyari)')
    ap.add_argument("--min-name-len", type=int, default=6,
                    help="bundan qısa adlı kanallar (ATV, MTV, CBC...) yalnız aliases.json-da "
                         "varsa avtomatik əvəz edilir; səbəb: başqa ölkənin eyniadlı kanalı ilə qarışır")
    ap.add_argument("--state", default="state.json")
    ap.add_argument("--min-dead-days", type=int, default=3,
                    help="link ardıcıl neçə dəfə ölü çıxsa əvəz edilsin")
    ap.add_argument("--threads", type=int, default=40)
    ap.add_argument("--timeout", type=int, default=8)
    args = ap.parse_args()

    base_text = (fetch_text(args.input) if args.input.startswith("http")
                 else Path(args.input).read_text(encoding="utf-8", errors="replace"))
    entries = parse(base_text)
    print(f"Əsas siyahı: {len(entries)} kanal")

    # mənbə indeksi
    aliases = {}
    if Path(args.aliases).exists():
        aliases = {norm(k): norm(v) for k, v in
                   json.loads(Path(args.aliases).read_text(encoding="utf-8")).items()}
    index = {}
    if Path(args.sources).exists():
        for line in Path(args.sources).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            src = parse(fetch_text(line))
            print(f"Mənbə: {line} -> {len(src)} kanal")
            for e in src:
                for c in e.cands:
                    lst = index.setdefault(norm(e.name), [])
                    if all(c.url != x.url for x in lst):
                        lst.append(c)

    # toxunulmaz siyahı: "Kanal adı", "group:QRUP", "host:domen.az"
    lk_names, lk_groups, lk_hosts = set(), set(), set()
    if Path(args.locked).exists():
        for line in Path(args.locked).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.lower().startswith("group:"):
                lk_groups.add(line[6:].strip().lower())
            elif line.lower().startswith("host:"):
                lk_hosts.add(line[5:].strip().lower())
            else:
                lk_names.add(norm(line))

    def host(u):
        return re.sub(r"^\w+://(?:[^@/]*@)?([^/:?#]+).*", r"\1", u).lower()

    def group(e):
        m = re.search(r'group-title="([^"]*)"', e.extinf)
        return (m.group(1) if m else "").strip().lower()

    def entry_locked(e):
        return norm(e.name) in lk_names or group(e) in lk_groups

    state = {}
    if Path(args.state).exists():
        state = json.loads(Path(args.state).read_text(encoding="utf-8"))

    overrides = {}
    if Path(args.overrides).exists():
        for line in Path(args.overrides).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "|" in line:
                nm, url = line.split("|", 1)
                overrides[norm(nm)] = url.strip()

    cache = {}
    for e in entries:  # kilidli host/kanal/qrup linkləri yoxlanmır, işləyir sayılır
        for c in e.cands:
            if entry_locked(e) or host(c.url) in lk_hosts:
                cache[c.url] = True

    def check_many(cands):
        todo = [c for c in cands if c.url not in cache]
        with ThreadPoolExecutor(args.threads) as ex:
            for c, ok in zip(todo, ex.map(lambda c: is_alive(c.url, c.ua, args.timeout), todo)):
                cache[c.url] = ok

    # 1) əsas siyahının bütün linklərini yoxla
    check_many([c for e in entries for c in e.cands])
    http_urls = [u for u in cache if u.lower().startswith("http")]
    dead_ratio = (sum(1 for u in http_urls if not cache[u]) / len(http_urls)) if http_urls else 0
    if dead_ratio > 0.7:
        print(f"XƏTA: linklərin {dead_ratio:.0%}-i ölü görünür. Şəbəkə problemi ola bilər, "
              "fayl dəyişdirilmədi.", file=sys.stderr)
        sys.exit(1)

    # 2) ölü kanallar üçün mənbələrdən əvəzedici tap
    need = []
    for e in entries:
        if not any(cache[c.url] for c in e.cands):
            n = norm(e.name)
            need += index.get(aliases.get(n, n), [])
    check_many(need)

    out, replaced, still_dead, waiting = ["#EXTM3U"], [], [], []
    skipped_short, manual = [], []
    new_state = {}
    for e in entries:
        alive = [c for c in e.cands if cache[c.url]]
        chosen = alive[0] if alive else None
        if norm(e.name) in overrides:  # əl ilə verilmiş link hər şeydən üstündür
            chosen = Cand(overrides[norm(e.name)], [])
            manual.append(e.name)
        if not chosen:
            fails = state.get(e.key, 0) + 1
            new_state[e.key] = fails
            if fails < args.min_dead_days:
                # hələ tez-tezdir: keçici xəta ola bilər, köhnə link qalır
                chosen = e.cands[0]
                waiting.append((e.name, fails))
            elif len(norm(e.name)) < args.min_name_len and norm(e.name) not in aliases:
                chosen = e.cands[0]
                skipped_short.append(e.name)
            else:
                n = norm(e.name)
                # qısa ümumi adlar (atv, mtv, cbc, tv8...) başqa ölkələrin kanalları ilə qarışır:
                # yalnız aliases.json-da açıq göstərilibsə əvəz edilir
                pool = [] if (len(n) <= 4 and n not in aliases) else index.get(aliases.get(n, n), [])
                for c in pool:
                    if cache.get(c.url):
                        chosen = c
                        replaced.append((e.name, e.cands[0].url, c.url))
                        break
                if not chosen:
                    chosen = e.cands[0]
                    still_dead.append(e.name)
        out.append(e.extinf)
        out += e.group_lines
        out += chosen.opts
        out.append(chosen.url)

    Path(args.output).write_text("\n".join(out) + "\n", encoding="utf-8")
    Path(args.state).write_text(json.dumps(new_state, ensure_ascii=False, indent=1), encoding="utf-8")

    rep = ["# Yenilənmə hesabatı\n",
           f"- Cəmi kanal: {len(entries)}",
           f"- Linki əvəz edilən: {len(replaced)}",
           f"- Ölü qalan (əvəzedici tapılmadı, köhnə link saxlanıldı): {len(still_dead)}",
           f"- Gözləyən (ölü çıxdı, amma hələ {args.min_dead_days} dəfə təsdiqlənməyib): {len(waiting)}\n"]
    if replaced:
        rep.append("## Əvəz edilənlər")
        rep += [f"- {n}: `{a}` -> `{b}`" for n, a, b in replaced]
    if still_dead:
        rep.append("\n## Hələ də ölü olanlar (mənbə axtarın)")
        rep += [f"- {n}" for n in still_dead]
    if skipped_short:
        rep.append("\n## Qısa adlı ölü kanallar (avtomatik əvəz edilmədi, overrides.txt-də link verin)")
        rep += [f"- {n}" for n in skipped_short]
    if waiting:
        rep.append("\n## Gözləyənlər (hələ əvəz edilməyib)")
        rep += [f"- {n}: {k}/{args.min_dead_days}" for n, k in waiting]
    Path(args.report).write_text("\n".join(rep) + "\n", encoding="utf-8")
    print("\n".join(rep[1:5]))


if __name__ == "__main__":
    main()
#!/usr/bin/env python3
"""
M3U playlist yeniləyici.

- playlist.m3u  : sizin mövcud playlist (kanal adları buradan götürülür)
- sources.txt   : daim yenilənən m3u mənbə linkləri (hər sətirdə bir URL)
- manual.txt    : (ixtiyari) toxunulmamalı kanal adları, hər sətirdə bir ad

Əl ilə əlavə etdiyiniz kanalı qorumağın 3 yolu:
  1) EXTINF sətrinə  manual="1"  əlavə edin
  2) Kanaldan əvvəlki sətrə  #MANUAL  yazın
  3) Kanal adını manual.txt faylına yazın
Bu kanallar sözbasöz olduğu kimi qalır.

Hər kanal üçün HD və SD variantı axtarılır, hər biri üçün mənbələrdən
sınaqdan keçirilib ən sürətli/stabil (donmayan) link seçilir.
"""
import argparse
import difflib
import re
import sys
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urljoin

import requests

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36"
SESSION = requests.Session()
SESSION.headers["User-Agent"] = UA

EXTINF_RE = re.compile(r"^#EXTINF:(\S+)\s*(.*),(.*)$")
QUALITY_RE = re.compile(
    r"\b(4k|uhd|fhd|full\s?hd|hd|sd|1080p?|720p?|576p?|480p?|360p?)\b", re.I
)
HD_TOKENS = {"4k", "uhd", "fhd", "fullhd", "full hd", "hd", "1080", "1080p", "720", "720p"}
SD_TOKENS = {"sd", "576", "576p", "480", "480p", "360", "360p"}
MAX_CANDIDATES = 8


# ---------------------------------------------------------------- parsing
def norm(name: str) -> str:
    name = QUALITY_RE.sub(" ", name)
    for a, b in (("ə", "e"), ("Ə", "e"), ("ı", "i"), ("İ", "i")):
        name = name.replace(a, b)
    name = unicodedata.normalize("NFKD", name)
    name = "".join(c for c in name if not unicodedata.combining(c)).lower()
    return re.sub(r"[^a-z0-9]", "", name)


def base_name(name: str) -> str:
    return re.sub(r"\s+", " ", QUALITY_RE.sub(" ", name)).strip()


def classify(name: str) -> str:
    m = QUALITY_RE.search(name)
    if not m:
        return "?"
    tok = re.sub(r"\s+", "", m.group(1).lower())
    if tok in HD_TOKENS or tok == "fullhd":
        return "HD"
    return "SD"


def parse_m3u(text: str):
    entries, cur, flag = [], None, False
    header = "#EXTM3U"
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXTM3U"):
            header = line
            continue
        if line.upper().startswith("#MANUAL"):
            flag = True
            continue
        if line.startswith("#EXTINF"):
            m = EXTINF_RE.match(line)
            attrs = m.group(2).strip() if m else ""
            name = m.group(3).strip() if m else line.split(",")[-1].strip()
            cur = {
                "inf": line, "attrs": attrs, "name": name, "extras": [],
                "manual_flag": flag, "manual": flag or 'manual="1"' in attrs,
            }
            flag = False
            continue
        if line.startswith("#"):
            if cur is not None:
                cur["extras"].append(line)
            continue
        if cur is not None:
            cur["url"] = line
            entries.append(cur)
            cur = None
    return entries, header


def headers_of(extras):
    h = {}
    for e in extras:
        low = e.lower()
        if "http-user-agent=" in low:
            h["User-Agent"] = e.split("=", 1)[1]
        elif "http-referrer=" in low or "http-referer=" in low:
            h["Referer"] = e.split("=", 1)[1]
    return h


# ---------------------------------------------------------------- probing
def read_more(it, limit, seconds):
    buf, t = b"", time.time()
    for chunk in it:
        buf += chunk
        if len(buf) >= limit or time.time() - t > seconds:
            break
    return buf


def probe_hls(text, base, headers, res, depth):
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if any(l.startswith("#EXT-X-STREAM-INF") for l in lines):
        variants = []
        for i, l in enumerate(lines):
            if not l.startswith("#EXT-X-STREAM-INF"):
                continue
            bw = re.search(r"BANDWIDTH=(\d+)", l)
            rs = re.search(r"RESOLUTION=\d+x(\d+)", l)
            uri = next((x for x in lines[i + 1:] if not x.startswith("#")), None)
            if uri:
                variants.append((int(rs.group(1)) if rs else 0,
                                 int(bw.group(1)) if bw else 0, urljoin(base, uri)))
        if not variants or depth > 0:
            return res
        res["variants"] = [(h, u) for h, _, u in variants if h]
        best = max(variants, key=lambda v: v[1])
        res["height"] = best[0] or None
        r = SESSION.get(best[2], headers=headers, timeout=(5, 8))
        if r.status_code != 200:
            return res
        return probe_hls(r.text, best[2], headers, res, depth + 1)

    segs, dur = [], None
    for l in lines:
        if l.startswith("#EXTINF:"):
            try:
                dur = float(l[8:].split(",")[0])
            except ValueError:
                dur = None
        elif not l.startswith("#"):
            segs.append((dur or 6.0, urljoin(base, l)))
            dur = None
    if not segs:
        return res
    ratios = []
    for d, u in segs[-2:]:
        t0 = time.time()
        with SESSION.get(u, headers=headers, stream=True, timeout=(5, 8)) as r:
            if r.status_code != 200:
                return res
            data = read_more(r.iter_content(65536), 4_000_000, 10)
        if len(data) < 20000 or data[:1] == b"<":
            return res
        ratios.append((time.time() - t0) / max(d, 1.0))
    worst, avg = max(ratios), sum(ratios) / len(ratios)
    if worst < 0.7:
        res["ok"] = True
        res["score"] = min(1.0 / max(avg, 0.05), 20.0)
    return res


def probe(url, headers):
    res = {"ok": False, "score": 0.0, "height": None, "variants": []}
    t0 = time.time()
    try:
        with SESSION.get(url, headers=headers, stream=True, timeout=(5, 8)) as resp:
            if resp.status_code != 200:
                return res
            if "text/html" in resp.headers.get("content-type", "").lower():
                return res
            it = resp.iter_content(8192)
            first = next(it, b"")
            if not first:
                return res
            if first.lstrip().startswith(b"#EXTM3U"):
                body = first + read_more(it, 1_000_000, 5)
                return probe_hls(body.decode("utf-8", "ignore"), resp.url, headers, res, 0)
            data, t1 = len(first), time.time()
            while time.time() - t1 < 3 and data < 2_000_000:
                chunk = next(it, b"")
                if not chunk:
                    break
                data += len(chunk)
            speed = data / 1024 / max(time.time() - t0, 0.3)  # KB/s
            if speed >= 250:
                res["ok"], res["score"] = True, min(speed / 250.0, 20.0)
    except Exception:
        pass
    return res


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--playlist", default="playlist.m3u")
    ap.add_argument("--sources", default="sources.txt")
    ap.add_argument("--manual", default="manual.txt")
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    pl_path = Path(a.playlist)
    entries, header = parse_m3u(pl_path.read_text(encoding="utf-8", errors="ignore"))

    manual_names = set()
    if Path(a.manual).exists():
        manual_names = {norm(l) for l in Path(a.manual).read_text(encoding="utf-8").splitlines()
                        if l.strip() and not l.startswith("#")}
    for e in entries:
        e["key"], e["q"] = norm(e["name"]), classify(e["name"])
        if e["key"] in manual_names:
            e["manual"] = True

    # mənbələri yüklə
    pool = {}
    for src in Path(a.sources).read_text(encoding="utf-8").splitlines():
        src = src.strip()
        if not src or src.startswith("#"):
            continue
        try:
            r = SESSION.get(src, timeout=(8, 30))
            r.raise_for_status()
            got, _ = parse_m3u(r.text)
        except Exception as ex:
            print(f"[XƏTA] mənbə oxunmadı: {src} ({ex})")
            continue
        for e in got:
            if "url" not in e:
                continue
            e["key"], e["q"] = norm(e["name"]), classify(e["name"])
            pool.setdefault(e["key"], []).append(e)
        print(f"[OK] {src}: {len(got)} kanal")

    # idarə olunan kanallar
    managed, manual_cover = {}, {}
    for e in entries:
        if e["manual"]:
            manual_cover.setdefault(e["key"], set()).add(e["q"])
        else:
            managed.setdefault(e["key"], []).append(e)

    def candidates(key):
        src_key = key if key in pool else None
        if not src_key:
            m = difflib.get_close_matches(key, list(pool), n=1, cutoff=0.88)
            src_key = m[0] if m else None
        out = list(pool.get(src_key, [])) if src_key else []
        out += managed.get(key, [])  # mövcud linklər də namizəddir (geriləmə olmasın)
        seen, uniq = set(), []
        for c in sorted(out, key=lambda c: c["q"] == "?"):
            if c["url"] not in seen:
                seen.add(c["url"])
                uniq.append(c)
        return uniq[:MAX_CANDIDATES]

    cand_map = {k: candidates(k) for k in managed}
    todo = {(c["url"], tuple(sorted(headers_of(c["extras"]).items())))
            for cs in cand_map.values() for c in cs}
    print(f"[INFO] {len(todo)} link yoxlanılır...")
    cache = {}
    with ThreadPoolExecutor(a.workers) as ex:
        futs = {t: ex.submit(probe, t[0], dict(t[1])) for t in todo}
        for t, f in futs.items():
            cache[t] = f.result()

    def result_of(c):
        return cache[(c["url"], tuple(sorted(headers_of(c["extras"]).items())))]

    resolved = {}
    for key, olds in managed.items():
        tpl_all = olds
        base = base_name(olds[0]["name"])
        out = []
        for q in ("HD", "SD"):
            if q in manual_cover.get(key, set()) or "?" in manual_cover.get(key, set()):
                continue
            scored = []
            for c in cand_map[key]:
                r = result_of(c)
                if not r["ok"]:
                    continue
                fq = c["q"] if c["q"] != "?" else ("HD" if (r["height"] or 0) >= 720 else "SD")
                if fq == q:
                    scored.append((r["score"], c))
            tpl = next((o for o in tpl_all if o["q"] == q), tpl_all[0])
            if scored:
                best = max(scored, key=lambda s: s[0])[1]
                out.append({"attrs": tpl["attrs"], "name": f"{base} {q}",
                            "extras": best["extras"], "url": best["url"]})
                continue
            # SD yoxdursa HD master-dan aşağı keyfiyyət çıxar
            if q == "SD":
                for c in cand_map[key]:
                    r = result_of(c)
                    lows = [(h, u) for h, u in r["variants"] if 0 < h <= 576]
                    if r["ok"] and lows:
                        h, u = max(lows)
                        if probe(u, headers_of(c["extras"]))["ok"]:
                            out.append({"attrs": tpl["attrs"], "name": f"{base} SD",
                                        "extras": c["extras"], "url": u})
                            break
                if out and out[-1]["name"].endswith("SD"):
                    continue
            old = next((o for o in tpl_all if o["q"] == q), None)
            if old:
                print(f"[XƏBƏRDARLIQ] {base} {q}: işləyən link tapılmadı, köhnəsi saxlanıldı")
                out.append(old)
            else:
                print(f"[XƏBƏRDARLIQ] {base} {q}: tapılmadı")
        resolved[key] = out

    # yazma
    lines, done = [header], set()
    for e in entries:
        if e["manual"]:
            if e["manual_flag"]:
                lines.append("#MANUAL")
            lines.append(e["inf"])
            lines += e["extras"]
            lines.append(e["url"])
        elif e["key"] not in done:
            done.add(e["key"])
            for n in resolved.get(e["key"], []):
                attrs = (n["attrs"] + " ") if n["attrs"] else ""
                lines.append(f'#EXTINF:-1 {attrs},{n["name"]}'.replace(" ,", ",", 1))
                lines += n["extras"]
                lines.append(n["url"])
    text = "\n".join(lines) + "\n"
    if a.dry_run:
        print(text[:3000])
        return
    pl_path.with_suffix(".m3u.bak").write_text(pl_path.read_text(encoding="utf-8", errors="ignore"),
                                               encoding="utf-8")
    pl_path.write_text(text, encoding="utf-8")
    print(f"[HAZIR] {pl_path} yeniləndi")


if __name__ == "__main__":
    sys.exit(main())

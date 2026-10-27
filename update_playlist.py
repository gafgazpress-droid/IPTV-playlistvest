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

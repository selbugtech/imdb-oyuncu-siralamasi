"""Sundance kazananlarını İngilizce Wikipedia'dan toplar.

Wikidata'daki Sundance kayıtları eksik olduğundan iki kaynak kullanılır:
  * "YYYY Sundance Film Festival" sayfalarının "Awards" bölümü (2003 ve sonrası)
  * kategori sayfaları ("Grand Jury Prize Dramatic" vb.), eski yıllar için
Filmler önce Wikipedia bağlantısı -> Wikidata -> IMDb kimliği ile, bağlantı yoksa
IMDb title.basics içinde ad + yıl eşleşmesiyle bulunur.
"""
import gzip
import json
import os
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

API = "https://en.wikipedia.org/w/api.php"
UA = "imdb-ranking-app/1.0 (awards export script)"
HERE = os.path.dirname(os.path.abspath(__file__))
TITLE_BASICS = os.path.join(HERE, "data", "raw", "title.basics.tsv.gz")

# Kategori sayfası -> kanonik kategori adı
CATEGORY_PAGES = {
    "Grand Jury Prize Dramatic": "Grand Jury Prize: U.S. Dramatic",
    "Grand Jury Prize Documentary": "Grand Jury Prize: U.S. Documentary",
    "Audience Award Dramatic": "Audience Award: U.S. Dramatic",
    "Audience Award Documentary": "Audience Award: U.S. Documentary",
    "Waldo Salt Screenwriting Award": "Screenwriting Award: U.S. Dramatic",
}
YEAR_PAGES = range(2003, 2027)


CACHE_DIR = os.path.join(HERE, "data", "wiki_cache")
_last = [0.0]


def api(params):
    params = {**params, "format": "json", "formatversion": "2", "maxlag": "5"}
    url = API + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    for attempt in range(6):
        wait = _last[0] + 1.0 - time.time()  # saniyede en fazla bir istek
        if wait > 0:
            time.sleep(wait)
        _last[0] = time.time()
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if attempt == 5 or e.code not in (429, 500, 502, 503, 504):
                raise
            time.sleep(int(e.headers.get("Retry-After") or 0) or 15 * (attempt + 1))
        except Exception:  # noqa: BLE001
            if attempt == 5:
                raise
            time.sleep(5 * (attempt + 1))


def wikitext(page):
    """Sayfa metnini getirir; tekrar çalıştırmalarda Wikipedia'yı yormamak için önbelleğe alır."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, re.sub(r"[^\w.-]+", "_", page) + ".json")
    if os.path.exists(path) and time.time() - os.path.getmtime(path) < 7 * 86400:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)["text"]
    d = api({"action": "parse", "page": page, "prop": "wikitext", "redirects": 1})
    text = None if "error" in d else d["parse"]["wikitext"]
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"text": text}, fh)
    return text


# ---------------------------------------------------------------- temizlik
def strip_markup(t):
    t = re.sub(r"<ref[^>]*/>", "", t)
    t = re.sub(r"<ref[^>]*>.*?</ref>", "", t, flags=re.S)
    t = re.sub(r"<!--.*?-->", "", t, flags=re.S)
    t = re.sub(r"<[^>]+>", "", t)
    # {{ill|Ad|...}} ve {{interlanguage link|Ad|...}} -> Ad ; diğer şablonlar silinir
    t = re.sub(r"\{\{\s*(?:ill|interlanguage link)\s*\|([^|}]*)[^}]*\}\}", r"\1", t, flags=re.I)
    for _ in range(3):
        t = re.sub(r"\{\{[^{}]*\}\}", "", t)
    return t


def unbold(t):
    """'''kalın''' işaretlerini kaldırır, '''''kalın italik''''' -> ''italik''."""
    return t.replace("'" * 5, "''").replace("'" * 3, "")


def link_text(t):
    """[[Hedef|Metin]] -> Metin"""
    return re.sub(r"\[\[(?:[^\]|]*\|)?([^\]]*)\]\]", r"\1", t)


def first_film(t):
    """Satırdaki ilk italik bölümden (film adı) görünen ad ve Wikipedia hedefini döndürür."""
    t = re.sub(r"\[\[([^\]|]*)\|''([^'\]]*)''\]\]", r"[[\1|\2]]", t)  # [[A|''B'']] -> [[A|B]]
    m = re.search(r"''(?!')(.+?)''", t)
    if not m:
        return None, None
    seg = m.group(1).strip("' ")
    lm = re.search(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]", seg)
    target = lm.group(1).strip() if lm else None
    name = link_text(seg).strip(" '\"")
    return (name or None), target


def people_from(t, film_start, film_end):
    """'... by Ad Soyad' ya da 'Ad Soyad for ''Film''' kalıplarından kişileri çıkarır."""
    before, after = t[:film_start], t[film_end:]
    m = re.search(r"\bby\s+(.+)$", after) or re.match(r"^\s*\(([^)]*[A-Za-z][^)]*)\)", after)
    src = m.group(1) if m else None
    if not src:
        m = re.search(r"[–—-]\s*(.+?)\s+for\s*$", before)
        src = m.group(1) if m else None
    if not src:
        return []
    src = re.sub(r"\(.*?\)", "", link_text(src))
    names = re.split(r",\s*|\s+and\s+|\s*&\s*", src)
    return [n.strip(" .'") for n in names if 2 < len(n.strip()) < 60 and not re.search(r"''|\d", n)]


# ---------------------------------------------------------------- kategori
def canon(text):
    """Farklı yazımları tek bir Sundance kategori adına indirger."""
    raw = re.sub(r"\s+", " ", link_text(text)).strip(" :–-")
    raw = re.sub(r"^(\d{4}|The)\s+", "", raw)                                 # "2015 Red Crown ..." -> "Red Crown ..."
    raw = re.sub(r"(:\s*[^:]*\bfor|\s+went to)$", "", raw).strip(" :–-")  # "NHK Award: Ad Soyad for"
    raw = re.sub(r"\s*/\s*", "/", raw)
    t = raw.lower().replace("u. s.", "u.s.").replace("’", "'")
    t = re.sub(r"\bus\b", "u.s.", t).replace("non-fiction", "nonfiction")
    # özel adlı ödüller
    if "festival favorite" in t:
        return "Festival Favorite Award" + (" (Runner Up)" if "runner" in t else "")
    if "best of next" in t:
        return "Audience Award: NEXT"
    if "honorable mention" in t and "short" in t:
        return "Honorable Mention: Short Film"
    if "nhk" in t:
        return "Sundance Institute/NHK Award"
    if "producer" in t and ("amazon" in t or "producers award" in t):
        return "Producers Award: " + ("Nonfiction" if "nonfiction" in t else "Fiction") if "fiction" in t else "Producers Award"
    if "shorts program" in t:  # "U.S. Fiction: Shorts Program Awards"
        t = "short film jury award: " + t.split(":")[0]
    if "short film jury award" in t:
        rest = re.sub(r"^.*short film jury awards?:?\s*", "", t).replace("u.s.", "U.S.")
        return "Short Film Jury Award: " + " ".join(w if w == "U.S." else w.capitalize() for w in rest.split()) if rest else "Short Film Jury Award"
    if "world cinema" in t or "international" in t or "world dramatic" in t or "world documentary" in t:
        scope = "World Cinema"
    elif re.search(r"\bnext\b", t):
        scope = "NEXT"
    elif "short" in t:
        scope = "Short Film"
    else:
        scope = "U.S."
    genre = "Dramatic" if re.search(r"drama", t) else "Documentary" if "documentar" in t else ""
    if "special jury" in t:
        kind = "Special Jury Award"
    elif "grand jury" in t or re.search(r"\bjury prize\b", t):
        kind = "Grand Jury Prize"
    elif "audience" in t:
        kind = "Audience Award"
    elif "direct" in t:
        kind = "Directing Award"
    elif "screenwrit" in t or "waldo salt" in t:
        kind = "Screenwriting Award"
        if scope == "U.S." and not genre:  # Waldo Salt ödülü U.S. Dramatic yarışmasına verilir
            genre = "Dramatic"
    elif "editing" in t:
        kind = "Editing Award"
    elif "cinematograph" in t:
        kind = "Cinematography Award"
    elif "sloan" in t:
        return "Alfred P. Sloan Prize"
    elif "innovator" in t:
        return "NEXT Innovator Award"
    elif "filmmaker trophy" in t:
        kind = "Filmmaker Trophy"
    else:
        return raw[0].upper() + raw[1:] if raw else ""
    if scope in ("NEXT", "Short Film") and not genre:
        return f"{kind}: {scope}"
    if scope == "Short Film":
        return f"{kind}: Short Film"
    return f"{kind}: {scope} {genre}".strip()


HEADING_KIND = [
    (r"grand jury", "Grand Jury Prize"),
    (r"audience", "Audience Award"),
    (r"^direct", "Directing Award"),
    (r"short", "Short Film"),
    (r"special jury", "Special Jury Award"),
    (r"next", "NEXT"),
]


def parse_year_page(year, text):
    m = re.search(r"^==\s*(Awards?|Award winners|Winners)\s*==\s*$", text, flags=re.M | re.I)
    if not m:
        return []
    body = text[m.end():]
    end = re.search(r"^==[^=].*==\s*$", body, flags=re.M)
    body = strip_markup(body[: end.start()] if end else body)
    heading = ""
    rows = []
    for line in body.splitlines():
        h = re.match(r"^===+\s*(.+?)\s*===+\s*$", line)
        if h:
            heading = link_text(h.group(1))
            continue
        prose = re.match(r"^The winner of the (.+?) was (''.*)$", line)
        if prose:  # 2019 gibi düz yazıyla anlatılan yıllar
            line = f"{prose.group(1)} – {prose.group(2)}".replace(" Award – ", " – ")
            line = re.sub(r"\),? directed by ", ") by ", line)
        elif not line.startswith("*"):
            continue
        line = unbold(line.lstrip("*: ").strip())
        film, target = first_film(line)
        if not film:
            continue
        fm = re.search(r"''(?!')(.+?)''", line)
        left = link_text(re.split(r"\s+[–—-]\s+|:\s*''", line[: fm.start()], maxsplit=1)[0])
        # "Directing Award: U.S. Dramatic – Ad for ''Film''" kalıbında kategori tirenin solunda
        cat_src = left if left.strip() else heading
        # Başlık yarışma bölümünü veriyorsa ("=== U.S. Dramatic Competition ===") satırdaki ödüle ekle
        scope_re = r"dramatic|documentar|world cinema|\bnext\b|short"
        if left.strip() and re.search(scope_re, heading, re.I) and not re.search(scope_re, left, re.I):
            cat_src = f"{left} {heading}"
        hk = next((k for p, k in HEADING_KIND if re.search(p, heading, re.I)), "")
        if hk and hk not in ("Short Film", "NEXT") and not re.search(r"prize|award|jury", cat_src, re.I):
            cat_src = f"{hk} {cat_src}"
        elif hk == "Short Film" and "short" not in cat_src.lower():
            cat_src = f"Short Film Jury Award: {cat_src}"
        cat = canon(cat_src)
        if not cat or len(cat) > 80:
            continue
        rows.append({"cat": cat, "film": film, "target": target, "year": year,
                     "people": people_from(line, fm.start(), fm.end())})
    return rows


def parse_category_page(cat, text):
    text = unbold(strip_markup(text))
    # Giriş paragrafındaki filmleri (ör. ödülün adını aldığı kişinin filmleri) atla
    start = min([i for i in (text.find("{|"), text.find("== Winners"), text.find("==Winners")) if i >= 0] or [0])
    text = text[start:]
    stop = re.search(r"^==\s*(See also|References|Notes)\s*==", text, flags=re.M | re.I)
    text = text[: stop.start()] if stop else text
    rows, year, cur = [], None, cat
    for chunk in re.split(r"\n\|-|\n(?=\*)|\n(?===)", text):
        h = re.match(r"^==\s*([^=].*?)\s*==\s*$", chunk.strip().splitlines()[0]) if chunk.strip() else None
        if h:  # "International winners" bölümü World Cinema kategorisidir
            cur = cat.replace("U.S.", "World Cinema") if re.search(r"international|world", h.group(1), re.I) else cat
            year = None
        # yıl hücresi bağlantı olabilir: [[2025 Sundance Film Festival|2025]]
        plain = re.sub(r"\[\[[^\]|]*\|([^\]]*)\]\]", r"\1", chunk)
        film_at = plain.find("''") if "''" in plain else 10**9
        ym = re.search(r"(?<![\d\[(])(19[789]\d|20[0-2]\d)(?![\d\])])", plain)
        if ym and ym.start() < film_at:
            year = int(ym.group(1))
        film, target = first_film(chunk)
        if film and year:
            rows.append({"cat": cur, "film": film, "target": target, "year": year, "people": []})
    return rows


# ---------------------------------------------------------------- IMDb eşleme
def norm(s):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    s = s.replace("&", "and")
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def imdb_from_targets(targets, sparql):
    """Wikipedia sayfa adları -> IMDb tt kimliği (Wikidata üzerinden)."""
    qids = {}
    targets = sorted(set(targets))
    for i in range(0, len(targets), 50):
        d = api({"action": "query", "titles": "|".join(targets[i : i + 50]), "prop": "pageprops",
                 "ppprop": "wikibase_item", "redirects": 1})
        q = d.get("query", {})
        alias = {}
        for r in q.get("normalized", []) + q.get("redirects", []):
            alias[r["to"]] = alias.get(r["from"], r["from"])
        for p in q.get("pages", []):
            qid = p.get("pageprops", {}).get("wikibase_item")
            if not qid:
                continue
            title = p["title"]
            origin = title
            while origin in alias:
                origin = alias[origin]
            qids[origin] = qid
            qids[title] = qid
    imdb = {}
    ids = sorted(set(qids.values()))
    for i in range(0, len(ids), 200):
        vals = " ".join(f"wd:{q}" for q in ids[i : i + 200])
        for b in sparql(f"SELECT ?f ?imdb WHERE {{ VALUES ?f {{ {vals} }} ?f wdt:P345 ?imdb FILTER(STRSTARTS(?imdb, \"tt\")) }}"):
            imdb[b["f"]["value"].rsplit("/", 1)[-1]] = b["imdb"]["value"]
    return {t: imdb.get(q) for t, q in qids.items()}


def imdb_by_title(wanted):
    """{norm(ad)} kümesi için title.basics'ten (ad, yıl) -> [(tconst, tür)] dizini kurar."""
    index = {}
    if not os.path.exists(TITLE_BASICS):
        return index
    ok_types = {"movie", "tvMovie", "short", "tvMiniSeries", "video", "tvSpecial"}
    with gzip.open(TITLE_BASICS, "rt", encoding="utf-8") as f:
        next(f)
        for line in f:
            r = line.split("\t", 6)
            if r[1] not in ok_types or r[5] == "\\N":
                continue
            for name in (r[2], r[3]):
                n = norm(name)
                if n in wanted:
                    index.setdefault((n, int(r[5])), []).append((r[0], r[1]))
    return index


def sundance_wins(sparql, log=print):
    rows = []
    for page, cat in CATEGORY_PAGES.items():
        t = wikitext(page)
        if t:
            got = parse_category_page(cat, t)
            rows += got
            log(f"  Wikipedia {page}: {len(got)}")
    for y in YEAR_PAGES:
        t = wikitext(f"{y} Sundance Film Festival")
        if t:
            got = parse_year_page(y, t)
            rows += got
            log(f"  Wikipedia {y} Sundance: {len(got)}")

    by_target = imdb_from_targets([r["target"] for r in rows if r["target"]], sparql)
    missing = {norm(r["film"]) for r in rows if not by_target.get(r["target"] or "")}
    index = imdb_by_title(missing)
    type_rank = {"movie": 0, "tvMovie": 1, "video": 2, "short": 3, "tvMiniSeries": 4, "tvSpecial": 5}
    unresolved = 0
    for r in rows:
        tt = by_target.get(r["target"] or "")
        if not tt:
            n = norm(r["film"])
            for dy in (0, -1, 1, -2):
                cands = index.get((n, r["year"] + dy))
                if cands:
                    short = "short" in r["cat"].lower()
                    tt = sorted(cands, key=lambda c: (type_rank.get(c[1], 9) if not short else (c[1] != "short")))[0][0]
                    break
        r["imdb"] = tt
        unresolved += not tt
    log(f"  Wikipedia Sundance: {len(rows)} kayıt, {unresolved} tanesi IMDb ile eşleşmedi")
    return rows

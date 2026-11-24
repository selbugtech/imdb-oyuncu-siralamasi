"""Prestijli film ödüllerini Wikidata'dan çeker ve docs/awards.json üretir.

Kaynak: Wikidata (CC0). Filme verilen ödüller (P166) ile kişiye bir film için
verilen ödüller (P166 + "for work" P1686 niteleyicisi) birlikte toplanır.

Kullanım:
    python fetch_awards.py
    python fetch_awards.py --endpoint https://query.wikidata.org/sparql
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import sundance_wiki

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "docs", "awards.json")
# Wikidata'nın kendi servisi (query.wikidata.org) yoğunlukta çok sıkı hız sınırı uyguluyor;
# varsayılan olarak aynı veriyi sunan QLever kullanılır. --endpoint ile değiştirilebilir.
ENDPOINT = "https://qlever.dev/api/wikidata"
PREFIXES = """PREFIX wd: <http://www.wikidata.org/entity/>
PREFIX wdt: <http://www.wikidata.org/prop/direct/>
PREFIX p: <http://www.wikidata.org/prop/>
PREFIX ps: <http://www.wikidata.org/prop/statement/>
PREFIX pq: <http://www.wikidata.org/prop/qualifier/>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
"""
UA = "imdb-ranking-app/1.0 (awards export script)"

# (kimlik, görünen ad, etiket deseni, kategori adından atılacak önek)
FESTIVALS = [
    ("oscar", "Oscar", r"^Academy Award for |^Special Achievement Academy Award", r"^Academy Award for "),
    ("bafta", "BAFTA", r"^(BAFTA|British Academy Film) Award", r"^(BAFTA|British Academy Film) Award (for )?"),
    ("globe", "Altın Küre", r"^Golden Globe Award for ", r"^Golden Globe Award for "),
    ("cannes", "Cannes", r"Cannes|Palme d'Or|Caméra d'Or|Queer Palm", r"^(Cannes Film Festival|Festival de Cannes)( Award)?( for)? "),
    ("venice", "Venedik", r"Venice|Golden Lion|Silver Lion|Volpi Cup", r"^(Venice Film Festival|Venice)( Award)?( for)? "),
    ("berlin", "Berlin", r"Berlin|Golden Bear|Silver Bear", r"^(Berlin International Film Festival|Berlinale)( Award)?( for)? "),
    ("sundance", "Sundance", r"Sundance", r"^Sundance( Film Festival)?( -)? ?"),
]
BATCH = 150
EXCLUDE = re.compile(
    r"Television|TV |Series|Lifetime|Honorary|Cecil B\. DeMille|Fellowship"
    r"|^(\d+\w* )?(Cannes|Venice|Berlin|Sundance)\w*( International| World)? Film Festival$"
    r"|Pornfilm|Art Film Festival",
    re.I,
)


_pace = {"gap": 0.0, "last": 0.0}


def sparql(query, retries=8):
    """Sorgu çalıştırır; hız sınırına (HTTP 429) takılırsa bekleyip sonraki sorguları da yavaşlatır."""
    url = ENDPOINT + "?" + urllib.parse.urlencode({"query": PREFIXES + query})
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/sparql-results+json"})
    for attempt in range(retries):
        wait = _pace["last"] + _pace["gap"] - time.time()
        if wait > 0:
            time.sleep(wait)
        _pace["last"] = time.time()
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                return json.load(r)["results"]["bindings"]
        except urllib.error.HTTPError as e:
            if e.code != 429 or attempt == retries - 1:
                raise
            delay = int(e.headers.get("Retry-After") or 0) or 65
            _pace["gap"] = max(_pace["gap"], delay)
            print(f"  Wikidata hız sınırı, {delay} sn bekleniyor...", flush=True)
        except Exception as e:  # noqa: BLE001
            if attempt == retries - 1:
                raise
            print(f"  tekrar deneniyor ({e})", flush=True)
            _pace["gap"] = max(_pace["gap"], 10)


def qid(v):
    return v["value"].rsplit("/", 1)[-1]


def candidate_awards():
    """IMDb kimliği olan yapımlarla ilişkili tüm ödülleri ve İngilizce adlarını döndürür."""
    found = {}
    queries = [
        # filme verilen ödüller
        """SELECT DISTINCT ?award ?l WHERE {
             ?f wdt:P345 ?imdb ; wdt:P166 ?award .
             FILTER(STRSTARTS(?imdb, "tt"))
             ?award rdfs:label ?l FILTER(LANG(?l) = "en") }""",
        # bir film için kişiye verilen ödüller
        """SELECT DISTINCT ?award ?l WHERE {
             ?p p:P166 ?st . ?st ps:P166 ?award ; pq:P1686 ?f .
             ?f wdt:P345 ?imdb FILTER(STRSTARTS(?imdb, "tt"))
             ?award rdfs:label ?l FILTER(LANG(?l) = "en") }""",
    ]
    for q in queries:
        for b in sparql(q):
            found[qid(b["award"])] = b["l"]["value"]
    return found


def classify(awards):
    out = {}
    for q, label in awards.items():
        if EXCLUDE.search(label):
            continue
        for key, _, pattern, prefix in FESTIVALS:
            if re.search(pattern, label):
                cat = re.sub(prefix, "", label).strip(" -–:") or label
                out[q] = (key, cat[0].upper() + cat[1:])
                break
    return out


def year_of(b, key):
    v = b.get(key)
    return int(v["value"][:4]) if v and re.match(r"^\d{4}", v["value"]) else None


def fetch_wins(award_ids):
    """Kazananları üç biçimden toplar: filmin "ödül aldı" (P166) kaydı, kişinin bir film için
    aldığı ödül (P166 + P1686) ve ödül kaydının kendisindeki "kazanan" (P1346) listesi."""
    rows = []
    ids = sorted(award_ids)
    for i in range(0, len(ids), BATCH):
        values = " ".join(f"wd:{a}" for a in ids[i : i + BATCH])
        film_tail = """?f wdt:P345 ?imdb FILTER(STRSTARTS(?imdb, "tt"))
            OPTIONAL {{ ?st pq:P585 ?date }}
            OPTIONAL {{ ?f rdfs:label ?fl FILTER(LANG(?fl) = "en") }}"""
        person_tail = film_tail + """
            OPTIONAL {{ ?p wdt:P345 ?pimdb }}
            OPTIONAL {{ ?p rdfs:label ?pl FILTER(LANG(?pl) = "en") }}"""
        queries = [
            ("?f p:P166 ?st . ?st ps:P166 ?award .", film_tail),
            ("?p p:P166 ?st . ?st ps:P166 ?award ; pq:P1686 ?f .", person_tail),
            ("?award p:P1346 ?st . ?st ps:P1346 ?f .", film_tail),
            ("?award p:P1346 ?st . ?st ps:P1346 ?p ; pq:P1686 ?f .", person_tail),
        ]
        for head, tail in queries:
            q = f"SELECT * WHERE {{ VALUES ?award {{ {values} }} {head} {tail.format()} }}"
            for b in sparql(q):
                pimdb = b.get("pimdb", {}).get("value")
                rows.append((qid(b["award"]), b["imdb"]["value"], b.get("fl", {}).get("value"), year_of(b, "date"),
                             b.get("pl", {}).get("value"), pimdb if pimdb and pimdb.startswith("nm") else None))
        print(f"  {min(i + BATCH, len(ids))}/{len(ids)} ödül sorgulandı", flush=True)
    return rows


def main():
    global ENDPOINT
    ap = argparse.ArgumentParser()
    ap.add_argument("--endpoint", default=ENDPOINT, help="SPARQL uç noktası")
    ap.add_argument("--no-wikipedia", action="store_true", help="Sundance için Wikipedia'yı kullanma")
    a = ap.parse_args()
    ENDPOINT = a.endpoint
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass
    print("Ödül türleri toplanıyor...", flush=True)
    awards = classify(candidate_awards())
    # Sundance kategorileri, Wikipedia ile birleşebilmesi için tek bir adlandırmaya indirgenir.
    awards = {q: (f, sundance_wiki.canon(c) or c) if f == "sundance" else (f, c) for q, (f, c) in awards.items()}
    print(f"  {len(awards)} ödül kategorisi bulundu", flush=True)

    print("Kazananlar çekiliyor (Wikidata)...", flush=True)
    rows = [(awards[aw], imdb, film, year, person, pimdb, False) for aw, imdb, film, year, person, pimdb in fetch_wins(awards)]

    if not a.no_wikipedia:
        print("Sundance kazananları çekiliyor (Wikipedia)...", flush=True)
        for r in sundance_wiki.sundance_wins(sparql, log=lambda m: print(m, flush=True)):
            people = r["people"] or [None]
            for person in people:
                rows.append((("sundance", r["cat"]), r["imdb"], r["film"], r["year"], person, None, True))

    # Aynı kategori + film tek kayıt; birden çok kazanan kişi (ör. yapımcılar) birleştirilir.
    merged = {}
    for cat, imdb, film, year, person, pimdb, from_wiki in rows:
        key = (cat, imdb or "title:" + sundance_wiki.norm(film or ""))
        m = merged.setdefault(key, {"imdb": imdb, "film": film, "year": year, "wiki": from_wiki, "people": {}})
        if from_wiki and not m["wiki"]:  # festival yılı Wikipedia'da daha güvenilir
            m["year"], m["wiki"] = year or m["year"], True
        elif year and not m["wiki"] and (not m["year"] or year < m["year"]):
            m["year"] = year
        if person:
            pid = int(pimdb[2:]) if pimdb else 0
            if pid or person not in m["people"]:
                m["people"][person] = pid
    # Aynı kişi Wikidata'da kimlikli, Wikipedia'da kimliksiz gelebilir: tekilleştir
    final = [(cat, m["imdb"], m["film"], m["year"], sorted(m["people"].items())) for (cat, _), m in merged.items()]

    cats = sorted({c for c, *_ in final})
    cat_index = {c: n for n, c in enumerate(cats)}
    data = {
        "source": "Wikidata (CC0), Sundance için ayrıca Wikipedia (CC BY-SA)",
        "fetchedAt": time.strftime("%Y-%m-%d"),
        "festivals": [{"id": k, "name": n} for k, n, *_ in FESTIVALS],
        "categories": [{"f": f, "name": c} for f, c in cats],
        # [kategori indeksi, imdb no, film adı, yıl, [[kişi, kişi imdb no], ...]]
        "wins": sorted(
            ([cat_index[c], int(i[2:]) if i else 0, f or "", y or 0, [list(x) for x in people]]
             for c, i, f, y, people in final),
            key=lambda r: (-r[3], r[0]),
        ),
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, separators=(",", ":"))
    by_f = {}
    for c, *_ in data["wins"]:
        by_f[cats[c][0]] = by_f.get(cats[c][0], 0) + 1
    print(f"{len(data['wins']):,} ödül kaydı -> {OUT} ({os.path.getsize(OUT) / 1e6:.2f} MB)")
    print("  " + ", ".join(f"{k}: {v}" for k, v in sorted(by_f.items())))


if __name__ == "__main__":
    main()

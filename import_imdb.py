"""IMDb resmi veri setlerini indirip SQLite veritabanına aktarır.

Kaynak: https://datasets.imdbws.com/ (kişisel ve ticari olmayan kullanım içindir)

Kullanım:
    python import_imdb.py                 # varsayılan: en az 1000 oy alan yapımlar
    python import_imdb.py --min-votes 500 # daha geniş veri
    python import_imdb.py --skip-download # data/raw içindeki dosyaları kullan
"""
import argparse
import gzip
import os
import sqlite3
import sys
import time
import urllib.request

BASE_URL = "https://datasets.imdbws.com/"
FILES = [
    "title.ratings.tsv.gz",
    "title.basics.tsv.gz",
    "title.principals.tsv.gz",
    "name.basics.tsv.gz",
]
HERE = os.path.dirname(os.path.abspath(__file__))
RAW_DIR = os.path.join(HERE, "data", "raw")
DB_PATH = os.path.join(HERE, "data", "imdb.db")

TITLE_TYPES = {"movie", "tvMovie", "tvSeries", "tvMiniSeries"}
CATEGORIES = {"actor", "actress"}


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def download(name, force=False):
    os.makedirs(RAW_DIR, exist_ok=True)
    dest = os.path.join(RAW_DIR, name)
    if os.path.exists(dest) and not force:
        log(f"{name} zaten var, indirme atlandı")
        return dest
    tmp = dest + ".part"
    req = urllib.request.Request(BASE_URL + name, headers={"User-Agent": "imdb-actor-ranking/1.0"})
    with urllib.request.urlopen(req) as resp, open(tmp, "wb") as out:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        last = 0.0
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            out.write(chunk)
            done += len(chunk)
            now = time.time()
            if now - last > 2:
                pct = f"{done * 100 / total:5.1f}%" if total else ""
                print(f"\r  {name}: {done / 1e6:8.1f} MB {pct}", end="", flush=True)
                last = now
    print()
    os.replace(tmp, dest)
    log(f"{name} indirildi")
    return dest


def rows(name):
    path = os.path.join(RAW_DIR, name)
    with gzip.open(path, "rt", encoding="utf-8", newline="\n") as f:
        next(f)  # başlık satırı
        for line in f:
            yield line.rstrip("\n").split("\t")


def nullable_int(v):
    return None if v == "\\N" else int(v)


def build(min_votes):
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    tmp_db = DB_PATH + ".tmp"
    if os.path.exists(tmp_db):
        os.remove(tmp_db)
    db = sqlite3.connect(tmp_db)
    db.executescript(
        """
        PRAGMA journal_mode = OFF;
        PRAGMA synchronous = OFF;
        CREATE TABLE titles (
            tconst TEXT PRIMARY KEY, type TEXT, title TEXT, year INTEGER,
            genres TEXT, rating REAL, votes INTEGER
        );
        CREATE TABLE credits (
            tconst TEXT, nconst TEXT, category TEXT, ordering INTEGER, characters TEXT
        );
        CREATE TABLE names (
            nconst TEXT PRIMARY KEY, name TEXT, birth INTEGER, death INTEGER, professions TEXT
        );
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
        """
    )

    log("Puanlar okunuyor...")
    ratings = {}
    for tconst, rating, votes in rows("title.ratings.tsv.gz"):
        v = int(votes)
        if v >= min_votes:
            ratings[tconst] = (float(rating), v)
    log(f"  {len(ratings):,} yapım en az {min_votes} oy almış")

    log("Yapım bilgileri okunuyor...")
    batch = []
    for r in rows("title.basics.tsv.gz"):
        tconst, ttype = r[0], r[1]
        if ttype not in TITLE_TYPES or r[4] == "1":  # yetişkin içerik hariç
            continue
        rv = ratings.get(tconst)
        if rv is None:
            continue
        genres = "" if r[8] == "\\N" else r[8]
        batch.append((tconst, ttype, r[2], nullable_int(r[5]), genres, rv[0], rv[1]))
    db.executemany("INSERT INTO titles VALUES (?,?,?,?,?,?,?)", batch)
    title_ids = {b[0] for b in batch}
    del ratings, batch
    log(f"  {len(title_ids):,} yapım kaydedildi")

    log("Oyuncu kadroları okunuyor (en büyük dosya, birkaç dakika sürebilir)...")
    # Aynı kişi bir yapımda birden çok satırda olabilir: (yapım, kişi) başına tek kayıt tutulur.
    credits = {}
    for r in rows("title.principals.tsv.gz"):
        if r[0] not in title_ids or r[3] not in CATEGORIES:
            continue
        chars = [] if r[5] == "\\N" else r[5].strip("[]").replace('","', "\x00").strip('"').split("\x00")
        # Belgesel/program görünümleri ("Self", "Self - Host") oyunculuk sayılmaz.
        chars = [c for c in chars if not c.startswith("Self")] if chars else chars
        if r[5] != "\\N" and not chars:
            continue
        key = (r[0], r[2])
        prev = credits.get(key)
        if prev is None:
            credits[key] = [r[3], int(r[1]), chars]
        else:
            prev[1] = min(prev[1], int(r[1]))
            prev[2] += [c for c in chars if c not in prev[2]]
    db.executemany(
        "INSERT INTO credits VALUES (?,?,?,?,?)",
        ((t, n, cat, o, " / ".join(ch)) for (t, n), (cat, o, ch) in credits.items()),
    )
    actor_ids = {n for _, n in credits}
    log(f"  {len(credits):,} oyuncu kaydı, {len(actor_ids):,} farklı oyuncu")
    del credits

    log("İsimler okunuyor...")
    batch = []
    for r in rows("name.basics.tsv.gz"):
        if r[0] in actor_ids:
            batch.append((r[0], r[1], nullable_int(r[2]), nullable_int(r[3]), r[4].replace("\\N", "")))
    db.executemany("INSERT INTO names VALUES (?,?,?,?,?)", batch)
    log(f"  {len(batch):,} isim kaydedildi")

    log("İndeksler oluşturuluyor...")
    db.executescript(
        """
        CREATE INDEX idx_credits_nconst ON credits(nconst);
        CREATE INDEX idx_credits_tconst ON credits(tconst);
        CREATE INDEX idx_titles_filter ON titles(type, year);
        CREATE INDEX idx_names_name ON names(name COLLATE NOCASE);
        """
    )
    db.executemany(
        "INSERT INTO meta VALUES (?,?)",
        [("min_votes", str(min_votes)), ("imported_at", time.strftime("%Y-%m-%d %H:%M"))],
    )
    db.commit()
    db.execute("ANALYZE")
    db.close()
    os.replace(tmp_db, DB_PATH)
    log(f"Hazır: {DB_PATH}")


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--min-votes", type=int, default=1000, help="yapım başına en az oy (varsayılan 1000)")
    p.add_argument("--skip-download", action="store_true", help="mevcut dosyaları kullan")
    p.add_argument("--force-download", action="store_true", help="dosyaları yeniden indir")
    a = p.parse_args()
    if not a.skip_download:
        for f in FILES:
            download(f, force=a.force_download)
    missing = [f for f in FILES if not os.path.exists(os.path.join(RAW_DIR, f))]
    if missing:
        sys.exit(f"Eksik dosyalar: {', '.join(missing)}")
    build(a.min_votes)


if __name__ == "__main__":
    main()

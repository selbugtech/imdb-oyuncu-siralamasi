"""data/imdb.db içinden GitHub Pages için sıkıştırılmış docs/data.json üretir.

Kullanım:
    python export_static.py                # oyunculukta veya yönetmenlikte en az 3 yapımı olan kişiler
    python export_static.py --min-credits 5
"""
import argparse
import json
import os
import sqlite3

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "data", "imdb.db")
OUT = os.path.join(HERE, "docs", "data.json")
TYPES = ["movie", "tvMovie", "tvSeries", "tvMiniSeries"]


def num(imdb_id):
    return int(imdb_id[2:])


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--min-credits", type=int, default=3)
    a = p.parse_args()

    db = sqlite3.connect(DB_PATH)
    meta = dict(db.execute("SELECT key, value FROM meta"))

    genres = set()
    for (g,) in db.execute("SELECT DISTINCT genres FROM titles WHERE genres != ''"):
        genres.update(g.split(","))
    genres = sorted(genres)
    gbit = {g: 1 << i for i, g in enumerate(genres)}

    t = {"id": [], "ty": [], "ti": [], "y": [], "g": [], "r": [], "v": []}
    tindex = {}
    for tconst, ttype, title, year, gs, rating, votes in db.execute(
        "SELECT tconst, type, title, year, genres, rating, votes FROM titles ORDER BY votes DESC"
    ):
        tindex[tconst] = len(t["id"])
        t["id"].append(num(tconst))
        t["ty"].append(TYPES.index(ttype))
        t["ti"].append(title)
        t["y"].append(year or 0)
        t["g"].append(sum(gbit[g] for g in gs.split(",")) if gs else 0)
        t["r"].append(round(rating * 10))
        t["v"].append(votes)

    actors = {"id": [], "n": [], "b": [], "d": [], "s": []}
    credits = []
    rows = db.execute(
        """
        SELECT c.nconst, n.name, n.birth, n.death, c.tconst, c.ordering, c.category
        FROM credits c JOIN names n ON n.nconst = c.nconst
        WHERE c.nconst IN (
            SELECT nconst FROM credits GROUP BY nconst
            HAVING SUM(category != 'director') >= ? OR SUM(category = 'director') >= ?
        )
        ORDER BY c.nconst
        """,
        (a.min_credits, a.min_credits),
    )
    last = None
    for nconst, name, birth, death, tconst, ordering, category in rows:
        if nconst != last:
            actors["id"].append(num(nconst))
            actors["n"].append(name)
            actors["b"].append(birth or 0)
            actors["d"].append(death or 0)
            actors["s"].append(len(credits))
            last = nconst
        # tek sayı: yapım indeksi * 64 + sıra * 4 + görev (0 oyuncu, 1 kadın oyuncu, 2 yönetmen)
        role = {"actor": 0, "actress": 1, "director": 2}[category]
        credits.append(tindex[tconst] * 64 + min(ordering, 15) * 4 + role)
    actors["s"].append(len(credits))

    data = {
        "meta": {**meta, "minCredits": a.min_credits},
        "types": TYPES,
        "genres": genres,
        "titles": t,
        "actors": actors,
        "credits": credits,
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    print(f"{len(t['id']):,} yapım, {len(actors['id']):,} oyuncu, {len(credits):,} kayıt -> {OUT} "
          f"({os.path.getsize(OUT) / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()

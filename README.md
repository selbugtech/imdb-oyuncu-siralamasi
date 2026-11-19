# IMDb Oyuncu Sıralaması

IMDb'nin resmi veri setlerinden ([IMDb Non-Commercial Datasets](https://developer.imdb.com/non-commercial-datasets/)) oyuncuları rol aldıkları yapımların puanlarına göre sıralayan statik web uygulaması. Sıralama tamamen tarayıcıda hesaplanır; GitHub Pages üzerinde `docs/` klasöründen yayınlanır.

> Veriler IMDb'ye aittir ve yalnızca **kişisel ve ticari olmayan** kullanım içindir. Bu proje IMDb ile bağlantılı değildir.

## Yerelde çalıştırma

```powershell
python -m http.server 8000 -d docs
```

Ardından http://localhost:8000 adresini açın.

## Veriyi güncelleme

Yalnızca Python 3 standart kütüphanesi gerekir.

```powershell
python import_imdb.py --force-download   # ~1,3 GB indirir, data/imdb.db oluşturur
python export_static.py                  # docs/data.json üretir
```

`import_imdb.py --min-votes 500` daha fazla yapım alır; `export_static.py --min-credits 5` dosyayı küçültür.

## Sıralama ölçütleri

| Ölçüt | Açıklama |
|---|---|
| Bayes puanı | `(n·ort + m·C) / (n + m)`. n: oyuncunun yapım sayısı, C: filtrelenen yapımların ortalaması, m: güven katsayısı. Az yapımlı oyuncuları ortalamaya çeker. |
| Ağırlıklı | Yapım puanlarının oy sayısıyla ağırlıklı ortalaması |
| Ortalama | Yapım puanlarının düz ortalaması |
| Toplam oy | Popülerlik göstergesi |
| Yapım sayısı / En iyi | Filtrelere uyan yapım sayısı ve en yüksek puanlı yapım |

Filtreler: yapım türü, cinsiyet, tür, yıl aralığı, yapım başına en az oy, oyuncu başına en az yapım, rol önemi (jenerikteki ilk N isim). Filtreler adres çubuğuna yazılır, bağlantı paylaşılabilir.

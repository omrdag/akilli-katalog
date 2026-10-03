[README.md](https://github.com/user-attachments/files/33012778/README.md)
# Akıllı Katalog

PDF katalogların **tamamından** ürünleri (SKU, ad, fiyat, görsel) otomatik çıkaran, ürünleri ortak
kategorilere ve montaj tipine ayıran bağımsız araç. ByLamp ana sistemine bağlı **değildir**,
kendi SQLite veritabanıyla çalışır.

## Özellikler
- Marka + PDF yükle → tüm katalog arka planda işlenir (ilerleme çubuğu ile)
- Ürün kodu kalıbı PDF'ten otomatik bulunur (marka bağımsız, ör. `AD05-02400`, `CT-5418`)
- Katalogda fiyat listesi sayfaları varsa fiyatlar oradan alınır, sayfalarla karşılaştırılır
- Ürün görseli PDF'teki gerçek fotoğraftan kesilir
- **Ortak kategori düzeni**: her markanın başlıkları `kategori_kurallari.json` ile
  ana grup / kategoriye eşlenir (İç Mekan › LED Paneller, Dış Mekan › Bahçe Armatürleri…)
- **Montaj tipi**: Sıva Altı, Sıva Üstü, Ray Tipi, Duvar Tipi (ürün adından ya da sayfadaki ikondan)
- Uyarılı ürünler işaretlenir (fiyat listesinde yok, fiyat bulunamadı, sayfada bulunamadı…)
- Arama, marka/kategori/montaj filtresi, seçili ürünlerden Excel fiyat teklifi
- `/sistem-kontrol` sayfası: kütüphaneler, ürün/marka sayıları

## Hazır veri
`data/ack-2026.json.gz` — ACK 2026 Fiyat Listesi'nden çıkarılmış 1.640 ürün (görselleriyle).
Veritabanı boşsa uygulama açılışta bunu otomatik yükler, böylece deploy sonrası katalog hemen dolu gelir.

| | |
|---|---|
| PDF sayfası | 178 (3–10 arası fiyat listesi) |
| Fiyat listesindeki kod | 1.609 |
| Çıkarılan ürün | 1.640 |
| Ürün görseli | 528 (varyantlar aynı görseli paylaşır) |
| Uyarılı ürün | 38 (31 fiyat listesinde yok, 6 "fiyat sorunuz", 1 sayfada bulunamadı) |

## Dosyalar
| Dosya | Görev |
|---|---|
| `main.py` | Flask uygulaması (sayfalar, yükleme işi, teklif) |
| `catalog_extract.py` | PDF'ten ürün çıkarma (PyMuPDF) |
| `kategori.py` | Ortak kategori ve montaj tipi sınıflandırma |
| `kategori_kurallari.json` | Düzenlenebilir eşleme kuralları |
| `data/*.json.gz` | Boş veritabanına yüklenen hazır kataloglar |

## Yeni marka eklerken
1. PDF'i marka adıyla yükleyin.
2. "Diğer" grubuna düşen kategorileri `kategori_kurallari.json` → `baslik` bölümüne ekleyin.
3. Sol menüdeki **Kuralları yeniden uygula** düğmesine basın (PDF'i tekrar işlemeye gerek yok).

## Yerel çalıştırma
```
pip install -r requirements.txt
python main.py
```
Tarayıcıda http://localhost:5000

Komut satırından test: `python catalog_extract.py katalog.pdf`

## Railway
- Poppler artık gerekmez (nixpacks.toml kaldırıldı).
- `Procfile`: tek worker + 4 thread (arka plan işleri aynı süreçte izlenir), 120 sn zaman aşımı.
- Not: Railway diski kalıcı değildir; yeniden deploy'da veritabanı sıfırlanır ve `data/` içindeki hazır
  katalog yeniden yüklenir. Kalıcı veri için bir Railway Volume bağlayıp `DB_PATH` ortam değişkenini
  o klasöre yönlendirin (ör. `DB_PATH=/data/akilli_katalog.db`).

# Akıllı Katalog

PDF katalog sayfalarından ürünleri (SKU, ad, fiyat, görsel) otomatik çıkaran bağımsız araç.
ByLamp ana sistemine bağlı **değildir** — kendi SQLite veritabanı ile çalışır.

## Özellikler
- PDF yükle + sayfa + ızgara düzeni seç
- Ürünleri otomatik çıkar (metin + görsel)
- Kataloğda listele, ara, filtrele
- Seçili ürünlerden Excel fiyat teklifi oluştur

## Gereksinimler
- Python 3.11
- poppler-utils (pdftotext, pdftoppm) — Railway'de nixpacks.toml ile otomatik kurulur
- Python paketleri: requirements.txt

## Yerel çalıştırma
```
pip install -r requirements.txt
python main.py
```
Tarayıcıda http://localhost:5000

## Railway'e deploy
1. Bu repoyu GitHub'a yükle
2. Railway'de "New Project" → "Deploy from GitHub repo"
3. nixpacks.toml poppler'ı otomatik kurar
4. Deploy sonrası verilen URL'den erişilir

## Notlar
- Bu bir TEST aracıdır. Çıkan sonuçlar kontrol edilmelidir.
- Izgara düzeni sayfaya göre değişebilir (örn. "3,3,3,4" veya "3,3,3,3").
- Veriler SQLite'ta (akilli_katalog.db) saklanır.

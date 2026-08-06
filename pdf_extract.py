"""
Akıllı Katalog — PDF'ten ürün çıkarma (poppler'sız).
Metin+konum: pdfplumber. Görsel: PyMuPDF/fitz. Poppler GEREKMEZ.
"""
import re
import io
import base64

import pdfplumber
import fitz
from PIL import Image

SKU_RE = re.compile(r'CT-\d+')
# Ada karışan etiketleri temizle
NOISE = {'YENi', 'YENİ', 'OYNAR', 'BAŞLIKLI', 'DAİRE', 'DELİK', 'ÇAPI', 'KARE',
         'ÇERÇEVESİZ', 'PANEL', 'AYARLANABİLİR', 'ÖLÇÜ', 'W', 'K'}


def clean_name(name):
    parts = [p for p in name.split() if p.strip() and p.strip() not in NOISE]
    return re.sub(r'\s+', ' ', ' '.join(parts)).strip()[:50]


def extract_products_with_positions(pdf_path, page_num):
    products = []
    with pdfplumber.open(pdf_path) as pdf:
        if page_num < 1 or page_num > len(pdf.pages):
            return products, None, None
        page = pdf.pages[page_num - 1]
        page_w = page.width
        page_h = page.height
        words = page.extract_words()

    skus = [(w, SKU_RE.match(w['text']).group(0)) for w in words
            if SKU_RE.match(w['text']) and 0 <= w['x0'] <= page_w and 0 <= w['top'] <= page_h]

    for sw, sku in skus:
        sy, sx = sw['top'], sw['x0']
        # aynı satırda sonraki SKU'nun x'i = sağ sınır
        next_x = page_w + 100
        for w2, s2 in skus:
            if abs(w2['top'] - sy) < 15 and w2['x0'] > sx + 5:
                next_x = min(next_x, w2['x0'])
        name_bits = []
        price = None
        raw_extra = sw['text'][len(sku):]
        if raw_extra:
            name_bits.append(raw_extra)
        for w in words:
            if abs(w['top'] - sy) < 15 and sx < w['x0'] < next_x:
                t = w['text']
                if SKU_RE.match(t):
                    continue
                if re.fullmatch(r'\d{2,4}', t.replace('₺', '').strip()):
                    if price is None:
                        price = t.replace('₺', '').strip()
                    continue
                if t.strip() and not t.startswith('₺'):
                    name_bits.append(t.strip())
        name = clean_name(' '.join(name_bits))
        products.append({'sku': sku, 'name': name, 'price': price or '0', 'x': sx, 'y': sy})

    products.sort(key=lambda p: (round(p['y'] / 20), p['x']))
    return products, page_w, page_h


def crop_image_at(pdf_path, page_num, products, page_w, page_h, dpi=150):
    doc = fitz.open(pdf_path)
    page = doc[page_num - 1]
    pix = page.get_pixmap(dpi=dpi)
    img = Image.open(io.BytesIO(pix.tobytes("png")))
    IW, IH = img.size
    sx = IW / page_w
    sy = IH / page_h
    ys = sorted(set(round(p['y']) for p in products))
    xs = sorted(set(round(p['x']) for p in products))

    def gaps(vals):
        if len(vals) < 2:
            return None
        ds = [vals[i+1]-vals[i] for i in range(len(vals)-1) if vals[i+1]-vals[i] > 5]
        return min(ds) if ds else None

    row_gap = gaps(ys) or (page_h * 0.22)
    col_gap = gaps(xs) or (page_w * 0.30)

    for p in products:
        x0 = max(0, p['x'] * sx - 5)
        y0 = max(0, p['y'] * sy - 5)
        x1 = min(IW, (p['x'] + col_gap) * sx)
        y1 = min(IH, (p['y'] + row_gap) * sy)
        try:
            crop = img.crop((int(x0), int(y0), int(x1), int(y1)))
            buf = io.BytesIO()
            crop.save(buf, format='PNG')
            p['image_b64'] = base64.b64encode(buf.getvalue()).decode()
        except Exception:
            p['image_b64'] = None
    return products


def extract_catalog_page(pdf_path, page_num, rows_layout=None):
    products, page_w, page_h = extract_products_with_positions(pdf_path, page_num)
    if not products:
        return []
    products = crop_image_at(pdf_path, page_num, products, page_w, page_h)
    return [{'sku': p['sku'], 'name': p['name'], 'price': p['price'],
             'image_b64': p.get('image_b64')} for p in products]


if __name__ == '__main__':
    pdf = '/mnt/user-data/uploads/cata-2024-fiyat-listesi.pdf'
    prods = extract_catalog_page(pdf, 9)
    print(f"Cikarilan: {len(prods)} urun")
    for p in prods:
        has = "var" if p.get('image_b64') else "yok"
        print(f"  {p['sku']:8} | {p['name']:32} | {p['price']:>4} | gorsel:{has}")

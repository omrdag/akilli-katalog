"""
Akıllı Katalog — PDF'ten ürün çıkarma modülü.

Yaklaşım:
- Metin: pdftotext (poppler) ile -layout modunda. Bu PDF türünde en güvenilir yol.
- Görsel: pdftoppm (poppler) ile sayfayı PNG'ye çevir, ızgaraya göre hücre kırp.
- Eşleştirme: metindeki SKU sırası ile ızgara hücre sırası (soldan sağa, üstten alta).

poppler-utils gereklidir (pdftotext, pdftoppm). Railway'de nixpacks.toml ile kurulur.
"""
import re
import os
import subprocess
import base64
import io
import tempfile


SKU_RE = re.compile(r'CT-\d+(?:\s+[A-Z]{1,3})?')


def extract_page_text(pdf_path, page_num):
    """Belirli bir sayfanın metnini -layout modunda döndürür."""
    try:
        result = subprocess.run(
            ['pdftotext', '-f', str(page_num), '-l', str(page_num), '-layout', pdf_path, '-'],
            capture_output=True, text=True, timeout=30
        )
        return result.stdout
    except Exception as e:
        print(f"[PDFExtract] metin hatası: {e}")
        return ""


def parse_products_from_text(text):
    """Metinden ürünleri (sku, ad, fiyat) sırayla çıkarır.
    Katalog formatı: 'CT-5418 ASSOS (SİYAH-BAKIR)   110'
    """
    products = []
    # Satırları tara; her CT- kodu bir ürün başlangıcı
    # Metinde birden fazla ürün aynı satırda olabilir (3 sütun yan yana)
    # Bu yüzden tüm metinde CT-XXXX ... fiyat kalıplarını yakalıyoruz.
    # Kalıp: CT-<rakam>[ SB/SP/BP/BB] <AD ...> <fiyat(rakam)>
    pattern = re.compile(
        r'(CT-\d+(?:\s+[A-Z]{1,3})?)\s+'      # SKU (opsiyonel varyant SB/SP...)
        r'([A-ZÇĞİÖŞÜa-zçğıöşü0-9\.\-\s\(\)/]+?)\s+'  # ürün adı
        r'(\d{2,4})(?:\s|₺|i|$)'              # fiyat (2-4 hane)
    )
    for m in pattern.finditer(text):
        sku = m.group(1).strip()
        name = m.group(2).strip()
        price = m.group(3).strip()
        # Ürün adı çok uzun/kirliyse kırp
        name = re.sub(r'\s+', ' ', name)
        if len(name) > 60:
            name = name[:60]
        products.append({'sku': sku, 'name': name, 'price': price})
    return products


def render_page_image(pdf_path, page_num, dpi=150):
    """Sayfayı PNG'ye çevirip yolunu döndürür."""
    tmpdir = tempfile.mkdtemp()
    prefix = os.path.join(tmpdir, 'page')
    try:
        subprocess.run(
            ['pdftoppm', '-f', str(page_num), '-l', str(page_num), '-png', '-r', str(dpi), pdf_path, prefix],
            capture_output=True, timeout=60
        )
        # pdftoppm çıktısı: page-NN.png
        for f in os.listdir(tmpdir):
            if f.endswith('.png'):
                return os.path.join(tmpdir, f)
    except Exception as e:
        print(f"[PDFExtract] görüntü hatası: {e}")
    return None


def crop_grid_cells(image_path, rows_layout):
    """Sayfa görüntüsünü ızgaraya göre hücrelere böler.
    rows_layout: her satırdaki sütun sayısı listesi, örn. [3,3,3,4]
    Döndürür: base64 PNG listesi (okuma sırasına göre).
    """
    from PIL import Image
    img = Image.open(image_path)
    W, H = img.size

    # Kenar/başlık boşlukları (deneysel, 150 dpi A4 için)
    left_margin = int(W * 0.038)
    right_margin = int(W * 0.038)
    top = int(H * 0.078)
    bottom = int(H * 0.96)

    grid_w = W - left_margin - right_margin
    n_rows = len(rows_layout)
    row_h = (bottom - top) / n_rows

    cells_b64 = []
    for r, ncols in enumerate(rows_layout):
        col_w = grid_w / ncols
        for c in range(ncols):
            x1 = int(left_margin + c * col_w)
            y1 = int(top + r * row_h)
            x2 = int(left_margin + (c + 1) * col_w)
            y2 = int(top + (r + 1) * row_h)
            crop = img.crop((x1, y1, x2, y2))
            buf = io.BytesIO()
            crop.save(buf, format='PNG')
            b64 = base64.b64encode(buf.getvalue()).decode()
            cells_b64.append(b64)
    return cells_b64


def extract_catalog_page(pdf_path, page_num, rows_layout):
    """Ana fonksiyon: bir sayfadan ürünleri (metin + görsel) çıkarır.
    rows_layout: örn. [3,3,3,4] — her satırdaki sütun sayısı.
    Döndürür: [{'sku','name','price','image_b64'}, ...]
    """
    text = extract_page_text(pdf_path, page_num)
    products = parse_products_from_text(text)

    img_path = render_page_image(pdf_path, page_num)
    cells = []
    if img_path:
        try:
            cells = crop_grid_cells(img_path, rows_layout)
        except Exception as e:
            print(f"[PDFExtract] kırpma hatası: {e}")

    # Ürünleri hücrelerle eşleştir (sıra bazlı)
    results = []
    for i, prod in enumerate(products):
        prod['image_b64'] = cells[i] if i < len(cells) else None
        results.append(prod)
    return results


if __name__ == '__main__':
    # Yerel test
    pdf = '/mnt/user-data/uploads/cata-2024-fiyat-listesi.pdf'
    page = 9
    layout = [3, 3, 3, 4]
    prods = extract_catalog_page(pdf, page, layout)
    print(f"Çıkarılan ürün sayısı: {len(prods)}")
    for p in prods:
        has_img = "✓görsel" if p.get('image_b64') else "✗görsel"
        print(f"  {p['sku']:14} | {p['name']:30} | {p['price']:>4}₺ | {has_img}")

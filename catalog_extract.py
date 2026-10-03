"""
Akıllı Katalog — Tüm PDF katalogdan ürün çıkarma.

Her sayfadan: ürün kodu (SKU), fiyat, ürün görseli, katalog başlığı (kategori),
alt grup başlığı ve varyant bilgisi (güç, renk, K, lm) çıkarılır.

Marka bağımsız çalışır:
  * SKU kalıbı PDF'in kendisinden otomatik bulunur (en sık tekrar eden kod biçimi).
  * Katalogda "fiyat listesi / içindekiler" sayfaları varsa (çok sayıda kod + fiyat
    bulunan sayfalar) fiyatlar oradan alınır ve sayfadaki ürünlerle karşılaştırılır.
  * Kategori, sayfa kenarındaki dikey başlıktan (yoksa üst başlıktan) okunur.

Kütüphaneler: PyMuPDF (pymupdf). Poppler GEREKMEZ.
"""
import re
import io
import collections

import pymupdf as fitz
from PIL import Image

PRICE_RE = re.compile(r'(\d[\d.]*,\d{2}|\d+[.]\d{2})\s*(USD|EUR|TL|₺|\$|€)')
POWER_RE = re.compile(r'^\d+(?:[.,]\d+)?\s*W\b')
CODE_TOKEN = re.compile(r'^[A-ZÇĞİÖŞÜ]{1,5}[- ]?\d[\dA-Z]*(?:[-./]\d[\dA-Z]*)*$')
NOISE_RE = re.compile(r'Boyutlar/Dimensions:?|FİYAT S\.?|Kesim Ölçüsü.*')


# ───────────────────────── yardımcılar ─────────────────────────
def _shape(tok):
    """'AD05-02400' -> 'AA99-99999'"""
    return re.sub(r'[0-9]', '9', re.sub(r'[A-ZÇĞİÖŞÜ]', 'A', tok))


def _dist(a, b):
    dx = max(b.x0 - a.x1, a.x0 - b.x1, 0)
    dy = max(b.y0 - a.y1, a.y0 - b.y1, 0)
    return (dx * dx + dy * dy) ** .5


def _num(s):
    if not s:
        return None
    try:
        return float(s.replace('.', '').replace(',', '.')) if ',' in s else float(s)
    except ValueError:
        return None


def _clean(s):
    s = NOISE_RE.sub('', s or '')
    s = re.sub(r'\s+-\s*$', '', s)
    return re.sub(r'\s+', ' ', s).strip(' -')


# ───────────────────────── SKU kalıbı ─────────────────────────
def detect_sku_pattern(doc, sample_pages=60):
    """PDF'te en çok tekrar eden kod biçimini bulur ve regex döndürür."""
    shapes = collections.Counter()
    n = doc.page_count
    step = max(1, n // sample_pages)
    for i in range(0, n, step):
        for w in doc[i].get_text('words'):
            t = w[4].strip()
            if len(t) >= 5 and CODE_TOKEN.match(t) and re.search(r'\d{3,}', t):
                shapes[_shape(t)] += 1
    if not shapes:
        return None
    top, cnt = shapes.most_common(1)[0]
    if cnt < 10:
        return None
    # Biçimi regex'e çevir
    rx = ''
    for ch, grp in __import__('itertools').groupby(top):
        k = len(list(grp))
        rx += {'A': f'[A-ZÇĞİÖŞÜ]{{{k}}}', '9': f'\\d{{{k}}}'}.get(ch, re.escape(ch) * k)
    return re.compile(r'(?<![\w-])' + rx + r'[A-Z]?(?![\w])')


# ───────────────────────── fiyat listesi ─────────────────────────
def read_price_list(doc, sku_re, min_codes=60):
    """Çok sayıda kod içeren sayfaları fiyat listesi kabul eder: {sku: {'price','page','raw'}}"""
    idx, pages = {}, []
    for i in range(doc.page_count):
        page = doc[i]
        words = page.get_text('words')
        skus = [w for w in words if sku_re.fullmatch(w[4])]
        if len(skus) < min_codes:
            continue
        pages.append(i + 1)
        for s in skus:
            y = (s[1] + s[3]) / 2
            row = sorted([w for w in words if abs((w[1] + w[3]) / 2 - y) < 3 and s[2] < w[0] < s[2] + 120],
                         key=lambda w: w[0])
            txt = ' '.join(w[4] for w in row)
            pm = re.search(r'([\d.,]+)\s*(USD|EUR|TL|₺)', txt)
            sm = re.search(r'(?:sf|s\.|sayfa)\s*(\d+)', txt, re.I)
            ask = bool(re.search(r'sorunuz', txt, re.I))
            idx[s[4]] = {'price': _num(pm.group(1)) if pm else None,
                         'currency': pm.group(2) if pm else None,
                         'page': int(sm.group(1)) if sm else None,
                         'ask': ask}
    return idx, pages


# ───────────────────────── sayfa analizi ─────────────────────────
def _side_title(page):
    """Sayfa kenarındaki dikey başlık (ör. 'LED PANELLER')."""
    W = page.rect.width
    found = []
    for b in page.get_text('dict')['blocks']:
        for l in b.get('lines', []):
            if l['dir'] == (0.0, -1.0):
                x0, y0, x1, y1 = l['bbox']
                if (x1 < 30 or x0 > W - 30) and y0 < 40:
                    t = ''.join(s['text'] for s in l['spans']).strip()
                    if t and not t.isdigit():
                        found.append((x0, t))
    found.sort()
    return found[0][1] if found else ''


def _lines(page):
    out = []
    for b in page.get_text('dict')['blocks']:
        for l in b.get('lines', []):
            if l['dir'] != (1.0, 0.0):
                continue
            t = ''.join(s['text'] for s in l['spans']).strip()
            if t:
                out.append((fitz.Rect(l['bbox']), t))
    return out


def _subtitles(lines):
    """'TR <başlık>' biçimindeki alt grup başlıkları."""
    res = []
    for r, t in lines:
        m = re.match(r'^TR\s*(.*)$', t)
        if not m:
            continue
        txt = m.group(1).strip()
        if not txt:
            c = [(abs(r2.y0 - r.y0) + abs(r2.x0 - r.x1) * .2, t2) for r2, t2 in lines
                 if r2.x0 >= r.x1 - 1 and abs(r2.y0 - r.y0) < 6 and t2 != 'TR']
            if c:
                txt = min(c)[1]
        if len(txt) > 3:
            res.append((r, txt))
    return res


def _photo_regions(page):
    """Ürün fotoğrafı olabilecek görsel alanları (üst üste binenler birleştirilir)."""
    W, H = page.rect.width, page.rect.height
    boxes = []
    for im in page.get_image_info():
        r = fitz.Rect(im['bbox']) & page.rect
        if r.is_empty or r.width < 45 or r.height < 45:
            continue
        if r.width * r.height > .5 * W * H or r.width / r.height > 6 or r.height / r.width > 6:
            continue
        boxes.append(r)
    merged = True
    while merged:
        merged = False
        for i in range(len(boxes)):
            for j in range(i + 1, len(boxes)):
                a, b = boxes[i], boxes[j]
                if a.intersects(b) and (a & b).get_area() > .15 * min(a.get_area(), b.get_area()):
                    boxes[i] = a | b
                    boxes.pop(j)
                    merged = True
                    break
            if merged:
                break
    return boxes


def _badges(page):
    out = []
    for r, t in _lines(page):
        if t in ('SIVA ALTI', 'SIVA ÜSTÜ'):
            out.append((r, 'Sıva Altı' if 'ALTI' in t else 'Sıva Üstü'))
    return out


def _ip_marks(page):
    out = []
    for r, t in _lines(page):
        m = re.fullmatch(r'-?(IP\s?\d{2})-?', t.strip())
        if m:
            out.append((r, m.group(1).replace(' ', '')))
    return out


def _render(page, rect, max_px=360):
    z = min(max_px / max(rect.width, rect.height), 2.0)
    pix = page.get_pixmap(matrix=fitz.Matrix(z, z), clip=rect)
    im = Image.open(io.BytesIO(pix.tobytes('png'))).convert('RGB')
    buf = io.BytesIO()
    im.save(buf, 'JPEG', quality=72)
    return buf.getvalue()


def extract_page(page, pno, sku_re):
    words = page.get_text('words')
    lines = _lines(page)
    subs = _subtitles(lines)
    regions = _photo_regions(page)
    badges = _badges(page)
    ips = _ip_marks(page)
    prices = [(r, PRICE_RE.search(t)) for r, t in lines if PRICE_RE.search(t)]
    powers = [(r, t) for r, t in lines if POWER_RE.match(t) and len(t) < 40]
    title = _side_title(page)
    out, seen = [], set()
    for w in words:
        m = sku_re.search(w[4])
        if not m or m.group(0) in seen:
            continue
        sku = m.group(0)
        seen.add(sku)
        sr = fitz.Rect(w[:4])
        cy = (sr.y0 + sr.y1) / 2
        # aynı satırdaki varyant bilgisi
        row = sorted([x for x in words if abs((x[1] + x[3]) / 2 - cy) < 3 and sr.x1 < x[0] < sr.x1 + 130
                      and not sku_re.search(x[4]) and x[4] not in ('USD', 'EUR', 'TL')], key=lambda x: x[0])
        variant = re.sub(r'\b\d{1,3}(?:[.]\d{3})*,\d{2}\b', '', ' '.join(x[4] for x in row))
        # fiyat: önce aynı satır sağı, sonra aynı sütunda en yakın (alt tarafı tercih)
        same = [(r.x0 - sr.x1, pm) for r, pm in prices if abs((r.y0 + r.y1) / 2 - cy) < 4 and r.x0 > sr.x1 - 2]
        near = [(_dist(sr, r) + (0 if r.y0 >= sr.y0 - 3 else 50), pm) for r, pm in prices if abs(r.x0 - sr.x0) < 200]
        pm = min(same, key=lambda x: x[0])[1] if same else (min(near, key=lambda x: x[0])[1] if near else None)
        # güç (ör. 24W) — üstte aynı sütunda
        pw = [(sr.y0 - r.y1, t) for r, t in powers if r.y1 <= sr.y0 + 2 and abs(r.x0 - sr.x0) < 120 and sr.y0 - r.y1 < 120]
        power = min(pw)[1] if pw else ''
        # hemen üstteki etiket (renk / ürün adı)
        lab = [(sr.y0 - r.y1, t) for r, t in lines if r.y1 <= sr.y0 + 1 and sr.y0 - r.y1 < 14 and abs(r.x0 - sr.x0) < 12
               and not sku_re.search(t) and not PRICE_RE.search(t) and not POWER_RE.match(t)]
        label = min(lab)[1] if lab else ''
        # alt grup başlığı
        sb = [(sr.y0 - r.y0 + (0 if abs(r.x0 - sr.x0) < 250 else 400), t) for r, t in subs if r.y0 <= sr.y0]
        sub = min(sb)[1] if sb else (subs[0][1] if subs else '')
        reg = min(regions, key=lambda r: _dist(sr, r)) if regions else None
        badge = min(badges, key=lambda b: _dist(sr, b[0]))[1] if badges else ''
        ip = min(ips, key=lambda b: _dist(sr, b[0]))[1] if ips else ''
        out.append(dict(sku=sku, page=pno, title=title, sub=sub, power=_clean(power), label=_clean(label),
                        variant=_clean(variant), price=_num(pm.group(1)) if pm else None,
                        currency=pm.group(2) if pm else None, region=reg, badge=badge, ip=ip))
    return out


# ───────────────────────── ana fonksiyon ─────────────────────────
def extract_catalog(pdf_path, progress=None):
    """
    Tüm PDF'i işler. progress(i, n, mesaj) ile ilerleme bildirir.
    Dönüş: dict(products=[...], stats={...})
    Her ürün: sku, ad, katalog_baslik, alt_grup, ozellik, fiyat, para_birimi, sayfa,
              montaj_rozeti, image_jpeg (bytes|None), uyari (list)
    """
    doc = fitz.open(pdf_path)
    n = doc.page_count
    sku_re = detect_sku_pattern(doc)
    if not sku_re:
        raise ValueError('PDF içinde ürün kodu kalıbı bulunamadı (metin içermeyen/taranmış PDF olabilir).')
    if progress:
        progress(0, n, 'Fiyat listesi aranıyor')
    plist, plist_pages = read_price_list(doc, sku_re)

    # Fiyat listesindeki sayfa numarası basılı sayfa ise PDF sayfasıyla arasındaki kaymayı bul
    raw, last_title, cache = [], '', {}
    for i in range(n):
        pno = i + 1
        if pno in plist_pages:
            continue
        if progress and i % 5 == 0:
            progress(i, n, f'Sayfa {pno}/{n} işleniyor')
        page = doc[i]
        items = extract_page(page, pno, sku_re)
        t = _side_title(page)
        if t:
            last_title = t
        for it in items:
            if not it['title']:
                it['title'] = last_title
            if it['region'] is not None:
                key = (pno, tuple(round(v) for v in it['region']))
                if key not in cache:
                    cache[key] = _render(page, it['region'])
                it['image_jpeg'] = cache[key]
            else:
                it['image_jpeg'] = None
        raw += items

    # sayfa kayması (fiyat listesindeki 'sf' numarası ile PDF sayfası farkı)
    shifts = collections.Counter(p['page'] - plist[p['sku']]['page'] for p in raw
                                 if p['sku'] in plist and plist[p['sku']]['page'])
    shift = shifts.most_common(1)[0][0] if shifts else 0

    products, seen = [], set()
    for p in raw:
        if p['sku'] in seen:
            continue
        seen.add(p['sku'])
        power = p['power'] if not re.search(r'\d\s*W\b', p['variant']) else ''
        uyari = []
        ix = plist.get(p['sku'])
        if ix:
            price, cur = ix['price'], ix['currency'] or p['currency']
            if price is None:
                uyari.append('Fiyat sorunuz' if ix['ask'] else 'Fiyat bulunamadı')
        else:
            price, cur = p['price'], p['currency']
            if plist:
                uyari.append('Fiyat listesinde yok')
            if price is None:
                uyari.append('Fiyat bulunamadı')
        ad = ' '.join(x for x in [p['sub'], power, p['label'], p['variant']] if x)
        products.append(dict(sku=p['sku'], ad=ad, katalog_baslik=p['title'].strip(), alt_grup=p['sub'],
                             ozellik=' '.join(x for x in [power, p['label'], p['variant']] if x),
                             fiyat=price, para_birimi=cur or '', sayfa=p['page'] - shift,
                             montaj_rozeti=p['badge'], ip=p['ip'], image_jpeg=p['image_jpeg'], uyari=uyari))
    for sku, ix in plist.items():
        if sku not in seen:
            products.append(dict(sku=sku, ad='', katalog_baslik='', alt_grup='', ozellik='',
                                 fiyat=ix['price'], para_birimi=ix['currency'] or '', sayfa=ix['page'],
                                 montaj_rozeti='', ip='', image_jpeg=None,
                                 uyari=['Fiyat listesinde var, sayfada bulunamadı']))
    stats = dict(sayfa=n, fiyat_listesi_sayfalari=plist_pages, fiyat_listesi_kod=len(plist),
                 urun=len(products), gorsel=len(cache), sku_kalibi=sku_re.pattern)
    if progress:
        progress(n, n, 'Tamamlandı')
    return dict(products=products, stats=stats)


if __name__ == '__main__':
    import sys, time
    t = time.time()
    r = extract_catalog(sys.argv[1], lambda i, n, m: print(f'\r{m}', end='', flush=True))
    print()
    print(r['stats'], f'{time.time()-t:.1f}s')
    print(collections.Counter(u for p in r['products'] for u in p['uyari']))

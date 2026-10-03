"""
Akıllı Katalog — Tüm PDF katalogdan ürün çıkarma.

Her sayfadan: ürün kodu (SKU), fiyat, ürün görseli, katalog başlığı (kategori),
alt grup başlığı ve varyant bilgisi (güç, renk, K, lm) çıkarılır.

Marka bağımsız çalışır:
  * SKU kalıbı PDF'in kendisinden otomatik bulunur (en sık tekrar eden kod biçimi):
    harfli (AD05-02400, CT-5418) ya da sadece rakamlı (001-004-0005) kodlar.
    İstenirse kalıp bir örnek kodla elle verilebilir (sku_ornek).
  * Fiyat aynı satırda ("26,00 USD") ya da ayrı rozet olarak ("$" + "1,60") okunur.
  * Katalogda "fiyat listesi" sayfaları varsa fiyatlar oradan alınır.
  * Kategori, sayfa kenarındaki dikey başlıktan ya da sayfa üstündeki başlık
    şeridinden okunur; katalogda hangisi daha tutarlıysa o kullanılır.

Kütüphaneler: PyMuPDF (pymupdf), Pillow.
"""
import re
import io
import itertools
import collections

import pymupdf as fitz
from PIL import Image

CUR = r'(?:USD|EUR|TL|₺|\$|€)'
PRICE_RE = re.compile(r'(\d[\d.]*,\d{1,2}|\d+[.]\d{2})\s*(' + CUR[3:-1] + r')')
PRICE_PRE_RE = re.compile(r'(' + CUR[3:-1] + r')\s*(\d[\d.]*(?:,\d{1,2})?)')
NUM_RE = re.compile(r'^\d{1,3}(?:\.\d{3})*(?:,\d{1,2})?$|^\d+(?:[.,]\d{1,2})?$')
SYMBOL_RE = re.compile(r'^' + CUR + r'$')
POWER_RE = re.compile(r'^\d+(?:[.,]\d+)?\s*W\b')
NOISE_RE = re.compile(r'Boyutlar/Dimensions:?|FİYAT S\.?|Kesim Ölçüsü.*')
LETTER_CODE = re.compile(r'^[A-ZÇĞİÖŞÜ]{1,5}[- ]?\d[\dA-Z]*(?:[-./]\d[\dA-Z]*)*$')
DIGIT_CODE = re.compile(r'^\d{2,6}(?:[-./]\d{2,6}){1,3}[A-Z]?$')
CUR_NAME = {'$': 'USD', '€': 'EUR', '₺': 'TL'}


class ExtractError(Exception):
    """Kullanıcıya gösterilecek, açıklamalı hata."""


# ───────────────────────── yardımcılar ─────────────────────────
def _shape(tok):
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


def _shape_regex(shape):
    rx = ''
    for ch, grp in itertools.groupby(shape):
        k = len(list(grp))
        rx += {'A': f'[A-ZÇĞİÖŞÜ]{{{k}}}', '9': f'\\d{{{k}}}'}.get(ch, re.escape(ch) * k)
    return re.compile(r'(?<![\w-])' + rx + r'[A-Z]?(?![\w-])')


def _is_code(t):
    if len(t) < 5 or not re.search(r'\d{3,}', t):
        return False
    if LETTER_CODE.match(t):
        return True
    # sadece rakamlı kodlarda tarih/ölçü gibi şeyleri ele
    return bool(DIGIT_CODE.match(t)) and not re.fullmatch(r'\d{1,2}[./]\d{1,2}[./]\d{2,4}', t)


# ───────────────────────── SKU kalıbı ─────────────────────────
def detect_sku_pattern(doc, sample_pages=80, example=None):
    """PDF'te en çok tekrar eden kod biçimini bulur. example verilirse onun biçimi kullanılır."""
    if example:
        return _shape_regex(_shape(example.strip().upper()))
    shapes = collections.Counter()
    n = doc.page_count
    step = max(1, n // sample_pages)
    for i in range(0, n, step):
        for w in doc[i].get_text('words'):
            t = w[4].strip()
            if _is_code(t):
                shapes[_shape(t)] += 1
    if not shapes:
        return None
    top, cnt = shapes.most_common(1)[0]
    if cnt < 8:
        return None
    # aynı türden (rakamlı/harfli, aynı ayraçlar, sonda harf yok) ikincil biçimler de kabul edilir
    def kind(sh):
        return (sh[0], ''.join(sorted(set(re.sub(r'[A9]', '', sh)))))
    extra = [sh for sh, c in shapes.items() if sh != top and c >= 10 and kind(sh) == kind(top)
             and not sh.endswith('A') and sh.count('-') + sh.count('.') + sh.count('/') == top.count('-') + top.count('.') + top.count('/')]
    if not extra:
        return _shape_regex(top)
    parts = [_shape_regex(sh).pattern for sh in [top] + extra]
    return re.compile('|'.join(f'(?:{p})' for p in parts))


# ───────────────────────── fiyat listesi ─────────────────────────
def read_price_list(doc, sku_re, min_codes=60):
    """Çok sayıda kod + fiyat içeren sayfaları fiyat listesi kabul eder."""
    idx, pages = {}, []
    for i in range(doc.page_count):
        words = doc[i].get_text('words')
        skus = [w for w in words if sku_re.fullmatch(w[4])]
        if len(skus) < min_codes:
            continue
        rows = []
        for s in skus:
            y = (s[1] + s[3]) / 2
            row = sorted([w for w in words if abs((w[1] + w[3]) / 2 - y) < 3 and s[2] < w[0] < s[2] + 120],
                         key=lambda w: w[0])
            rows.append((s, ' '.join(w[4] for w in row)))
        with_price = [r for r in rows if re.search(r'[\d.,]+\s*' + CUR, r[1]) or re.search(r'sorunuz', r[1], re.I)]
        if len(with_price) < len(rows) * .5:
            continue          # kod listesi var ama fiyat yok: fiyat listesi değil
        pages.append(i + 1)
        for s, txt in rows:
            pm = re.search(r'([\d.,]+)\s*(' + CUR[3:-1] + ')', txt)
            sm = re.search(r'(?:sf|s\.|sayfa)\s*(\d+)', txt, re.I)
            idx[s[4]] = {'price': _num(pm.group(1)) if pm else None,
                         'currency': CUR_NAME.get(pm.group(2), pm.group(2)) if pm else None,
                         'page': int(sm.group(1)) if sm else None,
                         'ask': bool(re.search(r'sorunuz', txt, re.I))}
    return idx, pages


# ───────────────────────── içindekiler ─────────────────────────
def read_toc(doc, max_pages=8):
    """'BAŞLIK ..... 4-17' biçimindeki içindekiler sayfasından {basılı sayfa: başlık} çıkarır."""
    best = {}
    for i in range(min(max_pages, doc.page_count)):
        lines = [l for l in _all_lines(doc[i]) if l[3]]
        entries = []
        for r, t, _, _ in lines:
            title = re.sub(r'[\s.·…_]+$', '', t).strip()
            if len(title) < 4 or not re.search(r'[A-ZÇĞİÖŞÜ]{3}', title) or re.search(r'\d', title):
                continue
            cy = (r.y0 + r.y1) / 2
            nums = [(r2.x0, t2) for r2, t2, _, _ in lines if abs((r2.y0 + r2.y1) / 2 - cy) < 4 and r2.x0 > r.x1
                    and re.fullmatch(r'\d{1,3}(?:\s*[-–]\s*\d{1,3})?', t2.strip())]
            if not nums:
                continue
            rng = min(nums)[1].replace('–', '-').replace(' ', '')
            a, _, b = rng.partition('-')
            a, b = int(a), int(b or a)
            if 0 < a <= b < a + 60:
                entries.append((a, b, title))
        if len(entries) >= 5 and len(entries) > len(best):
            best = {p: title.upper() for a, b, title in entries for p in range(a, b + 1)}
    return best


def printed_page_offset(doc, samples=25):
    """PDF sayfa no - basılı sayfa no farkı (sayfa altındaki numaradan)."""
    diffs = collections.Counter()
    n = doc.page_count
    for i in range(0, n, max(1, n // samples)):
        page = doc[i]
        H = page.rect.height
        for w in page.get_text('words'):
            if w[1] > H * .9 and re.fullmatch(r'\d{1,3}', w[4]):
                diffs[(i + 1) - int(w[4])] += 1
    return diffs.most_common(1)[0][0] if diffs else 0


# ───────────────────────── sayfa analizi ─────────────────────────
def _all_lines(page):
    """(rect, text, size, horizontal?) — tüm satırlar"""
    out = []
    for b in page.get_text('dict')['blocks']:
        for l in b.get('lines', []):
            t = ''.join(s['text'] for s in l['spans']).strip()
            if t:
                out.append((fitz.Rect(l['bbox']), t, max(s['size'] for s in l['spans']), l['dir'] == (1.0, 0.0)))
    return out


def _title_candidates(page, lines):
    """Kenardaki dikey başlık ve üstteki ortalanmış başlık şeridi adayları."""
    W, H = page.rect.width, page.rect.height
    side, banner = [], []
    for r, t, sz, hor in lines:
        if t.isdigit() or not re.search(r'[A-Za-zÇĞİÖŞÜçğıöşü]{3}', t):
            continue
        if not hor and (r.x1 < 40 or r.x0 > W - 40):
            side.append((r.x0, r.y0, t))
        elif hor and r.y0 < H * .25 and t.upper() == t and len(t) >= 5 \
                and abs((r.x0 + r.x1) / 2 - W / 2) < W * .2 and not re.search(r'\d{3,}', t):
            banner.append((round(sz), t))
    side.sort()
    return [t for _, _, t in side], banner


def _subtitles(lines):
    """'TR <başlık>' biçimindeki alt grup başlıkları."""
    hl = [(r, t) for r, t, _, hor in lines if hor]
    res = []
    for r, t in hl:
        m = re.match(r'^TR\s*(.*)$', t)
        if not m:
            continue
        txt = m.group(1).strip()
        if not txt:
            c = [(abs(r2.y0 - r.y0) + abs(r2.x0 - r.x1) * .2, t2) for r2, t2 in hl
                 if r2.x0 >= r.x1 - 1 and abs(r2.y0 - r.y0) < 6 and t2 != 'TR']
            if c:
                txt = min(c)[1]
        if len(txt) > 3:
            res.append((r, txt))
    return res


def _photo_regions(page, avoid=()):
    """Ürün fotoğrafı alanları. avoid: fiyat rozetleri gibi atlanacak alanlar."""
    W, H = page.rect.width, page.rect.height
    boxes = []
    try:
        infos = page.get_image_info()
    except Exception:
        infos = []
    for im in infos:
        r = fitz.Rect(im['bbox']) & page.rect
        if r.is_empty or r.width < 45 or r.height < 45:
            continue
        if r.width * r.height > .5 * W * H or r.width / r.height > 6 or r.height / r.width > 6:
            continue
        # fiyat rozeti (içinde fiyat yazısı olan küçük görsel) ürün fotoğrafı değildir
        if any(r.contains(fitz.Point((a.x0 + a.x1) / 2, (a.y0 + a.y1) / 2)) and r.width < 135 and r.height < 135
               for a in avoid):
            continue
        boxes.append(r)
    if len(boxes) > 150:          # çok parçalı (mozaik) sayfalar: birleştirmeyi sınırla
        boxes = sorted(boxes, key=lambda r: -r.get_area())[:150]
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


def _price_marks(words, lines):
    """Fiyat adayları: (rect, değer, para birimi)"""
    out = []
    for r, t, _, hor in lines:
        for m in PRICE_RE.finditer(t):
            out.append((r, _num(m.group(1)), CUR_NAME.get(m.group(2), m.group(2))))
        if not PRICE_RE.search(t):
            m = PRICE_PRE_RE.search(t)
            if m:
                out.append((r, _num(m.group(2)), CUR_NAME.get(m.group(1), m.group(1))))
    # ayrı duran sembol + sayı ("$" ve hemen yanında "1,60")
    taken = {tuple(round(v) for v in r) for r, _, _ in out}
    syms = [w for w in words if SYMBOL_RE.match(w[4])]
    nums = [w for w in words if NUM_RE.match(w[4])]
    for s in syms:
        sr = fitz.Rect(s[:4])
        cand = [(_dist(sr, fitz.Rect(n[:4])), n) for n in nums]
        cand = [c for c in cand if c[0] < 28]
        if not cand:
            continue
        n = min(cand, key=lambda c: c[0])[1]
        r = sr | fitz.Rect(n[:4])
        if tuple(round(v) for v in r) in taken:
            continue
        v = _num(n[4])
        if v and v > 0:
            out.append((r, v, CUR_NAME.get(s[4], s[4])))
    return out


def _badges(lines):
    out = []
    for r, t, _, _ in lines:
        u = t.upper().replace('İ', 'I')
        if u in ('SIVA ALTI', 'SIVA ÜSTÜ', 'SIVAALTI', 'SIVAÜSTÜ'):
            out.append((r, 'Sıva Altı' if 'ALTI' in u else 'Sıva Üstü'))
    return out


def _ip_marks(lines):
    out = []
    for r, t, _, _ in lines:
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


def _merge_split_codes(words, sku_re):
    """'400 000 127' gibi parçalanmış kodları tek kelimede birleştirir (400-000-127)."""
    out, i = [], 0
    ws = sorted(words, key=lambda w: (round((w[1] + w[3]) / 2), w[0]))
    while i < len(ws):
        w = ws[i]
        if re.fullmatch(r'\d{2,5}', w[4]):
            grp = [w]
            j = i + 1
            while j < len(ws) and len(grp) < 4 and re.fullmatch(r'\d{2,5}', ws[j][4]) \
                    and abs((ws[j][1] + ws[j][3]) / 2 - (w[1] + w[3]) / 2) < 2 and 0 <= ws[j][0] - grp[-1][2] < 6:
                grp.append(ws[j])
                j += 1
            if len(grp) >= 2:
                for sep in ('-', '.', '/', ''):
                    tok = sep.join(g[4] for g in grp)
                    if sku_re.fullmatch(tok):
                        out.append((grp[0][0], min(g[1] for g in grp), grp[-1][2], max(g[3] for g in grp), tok) + tuple(w[5:]))
                        i = j
                        break
                else:
                    out.append(w)
                    i += 1
                continue
        out.append(w)
        i += 1
    return out


def extract_page(page, pno, sku_re, lines=None):
    words = _merge_split_codes(page.get_text('words'), sku_re)
    lines = lines if lines is not None else _all_lines(page)
    hl = [(r, t) for r, t, _, hor in lines if hor]
    subs = _subtitles(lines)
    prices = _price_marks(words, lines)
    regions = _photo_regions(page, [r for r, _, _ in prices])
    badges = _badges(lines)
    ips = _ip_marks(lines)
    powers = [(r, t) for r, t in hl if POWER_RE.match(t) and len(t) < 40]

    items, seen = [], set()
    for w in words:
        m = sku_re.search(w[4])
        if not m or m.group(0) in seen:
            continue
        seen.add(m.group(0))
        items.append((m.group(0), fitz.Rect(w[:4])))

    # fiyat ataması:
    #  1) aynı satırda sağdaki fiyat (tablo düzeni)
    #  2) aynı ürün fotoğrafına bağlı fiyat (rozet düzeni: fiyat fotoğrafın üstünde/yanında)
    #  3) her fiyat en yakın koda, 4) varyantlar komşu koddan
    reg_of = {sku: (min(regions, key=lambda r: _dist(sr, r)) if regions else None) for sku, sr in items}
    price_reg = []
    for r, v, c in prices:
        if regions:
            pr = min(regions, key=lambda g: _dist(r, g))
            price_reg.append((r, v, c, pr if _dist(r, pr) < 60 else None))
        else:
            price_reg.append((r, v, c, None))
    price_of = {}
    for sku, sr in items:
        cy = (sr.y0 + sr.y1) / 2
        same = [(r.x0 - sr.x1, v, c) for r, v, c in prices if abs((r.y0 + r.y1) / 2 - cy) < 4 and r.x0 > sr.x1 - 2]
        if same:
            _, v, c = min(same, key=lambda x: x[0])
            price_of[sku] = (v, c)
            continue
        g = reg_of[sku]
        if g is not None:
            linked = [(_dist(sr, r), v, c) for r, v, c, pr in price_reg if pr is not None and pr == g]
            if linked:
                _, v, c = min(linked, key=lambda x: x[0])
                price_of[sku] = (v, c)
    rest = [(s, r) for s, r in items if s not in price_of]
    used = {(round(v, 2), c) for v, c in price_of.values()}
    if rest:
        for r, v, c, pr in price_reg:
            if pr is not None and any(reg_of[s] == pr for s, _ in items):
                continue          # bu fiyat başka bir ürünün fotoğrafına ait
            near = min(rest, key=lambda it: _dist(it[1], r))
            if _dist(near[1], r) < 220 and near[0] not in price_of:
                price_of[near[0]] = (v, c)
        for sku, sr in rest:
            if sku in price_of:
                continue
            nb = [(_dist(sr, r2), price_of[s2]) for s2, r2 in items if s2 in price_of and s2 != sku]
            nb = [x for x in nb if x[0] < 120]
            if nb:
                price_of[sku] = min(nb, key=lambda x: x[0])[1]

    out = []
    for sku, sr in items:
        cy = (sr.y0 + sr.y1) / 2
        row = sorted([x for x in words if abs((x[1] + x[3]) / 2 - cy) < 3 and sr.x1 < x[0] < sr.x1 + 130
                      and not sku_re.search(x[4]) and not SYMBOL_RE.match(x[4])], key=lambda x: x[0])
        variant = re.sub(r'\b\d{1,3}(?:[.]\d{3})*,\d{2}\b', '', ' '.join(x[4] for x in row))
        pw = [(sr.y0 - r.y1, t) for r, t in powers if r.y1 <= sr.y0 + 2 and abs(r.x0 - sr.x0) < 120 and sr.y0 - r.y1 < 120]
        power = min(pw)[1] if pw else ''
        lab = [(sr.y0 - r.y1, t, r) for r, t in hl if r.y1 <= sr.y0 + 1 and sr.y0 - r.y1 < 14 and abs(r.x0 - sr.x0) < 12
               and not sku_re.search(t) and not PRICE_RE.search(t) and not POWER_RE.match(t)
               and not SYMBOL_RE.match(t) and not NUM_RE.match(t)]
        label = ''
        if lab:
            _, _, lr = min(lab, key=lambda x: x[0])
            cyl = (lr.y0 + lr.y1) / 2
            parts = sorted([(r, t) for r, t in hl if abs((r.y0 + r.y1) / 2 - cyl) < 2 and r.x0 >= lr.x0 - 3
                            and r.x0 < lr.x0 + 140 and not sku_re.search(t) and not SYMBOL_RE.match(t)],
                           key=lambda x: (round(x[0].x0), -len(x[1])))
            txt, prev = '', None
            for r, t in parts:
                if prev is not None and r.x0 < prev.x1 - 1:
                    continue          # üst üste binen gölge/kontur katmanı
                if prev is not None and r.x0 - prev.x1 > 25:
                    break
                txt += ('' if prev is None or r.x0 - prev.x1 < 1.5 else ' ') + t
                prev = r
            label = txt
        sb = [(sr.y0 - r.y0 + (0 if abs(r.x0 - sr.x0) < 250 else 400), t) for r, t in subs if r.y0 <= sr.y0]
        sub = min(sb)[1] if sb else (subs[0][1] if subs else '')
        reg = reg_of[sku]
        badge = min(badges, key=lambda b: _dist(sr, b[0]))[1] if badges else ''
        ip = min(ips, key=lambda b: _dist(sr, b[0]))[1] if ips else ''
        pv = price_of.get(sku)
        out.append(dict(sku=sku, page=pno, title='', sub=sub, power=_clean(power), label=_clean(label),
                        variant=_clean(variant), price=pv[0] if pv else None, currency=pv[1] if pv else None,
                        region=reg, badge=badge, ip=ip))
    return out


# ───────────────────────── ana fonksiyon ─────────────────────────
def extract_catalog(pdf_path, progress=None, sku_ornek=None):
    """
    Tüm PDF'i işler. progress(i, n, mesaj) ile ilerleme bildirir.
    Dönüş: dict(products=[...], stats={...})
    """
    try:
        doc = fitz.open(pdf_path)
    except Exception as e:
        raise ExtractError(f'PDF açılamadı ({e}). Dosya bozuk ya da şifreli olabilir.')
    n = doc.page_count
    if n == 0:
        raise ExtractError('PDF boş görünüyor.')
    total_chars = sum(len(doc[i].get_text()) for i in range(0, n, max(1, n // 30)))
    if total_chars < 200:
        raise ExtractError('PDF\'te okunabilir yazı yok; sayfalar resim olarak kaydedilmiş (taranmış ya da '
                           'aşırı sıkıştırılmış). Kataloğun orijinal (sıkıştırılmamış) PDF\'ini yükleyin.')
    sku_re = detect_sku_pattern(doc, example=sku_ornek)
    if not sku_re:
        raise ExtractError('Ürün kodu kalıbı bulunamadı. Yükleme formundaki "Örnek ürün kodu" alanına '
                           'katalogdan bir kod yazıp tekrar deneyin.')
    if progress:
        progress(0, n, 'Fiyat listesi aranıyor')
    plist, plist_pages = read_price_list(doc, sku_re)

    # 1. tur: sayfa başlık adayları → katalogda hangi başlık kaynağı tutarlı?
    page_lines, side_by_page, banner_by_page = {}, {}, {}
    side_count, banner_sizes, text_pages = collections.Counter(), collections.Counter(), collections.Counter()
    for i in range(n):
        if i + 1 in plist_pages:
            continue
        lines = _all_lines(doc[i])
        page_lines[i] = lines
        side, banner = _title_candidates(doc[i], lines)
        side_by_page[i], banner_by_page[i] = side, banner
        for t in set(side):
            text_pages[t] += 1
        for sz, t in set(banner):
            banner_sizes[sz] += 1
            text_pages[t] += 1
    common = {t for t, c in text_pages.items() if c > max(8, n * .5)}      # her sayfada tekrar eden logo/üst yazı
    main_size = banner_sizes.most_common(1)[0][0] if banner_sizes else None

    def page_title(i):
        side = [t for t in side_by_page.get(i, []) if t not in common]
        ban = [t for sz, t in banner_by_page.get(i, []) if t not in common and main_size and abs(sz - main_size) <= 1]
        return side[0] if side else '', ban[0] if ban else ''

    titles = {i: page_title(i) for i in page_lines}
    toc = read_toc(doc)
    offset = printed_page_offset(doc) if toc else 0
    side_cov = sum(1 for s, b in titles.values() if s)
    ban_cov = sum(1 for s, b in titles.values() if b)
    use_side = side_cov >= ban_cov

    raw, last_title, cache, empty_pages = [], '', {}, []
    for i in range(n):
        pno = i + 1
        if i not in page_lines:
            continue
        if progress and i % 3 == 0:
            progress(i, n, f'Sayfa {pno}/{n} işleniyor')
        if sum(len(l[1]) for l in page_lines[i]) < 120 and len(doc[i].get_images()) > 0:
            empty_pages.append(pno)
        page = doc[i]
        s, b = titles[i]
        t = toc.get(pno - offset) or ((s or b) if use_side else (b or s))
        if t:
            last_title = t
        items = extract_page(page, pno, sku_re, page_lines[i])
        for it in items:
            it['title'] = last_title
            if it['region'] is not None:
                key = (pno, tuple(round(v) for v in it['region']))
                if key not in cache:
                    try:
                        cache[key] = _render(page, it['region'])
                    except Exception:
                        cache[key] = None
                it['image_jpeg'] = cache[key]
            else:
                it['image_jpeg'] = None
        raw += items
    if not raw:
        raise ExtractError('Kod kalıbı bulundu ama ürün sayfası çıkarılamadı. Örnek ürün kodu ile tekrar deneyin.')

    shifts = collections.Counter(p['page'] - plist[p['sku']]['page'] for p in raw
                                 if p['sku'] in plist and plist[p['sku']]['page'])
    shift = shifts.most_common(1)[0][0] if shifts else 0

    products, seen = [], set()
    for p in raw:
        if p['sku'] in seen:
            continue
        seen.add(p['sku'])
        power = p['power'] if not re.search(r'\d\s*W\b', p['variant'] + ' ' + p['label']) else ''
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
        if not p['image_jpeg']:
            uyari.append('Görsel bulunamadı')
        ad = ' '.join(x for x in [p['sub'], p['label'], power, p['variant']] if x) or p['title'].title()
        products.append(dict(sku=p['sku'], ad=ad, katalog_baslik=p['title'].strip(), alt_grup=p['sub'] or p['label'],
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
                 urun=len(products), gorsel=len([v for v in cache.values() if v]), sku_kalibi=sku_re.pattern,
                 yazisiz_sayfalar=empty_pages,
                 baslik_kaynagi=('içindekiler + ' if toc else '') + ('kenar' if use_side else 'üst şerit'))
    if progress:
        progress(n, n, 'Tamamlandı')
    return dict(products=products, stats=stats)


if __name__ == '__main__':
    import sys, time
    t = time.time()
    r = extract_catalog(sys.argv[1], lambda i, n, m: print(f'\r{m}      ', end='', flush=True),
                        sys.argv[2] if len(sys.argv) > 2 else None)
    print()
    print(r['stats'], f'{time.time()-t:.1f}s')
    print(collections.Counter(u for p in r['products'] for u in p['uyari']))
    print(collections.Counter(p['katalog_baslik'] for p in r['products']).most_common(40))

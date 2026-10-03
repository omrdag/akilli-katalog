"""
Ortak kategori sistemi.
Her markanın katalog başlıkları farklıdır; burada ortak bir ana grup / kategori
düzenine ve montaj tipine (Sıva Altı, Sıva Üstü, Ray, Duvar) eşlenir.
Kurallar kategori_kurallari.json dosyasındadır, kod değiştirmeden düzenlenebilir.
"""
import json
import os
import re

RULES_PATH = os.path.join(os.path.dirname(__file__), 'kategori_kurallari.json')


def load_rules():
    with open(RULES_PATH, encoding='utf-8') as f:
        return json.load(f)


def norm(s):
    """Türkçe uyumlu küçük harf + tek boşluk."""
    return re.sub(r'\s+', ' ', (s or '').replace('I', 'ı').replace('İ', 'i').lower()).strip()


def classify(p, rules=None):
    """p: dict(katalog_baslik, alt_grup, ad, montaj_rozeti). Dönüş: (ana_grup, kategori, [montaj])"""
    rules = rules or load_rules()
    raw = (p.get('katalog_baslik') or '').strip()
    raw = rules.get('baslik_duzeltme', {}).get(raw, raw)
    if not raw and not p.get('ad'):
        grp = ('Kontrol Edilecek', 'Sayfada bulunamayanlar')
    else:
        grp = tuple(rules['baslik'].get(raw, ('Diğer', raw.title() if raw else 'Kategorisiz')))
    sub = norm(p.get('alt_grup'))
    for r in rules.get('alt_grup', []):
        if r.get('haric_baslik_baslangic') and raw.startswith(r['haric_baslik_baslangic']):
            continue
        if re.search(r['desen'], sub):
            grp = (r['grup'], r['kategori'])
            break
    text = norm((p.get('ad') or '') + ' ' + (p.get('alt_grup') or ''))
    montaj = [r['tip'] for r in rules.get('montaj', []) if re.search(r['desen'], text)]
    if not montaj and p.get('montaj_rozeti'):
        montaj = [p['montaj_rozeti']]
    if not montaj:
        for r in rules.get('montaj_varsayilan', []):
            if 'kategori_baslangic' in r and grp[1].startswith(r['kategori_baslangic']):
                montaj = [r['tip']]
                break
            if 'desen' in r and re.search(r['desen'], text):
                montaj = [r['tip']]
                break
    return grp[0], grp[1], montaj


# ── Filtre özellikleri ──
RENKLER = {
    'beyaz': 'Beyaz', 'siyah': 'Siyah', 'füme': 'Füme', 'gri': 'Gri', 'krom': 'Krom',
    'saten': 'Saten', 'altın': 'Altın', 'gold': 'Altın', 'bakır': 'Bakır', 'gül': 'Gül',
    'bronz': 'Bronz', 'gümüş': 'Gümüş', 'antrasit': 'Antrasit', 'kahverengi': 'Kahverengi',
}
IŞIK_RENKLERİ = {'mavi': 'Mavi', 'yeşil': 'Yeşil', 'kırmızı': 'Kırmızı', 'sarı': 'Sarı', 'amber': 'Amber', 'pembe': 'Pembe'}


def attributes(p):
    """Ürün adından/özelliğinden filtrelenebilir özellikler: güç (W), ışık rengi, gövde rengi, IP."""
    text = ' '.join([p.get('ad') or '', p.get('ozellik') or ''])
    low = norm(text)
    watt = None
    m = re.search(r'(?<![\d.,])(\d{1,4}(?:[.,]\d+)?)\s?W\b', text)
    if m:
        try:
            watt = float(m.group(1).replace(',', '.'))
        except ValueError:
            watt = None
    cct = []
    for k in re.findall(r'\b(\d{4})\s?K\b', text):
        if 1800 <= int(k) <= 10000 and f'{k}K' not in cct:
            cct.append(f'{k}K')
    for tag in ('CCT', 'RGBW', 'RGB'):
        if re.search(rf'\b{tag}\b', text) and tag not in cct:
            cct.append(tag)
    for w, lab in IŞIK_RENKLERİ.items():
        if re.search(rf'\b{w}\b', low) and lab not in cct:
            cct.append(lab)
    renk = []
    for w, lab in RENKLER.items():
        if re.search(rf'\b{w}\b', low) and lab not in renk:
            renk.append(lab)
    ip = (p.get('ip') or '').upper()
    m = re.search(r'\bIP\s?(\d{2})\b', text)
    if m:
        ip = 'IP' + m.group(1)
    return dict(watt=watt, cct=','.join(cct), renk=','.join(renk), ip=ip)

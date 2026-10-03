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

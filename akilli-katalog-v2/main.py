"""
Akıllı Katalog — Bağımsız PDF katalog çıkarma uygulaması.
Ana ByLamp sistemine bağlı DEĞİLDİR. Kendi veritabanı (SQLite) ile çalışır.

Akış:
1. Marka adı + PDF katalog yüklenir (tüm PDF tek seferde işlenir, arka planda)
2. Ürün kodu, ad, fiyat, görsel, katalog başlığı ve sayfa çıkarılır
3. Ürünler ortak ana grup / kategori ve montaj tipine (Sıva Altı, Sıva Üstü...) ayrılır
4. Katalog sayfasında listelenir, aranır, filtrelenir; uyarılı ürünler işaretlenir
5. Seçili ürünlerden Excel fiyat teklifi oluşturulabilir
"""
import os
import io
import gzip
import json
import base64
import sqlite3
import threading
import uuid
import re
from datetime import datetime
from flask import (Flask, render_template, request, redirect, url_for,
                   flash, jsonify, send_file, g, abort, Response)
from werkzeug.utils import secure_filename

import kategori

app = Flask(__name__)
app.secret_key = os.environ.get('SESSION_SECRET', 'akilli-katalog-dev-key')
app.config['MAX_CONTENT_LENGTH'] = 200 * 1024 * 1024  # 200 MB

BASE = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE, 'uploads')
os.makedirs(UPLOAD_DIR, exist_ok=True)
DB_PATH = os.environ.get('DB_PATH', os.path.join(BASE, 'akilli_katalog.db'))
SEED_DIR = os.path.join(BASE, 'data')

COLUMNS = {
    'marka': 'TEXT', 'currency': 'TEXT', 'ana_grup': 'TEXT', 'alt_grup': 'TEXT',
    'katalog_baslik': 'TEXT', 'montaj': 'TEXT', 'ozellik': 'TEXT', 'uyari': 'TEXT',
    'image_blob': 'BLOB', 'watt': 'REAL', 'cct': 'TEXT', 'renk': 'TEXT', 'ip': 'TEXT', 'attr_v': 'INTEGER',
}
ATTR_VERSION = 1
PER_PAGE_OPTIONS = (48, 96, 192)


# ── Veritabanı ──
def connect():
    db = sqlite3.connect(DB_PATH, timeout=30)
    db.row_factory = sqlite3.Row
    return db


def get_db():
    if 'db' not in g:
        g.db = connect()
    return g.db


@app.teardown_appcontext
def close_db(exception):
    db = g.pop('db', None)
    if db is not None:
        db.close()


def init_db():
    db = connect()
    db.execute('''
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sku TEXT,
            product_name TEXT,
            price REAL DEFAULT 0,
            category TEXT,
            image_data TEXT,
            source_pdf TEXT,
            source_page INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    have = {r['name'] for r in db.execute('PRAGMA table_info(products)')}
    for col, typ in COLUMNS.items():
        if col not in have:
            db.execute(f'ALTER TABLE products ADD COLUMN {col} {typ}')
    db.execute('CREATE INDEX IF NOT EXISTS ix_products_sku ON products(sku)')
    db.execute('CREATE INDEX IF NOT EXISTS ix_products_cat ON products(category)')
    db.execute('CREATE INDEX IF NOT EXISTS ix_products_marka ON products(marka)')
    db.execute('''CREATE TABLE IF NOT EXISTS catalogs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, marka TEXT, dosya TEXT, sayfa INTEGER,
        urun INTEGER, uyari INTEGER, gorsel INTEGER, yuklenme TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    db.commit()
    seed_if_empty(db)
    backfill_attributes(db)
    db.close()


def backfill_attributes(db):
    """Eski kayıtlar için filtre özelliklerini (W, K, renk, IP) hesaplar."""
    rows = db.execute('SELECT id, product_name, ozellik, ip FROM products WHERE COALESCE(attr_v,0) < ?',
                      (ATTR_VERSION,)).fetchall()
    for r in rows:
        a = kategori.attributes(dict(ad=r['product_name'], ozellik=r['ozellik'], ip=r['ip']))
        db.execute('UPDATE products SET watt=?, cct=?, renk=?, ip=?, attr_v=? WHERE id=?',
                   (a['watt'], a['cct'], a['renk'], a['ip'], ATTR_VERSION, r['id']))
    db.commit()


def save_products(db, marka, source_pdf, products, replace=True, sayfa=None):
    """Çıkarılan ürünleri kategorize edip kaydeder."""
    rules = kategori.load_rules()
    if replace:
        db.execute('DELETE FROM products WHERE marka = ?', (marka,))
        db.execute('DELETE FROM catalogs WHERE marka = ?', (marka,))
    rows = []
    for p in products:
        grup, kat, montaj = kategori.classify(p, rules)
        img = p.get('image_jpeg')
        if img is None and p.get('image_b64'):
            img = base64.b64decode(p['image_b64'])
        a = kategori.attributes(p)
        rows.append((p['sku'], p.get('ad', ''), p.get('fiyat'), kat, source_pdf, p.get('sayfa'),
                     marka, p.get('para_birimi') or '', grup, p.get('alt_grup', ''),
                     p.get('katalog_baslik', ''), ','.join(montaj), p.get('ozellik', ''),
                     '|'.join(p.get('uyari', [])), img, a['watt'], a['cct'], a['renk'], a['ip'], ATTR_VERSION))
    db.executemany('''INSERT INTO products (sku, product_name, price, category, source_pdf, source_page,
                      marka, currency, ana_grup, alt_grup, katalog_baslik, montaj, ozellik, uyari, image_blob,
                      watt, cct, renk, ip, attr_v)
                      VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''', rows)
    db.execute('INSERT INTO catalogs (marka, dosya, sayfa, urun, uyari, gorsel) VALUES (?,?,?,?,?,?)',
               (marka, source_pdf, sayfa, len(rows), sum(1 for p in products if p.get('uyari')),
                sum(1 for r in rows if r[14])))
    db.commit()
    return len(rows)


def seed_if_empty(db):
    """data/*.json.gz içindeki hazır kataloğu (ör. ACK 2026) boş veritabanına yükler."""
    if db.execute('SELECT COUNT(*) FROM products').fetchone()[0]:
        return
    if not os.path.isdir(SEED_DIR):
        return
    for fn in sorted(os.listdir(SEED_DIR)):
        if fn.endswith('.json.gz'):
            with gzip.open(os.path.join(SEED_DIR, fn), 'rt', encoding='utf-8') as f:
                data = json.load(f)
            save_products(db, data['marka'], data['kaynak'], data['products'], replace=True,
                          sayfa=data.get('stats', {}).get('sayfa'))


# ── Arka plan işleri (PDF çıkarma) ──
JOBS = {}


def run_job(job_id, pdf_path, marka, filename):
    from catalog_extract import extract_catalog
    job = JOBS[job_id]

    def progress(i, n, msg):
        job.update(i=i, n=n, msg=msg)

    try:
        result = extract_catalog(pdf_path, progress)
        db = connect()
        saved = save_products(db, marka, filename, result['products'], replace=True,
                              sayfa=result['stats'].get('sayfa'))
        db.close()
        uyari = sum(1 for p in result['products'] if p['uyari'])
        job.update(state='done', saved=saved, uyari=uyari, stats=result['stats'],
                   msg=f'{saved} ürün kaydedildi ({uyari} uyarılı).')
    except Exception as e:  # noqa
        job.update(state='error', msg=f'PDF işlenirken hata: {e}')


# ── Filtreleme ──
# Çoklu seçilebilen filtreler: URL'de tekrar eden parametreler (?marka=ACK&marka=CATA)
LIST_FACETS = {          # parametre: (sütun, virgüllü liste mi?)
    'marka': ('marka', False),
    'grup': ('ana_grup', False),
    'kat': ('category', False),
    'montaj': ('montaj', True),
    'cct': ('cct', True),
    'renk': ('renk', True),
    'ip': ('ip', False),
}
SORTS = {
    'onerilen': ('Önerilen', "ana_grup, category, alt_grup, id"),
    'fiyat_artan': ('Fiyat (artan)', "price IS NULL, price ASC"),
    'fiyat_azalan': ('Fiyat (azalan)', "price IS NULL, price DESC"),
    'guc_artan': ('Güç (artan)', "watt IS NULL, watt ASC"),
    'guc_azalan': ('Güç (azalan)', "watt IS NULL, watt DESC"),
    'sku': ('Ürün kodu', "sku ASC"),
    'yeni': ('Son eklenen', "id DESC"),
}
FACET_LABELS = {'marka': 'Marka', 'grup': 'Ana grup', 'kat': 'Kategori', 'montaj': 'Montaj tipi',
                'cct': 'Işık rengi', 'renk': 'Gövde rengi', 'ip': 'Koruma (IP)'}


def read_filters(req):
    f = {k: [v for v in req.args.getlist(k) if v.strip()] for k in LIST_FACETS}
    for k in ('q', 'wmin', 'wmax', 'pmin', 'pmax', 'uyari', 'gorsel', 'sort', 'gorunum'):
        f[k] = (req.args.get(k) or '').strip()
    f['sort'] = f['sort'] if f['sort'] in SORTS else 'onerilen'
    f['gorunum'] = 'liste' if f['gorunum'] == 'liste' else 'izgara'
    f['adet'] = req.args.get('adet', 48, type=int)
    if f['adet'] not in PER_PAGE_OPTIONS:
        f['adet'] = 48
    return f


def build_filter(f, exclude=None):
    where, params = ['1=1'], []
    if f.get('q'):
        for term in f['q'].split():
            where.append('(sku LIKE ? OR product_name LIKE ? OR ozellik LIKE ? OR category LIKE ? '
                         'OR alt_grup LIKE ? OR marka LIKE ?)')
            params += [f'%{term}%'] * 6
    # Kategori ağacı: ana grup ve kategori seçimleri birbirini genişletir (VEYA)
    if exclude != 'kat' and (f.get('grup') or f.get('kat')):
        parts = []
        if f.get('grup'):
            parts.append(f"ana_grup IN ({','.join('?' * len(f['grup']))})")
            params += f['grup']
        if f.get('kat'):
            parts.append(f"category IN ({','.join('?' * len(f['kat']))})")
            params += f['kat']
        where.append('(' + ' OR '.join(parts) + ')')
    for key, (col, is_list) in LIST_FACETS.items():
        vals = f.get(key) or []
        if key in ('grup', 'kat') or key == exclude or not vals:
            continue
        if is_list:
            where.append('(' + ' OR '.join([f"(',' || COALESCE({col},'') || ',') LIKE ?"] * len(vals)) + ')')
            params += [f'%,{v},%' for v in vals]
        else:
            where.append(f"{col} IN ({','.join('?' * len(vals))})")
            params += vals
    for key, col, op in (('wmin', 'watt', '>='), ('wmax', 'watt', '<='), ('pmin', 'price', '>='), ('pmax', 'price', '<=')):
        if exclude in ('watt', 'price') and col == exclude:
            continue
        try:
            v = float(f.get(key, '').replace(',', '.'))
        except ValueError:
            continue
        where.append(f'{col} {op} ?')
        params.append(v)
    if f.get('uyari') == '1':
        where.append("COALESCE(uyari,'') <> ''")
    elif f.get('uyari') == '0':
        where.append("COALESCE(uyari,'') = ''")
    if f.get('gorsel') == '1':
        where.append('(image_blob IS NOT NULL OR image_data IS NOT NULL)')
    return ' AND '.join(where), params


def facet_counts(db, f, key):
    col, is_list = LIST_FACETS[key]
    w, p = build_filter(f, exclude=key)
    counts = {}
    if is_list:
        for (val,) in db.execute(f"SELECT {col} FROM products WHERE {w} AND COALESCE({col},'')<>''", p):
            for v in val.split(','):
                counts[v] = counts.get(v, 0) + 1
    else:
        for r in db.execute(f"SELECT {col}, COUNT(*) FROM products WHERE {w} AND COALESCE({col},'')<>'' "
                            f"GROUP BY {col}", p):
            counts[r[0]] = r[1]
    return counts


def sort_values(key, counts):
    if key == 'cct':
        def k(v):
            return (0, int(v[:-1])) if re.fullmatch(r'\d{4}K', v) else (1, v)
        return sorted(counts, key=k)
    if key == 'ip':
        return sorted(counts)
    if key == 'montaj':
        order = ['Sıva Altı', 'Sıva Üstü', 'Ray Tipi', 'Duvar Tipi']
        return sorted(counts, key=lambda v: (order.index(v) if v in order else 9, v))
    return sorted(counts, key=lambda v: (-counts[v], v))


def url_without(f, key, value=None):
    """Aktif filtre çipinden tek bir değeri kaldıran URL."""
    params = {}
    for k in LIST_FACETS:
        vals = [v for v in f[k] if not (k == key and (value is None or v == value))]
        if vals:
            params[k] = vals
    for k in ('q', 'wmin', 'wmax', 'pmin', 'pmax', 'uyari', 'gorsel', 'sort', 'gorunum'):
        if f.get(k) and k != key and not (key == 'watt' and k in ('wmin', 'wmax')) \
                and not (key == 'price' and k in ('pmin', 'pmax')):
            params[k] = f[k]
    if f['adet'] != 48:
        params['adet'] = f['adet']
    return url_for('catalog', **params)


# ── Sayfalar ──
@app.route('/')
def panel():
    """Genel durum paneli."""
    db = get_db()
    rules = kategori.load_rules()
    k = db.execute('''SELECT COUNT(*) n, COUNT(DISTINCT marka) m, COUNT(DISTINCT category) c,
                         SUM(CASE WHEN COALESCE(uyari,'')<>'' THEN 1 ELSE 0 END) w,
                         SUM(CASE WHEN image_blob IS NOT NULL OR image_data IS NOT NULL THEN 1 ELSE 0 END) g,
                         SUM(CASE WHEN price IS NOT NULL AND price > 0 THEN 1 ELSE 0 END) f
                  FROM products''').fetchone()
    brands = db.execute('''SELECT marka, COUNT(*) n, COUNT(DISTINCT category) c,
                              SUM(CASE WHEN COALESCE(uyari,'')<>'' THEN 1 ELSE 0 END) w,
                              MIN(CASE WHEN price>0 THEN price END) pmin, MAX(price) pmax, MAX(currency) cur
                       FROM products GROUP BY marka ORDER BY n DESC''').fetchall()
    order = {g: i for i, g in enumerate(rules['grup_sirasi'])}
    groups = sorted(db.execute('''SELECT ana_grup g, COUNT(*) n, COUNT(DISTINCT category) c FROM products
                                  GROUP BY ana_grup''').fetchall(), key=lambda r: order.get(r['g'], 99))
    top_cats = db.execute('''SELECT category, ana_grup, COUNT(*) n FROM products GROUP BY category
                              ORDER BY n DESC LIMIT 10''').fetchall()
    montaj = facet_counts(db, {kk: [] for kk in LIST_FACETS}, 'montaj')
    montaj = [(m, rules['montaj_etiket'].get(m, m), montaj[m]) for m in sort_values('montaj', montaj)]
    ips = facet_counts(db, {kk: [] for kk in LIST_FACETS}, 'ip')
    warns = {}
    for (u,) in db.execute("SELECT uyari FROM products WHERE COALESCE(uyari,'')<>''"):
        for x in u.split('|'):
            warns[x] = warns.get(x, 0) + 1
    catalogs = db.execute('SELECT * FROM catalogs ORDER BY yuklenme DESC LIMIT 10').fetchall()
    return render_template('panel.html', k=k, brands=brands, groups=groups, top_cats=top_cats, montaj=montaj,
                           ips=sorted(ips.items()), warns=sorted(warns.items(), key=lambda x: -x[1]),
                           catalogs=catalogs)


@app.route('/katalog')
def catalog():
    db = get_db()
    f = read_filters(request)
    page = max(1, request.args.get('sayfa', 1, type=int))
    where, params = build_filter(f)
    count = db.execute(f'SELECT COUNT(*) FROM products WHERE {where}', params).fetchone()[0]
    pages = max(1, (count + f['adet'] - 1) // f['adet'])
    page = min(page, pages)
    products = db.execute(f'''SELECT id, sku, product_name, price, currency, category, ana_grup, alt_grup,
                              montaj, uyari, source_page, marka, watt, cct, renk, ip,
                              (image_blob IS NOT NULL OR image_data IS NOT NULL) AS has_img
                              FROM products WHERE {where}
                              ORDER BY {SORTS[f['sort']][1]} LIMIT ? OFFSET ?''',
                          params + [f['adet'], (page - 1) * f['adet']]).fetchall()

    rules = kategori.load_rules()
    facets = {}
    for key in LIST_FACETS:
        if key in ('kat', 'grup'):
            continue
        c = facet_counts(db, f, key)
        for v in f[key]:
            c.setdefault(v, 0)
        facets[key] = [(v, rules['montaj_etiket'].get(v, v) if key == 'montaj' else v, c[v])
                       for v in sort_values(key, c)]
    # kategori ağacı: ana grup > kategori
    kc = facet_counts(db, f, 'kat')
    for v in f['kat']:
        kc.setdefault(v, 0)
    gw, gp = build_filter(f, exclude='kat')
    tree = {}
    for r in db.execute(f'SELECT DISTINCT ana_grup, category FROM products WHERE {gw}', gp):
        tree.setdefault(r[0] or 'Diğer', set()).add(r[1])
    for v in f['kat']:
        g = db.execute('SELECT ana_grup FROM products WHERE category=? LIMIT 1', (v,)).fetchone()
        tree.setdefault(g[0] if g else 'Diğer', set()).add(v)
    order = {g: i for i, g in enumerate(rules['grup_sirasi'])}
    tree = [(g, sorted(cs, key=lambda c: c or ''), sum(kc.get(c, 0) for c in cs))
            for g, cs in sorted(tree.items(), key=lambda kv: order.get(kv[0], 99))]
    ww, wp = build_filter(f, exclude='watt')
    wr = db.execute(f'SELECT MIN(watt), MAX(watt) FROM products WHERE {ww} AND watt IS NOT NULL', wp).fetchone()
    pw, pp = build_filter(f, exclude='price')
    pr = db.execute(f'SELECT MIN(price), MAX(price), MAX(currency) FROM products WHERE {pw} AND price > 0', pp).fetchone()

    chips = []
    for key in LIST_FACETS:
        for v in f[key]:
            label = rules['montaj_etiket'].get(v, v) if key == 'montaj' else v
            chips.append((FACET_LABELS[key], label, url_without(f, key, v)))
    if f['q']:
        chips.append(('Arama', f['q'], url_without(f, 'q')))
    if f['wmin'] or f['wmax']:
        chips.append(('Güç', f"{f['wmin'] or '0'}–{f['wmax'] or '∞'} W", url_without(f, 'watt')))
    if f['pmin'] or f['pmax']:
        chips.append(('Fiyat', f"{f['pmin'] or '0'}–{f['pmax'] or '∞'}", url_without(f, 'price')))
    if f['uyari']:
        chips.append(('Durum', 'Uyarılı' if f['uyari'] == '1' else 'Sorunsuz', url_without(f, 'uyari')))
    if f['gorsel']:
        chips.append(('Durum', 'Görselli', url_without(f, 'gorsel')))
    total = db.execute('SELECT COUNT(*) FROM products').fetchone()[0]

    def page_url(n=None, **over):
        params = {k: v for k, v in f.items() if v and k != 'adet'}
        if f['adet'] != 48:
            params['adet'] = f['adet']
        params.update(over)
        if n:
            params['sayfa'] = n
        return url_for('catalog', **{k: v for k, v in params.items() if v})

    return render_template('katalog.html', products=products, f=f, count=count, total=total, page=page,
                           pages=pages, facets=facets, tree=tree, kc=kc, wr=wr, pr=pr, chips=chips,
                           sorts=SORTS, labels=FACET_LABELS, page_url=page_url, per_page=PER_PAGE_OPTIONS)


@app.route('/img/<int:pid>')
def product_image(pid):
    row = get_db().execute('SELECT image_blob, image_data FROM products WHERE id = ?', (pid,)).fetchone()
    if not row:
        abort(404)
    if row['image_blob']:
        data, mime = row['image_blob'], 'image/jpeg'
    elif row['image_data'] and row['image_data'].startswith('data:image'):
        head, b64 = row['image_data'].split(',', 1)
        data, mime = base64.b64decode(b64), head[5:].split(';')[0]
    else:
        abort(404)
    resp = Response(data, mimetype=mime)
    resp.headers['Cache-Control'] = 'public, max-age=86400'
    return resp


@app.route('/upload', methods=['GET', 'POST'])
def upload():
    """PDF katalog yükleme — tüm PDF arka planda işlenir."""
    if request.method == 'POST':
        file = request.files.get('pdf')
        marka = (request.form.get('marka') or '').strip()
        if not file or not file.filename.lower().endswith('.pdf'):
            flash('Lütfen bir PDF dosyası seçin.', 'error')
            return redirect(url_for('upload'))
        if not marka:
            flash('Lütfen marka adını girin.', 'error')
            return redirect(url_for('upload'))
        filename = secure_filename(file.filename) or 'katalog.pdf'
        pdf_path = os.path.join(UPLOAD_DIR, filename)
        file.save(pdf_path)
        job_id = uuid.uuid4().hex[:10]
        JOBS[job_id] = dict(state='running', i=0, n=0, msg='Başlatılıyor', marka=marka, file=filename)
        threading.Thread(target=run_job, args=(job_id, pdf_path, marka, filename), daemon=True).start()
        return redirect(url_for('job_page', job_id=job_id))
    markalar = [r[0] for r in get_db().execute('SELECT DISTINCT marka FROM products WHERE marka IS NOT NULL ORDER BY marka')]
    return render_template('upload.html', markalar=markalar)


@app.route('/is/<job_id>')
def job_page(job_id):
    if job_id not in JOBS:
        flash('İşlem bulunamadı (sunucu yeniden başlamış olabilir).', 'error')
        return redirect(url_for('catalog'))
    return render_template('job.html', job_id=job_id, job=JOBS[job_id])


@app.route('/is/<job_id>/durum')
def job_status(job_id):
    job = JOBS.get(job_id)
    if not job:
        return jsonify(state='missing'), 404
    return jsonify({k: v for k, v in job.items() if k != 'stats'})


@app.route('/delete-all', methods=['POST'])
def delete_all():
    """Ürünleri temizle (marka verilirse sadece o markayı)."""
    marka = (request.form.get('marka') or '').strip()
    db = get_db()
    if marka:
        db.execute('DELETE FROM products WHERE marka = ?', (marka,))
        db.execute('DELETE FROM catalogs WHERE marka = ?', (marka,))
        msg = f'{marka} ürünleri silindi.'
    else:
        db.execute('DELETE FROM products')
        db.execute('DELETE FROM catalogs')
        msg = 'Tüm ürünler silindi.'
    db.commit()
    flash(msg, 'success')
    return redirect(url_for('panel'))


@app.route('/yeniden-kategorize', methods=['POST'])
def recategorize():
    """kategori_kurallari.json değiştiyse mevcut ürünlere yeniden uygular."""
    db = get_db()
    rules = kategori.load_rules()
    rows = db.execute('SELECT id, katalog_baslik, alt_grup, product_name, montaj FROM products').fetchall()
    for r in rows:
        eski = (r['montaj'] or '').split(',')
        rozet = next((m for m in eski if m in ('Sıva Altı', 'Sıva Üstü')), '')
        grup, kat, mont = kategori.classify(dict(katalog_baslik=r['katalog_baslik'], alt_grup=r['alt_grup'],
                                                 ad=r['product_name'], montaj_rozeti=rozet), rules)
        db.execute('UPDATE products SET ana_grup=?, category=?, montaj=? WHERE id=?',
                   (grup, kat, ','.join(mont), r['id']))
    db.commit()
    flash(f'{len(rows)} ürün yeniden kategorize edildi.', 'success')
    return redirect(url_for('panel'))


@app.route('/export-quote', methods=['POST'])
def export_quote():
    """Seçili ürünlerden Excel fiyat teklifi oluştur."""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.drawing.image import Image as XLImage

    ids = request.form.get('ids', '')
    customer = (request.form.get('customer_name') or '').strip()
    id_list = [int(x) for x in ids.split(',') if x.strip().isdigit()]
    if not id_list:
        flash('Teklif için ürün seçilmedi.', 'error')
        return redirect(url_for('catalog'))

    db = get_db()
    placeholders = ','.join(['?'] * len(id_list))
    rows = db.execute(f'SELECT * FROM products WHERE id IN ({placeholders})', id_list).fetchall()

    wb = Workbook()
    ws = wb.active
    ws.title = "Fiyat Teklifi"
    ws.sheet_view.showGridLines = False
    for i, w in enumerate([5, 16, 48, 16, 8, 16]):
        ws.column_dimensions[chr(65 + i)].width = w

    DARK = '111111'
    fill = PatternFill(start_color=DARK, end_color=DARK, fill_type='solid')
    for col in range(1, 7):
        ws.cell(row=1, column=col).fill = fill
        ws.cell(row=2, column=col).fill = fill
    ws.merge_cells('A1:D2')
    h = ws.cell(row=1, column=1, value='AKILLI KATALOG — FİYAT TEKLİFİ')
    h.font = Font(size=14, bold=True, color='FFFFFF')
    h.alignment = Alignment(horizontal='left', vertical='center', indent=1)
    ws.merge_cells('E1:F2')
    d = ws.cell(row=1, column=5, value=datetime.now().strftime('%d.%m.%Y'))
    d.font = Font(size=11, color='FFFFFF')
    d.alignment = Alignment(horizontal='right', vertical='center', indent=1)
    if customer:
        ws.cell(row=3, column=1, value=f'Sayın: {customer}').font = Font(size=11, bold=True)

    for col, t in enumerate(['#', 'GÖRSEL', 'ÜRÜN', 'SKU', 'ADET', 'FİYAT'], 1):
        c = ws.cell(row=5, column=col, value=t)
        c.font = Font(size=10, bold=True, color='888888')
        c.border = Border(top=Side(style='medium', color=DARK), bottom=Side(style='medium', color=DARK))

    fmt_for = {'USD': '#,##0.00 "$"', 'EUR': '#,##0.00 "€"'}
    r, totals = 6, {}
    for idx, row in enumerate(rows, 1):
        ws.row_dimensions[r].height = 54
        price = float(row['price'] or 0)
        cur = row['currency'] or 'TL'
        totals[cur] = totals.get(cur, 0) + price
        ws.cell(row=r, column=1, value=idx)
        img = row['image_blob']
        if not img and row['image_data'] and row['image_data'].startswith('data:image'):
            img = base64.b64decode(row['image_data'].split(',', 1)[1])
        if img:
            try:
                xi = XLImage(io.BytesIO(img))
                xi.width, xi.height = 60, 60
                ws.add_image(xi, f'B{r}')
            except Exception:
                pass
        ws.cell(row=r, column=3, value=row['product_name'] or '').alignment = Alignment(wrap_text=True, vertical='center')
        ws.cell(row=r, column=4, value=row['sku'] or '')
        ws.cell(row=r, column=5, value=1).alignment = Alignment(horizontal='center')
        pc = ws.cell(row=r, column=6, value=price)
        pc.number_format = fmt_for.get(cur, '#,##0.00 "₺"')
        r += 1

    r += 1
    for cur, total in totals.items():
        tl = ws.cell(row=r, column=5, value='TOPLAM')
        tl.font = Font(bold=True, color='FFFFFF'); tl.fill = fill
        tv = ws.cell(row=r, column=6, value=total)
        tv.font = Font(bold=True, color='FFFFFF'); tv.fill = fill
        tv.number_format = fmt_for.get(cur, '#,##0.00 "₺"')
        r += 1

    output = io.BytesIO()
    wb.save(output)
    output.seek(0)
    fname = f"Teklif_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
    return send_file(output, mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                     as_attachment=True, download_name=fname)


@app.route('/sistem-kontrol')
def sistem_kontrol():
    """Teşhis: kütüphaneler ve veritabanı durumu."""
    checks = {}
    from importlib.metadata import version
    for pkg in ('PyMuPDF', 'Pillow', 'openpyxl', 'Flask'):
        try:
            checks[pkg] = f'OK ({version(pkg)})'
        except Exception as e:
            checks[pkg] = f'HATA: {e}'
    db = get_db()
    checks['urun_sayisi'] = db.execute('SELECT COUNT(*) FROM products').fetchone()[0]
    checks['markalar'] = ', '.join(f"{r[0]} ({r[1]})" for r in db.execute(
        'SELECT marka, COUNT(*) FROM products GROUP BY marka')) or '-'
    checks['gorselli_urun'] = db.execute('SELECT COUNT(*) FROM products WHERE image_blob IS NOT NULL OR image_data IS NOT NULL').fetchone()[0]
    checks['uyarili_urun'] = db.execute("SELECT COUNT(*) FROM products WHERE COALESCE(uyari,'')<>''").fetchone()[0]
    checks['yuklenen_pdfler'] = ', '.join(f for f in os.listdir(UPLOAD_DIR) if f.lower().endswith('.pdf')) or 'yok'
    checks['veritabani'] = DB_PATH
    markalar = [r[0] for r in db.execute('SELECT DISTINCT marka FROM products WHERE marka IS NOT NULL ORDER BY marka')]
    return render_template('sistem.html', checks=checks, markalar=markalar)


init_db()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=True)

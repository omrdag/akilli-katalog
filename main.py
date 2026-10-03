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
PER_PAGE = 120

COLUMNS = {
    'marka': 'TEXT', 'currency': 'TEXT', 'ana_grup': 'TEXT', 'alt_grup': 'TEXT',
    'katalog_baslik': 'TEXT', 'montaj': 'TEXT', 'ozellik': 'TEXT', 'uyari': 'TEXT',
    'image_blob': 'BLOB',
}


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
    db.commit()
    seed_if_empty(db)
    db.close()


def save_products(db, marka, source_pdf, products, replace=True):
    """Çıkarılan ürünleri kategorize edip kaydeder."""
    rules = kategori.load_rules()
    if replace:
        db.execute('DELETE FROM products WHERE marka = ?', (marka,))
    rows = []
    for p in products:
        grup, kat, montaj = kategori.classify(p, rules)
        img = p.get('image_jpeg')
        if img is None and p.get('image_b64'):
            img = base64.b64decode(p['image_b64'])
        rows.append((p['sku'], p.get('ad', ''), p.get('fiyat'), kat, source_pdf, p.get('sayfa'),
                     marka, p.get('para_birimi') or '', grup, p.get('alt_grup', ''),
                     p.get('katalog_baslik', ''), ','.join(montaj), p.get('ozellik', ''),
                     '|'.join(p.get('uyari', [])), img))
    db.executemany('''INSERT INTO products (sku, product_name, price, category, source_pdf, source_page,
                      marka, currency, ana_grup, alt_grup, katalog_baslik, montaj, ozellik, uyari, image_blob)
                      VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''', rows)
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
            save_products(db, data['marka'], data['kaynak'], data['products'], replace=True)


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
        saved = save_products(db, marka, filename, result['products'], replace=True)
        db.close()
        uyari = sum(1 for p in result['products'] if p['uyari'])
        job.update(state='done', saved=saved, uyari=uyari, stats=result['stats'],
                   msg=f'{saved} ürün kaydedildi ({uyari} uyarılı).')
    except Exception as e:  # noqa
        job.update(state='error', msg=f'PDF işlenirken hata: {e}')


# ── Sorgu yardımcıları ──
def build_filter(args):
    where, params = ['1=1'], []
    q = (args.get('q') or '').strip()
    if q:
        for term in q.split():
            where.append('(sku LIKE ? OR product_name LIKE ? OR ozellik LIKE ? OR category LIKE ? OR alt_grup LIKE ?)')
            params += [f'%{term}%'] * 5
    if args.get('marka'):
        where.append('marka = ?'); params.append(args['marka'])
    if args.get('kat'):
        where.append('category = ?'); params.append(args['kat'])
    if args.get('montaj'):
        where.append("(',' || montaj || ',') LIKE ?"); params.append(f"%,{args['montaj']},%")
    if args.get('uyari'):
        where.append("COALESCE(uyari,'') <> ''")
    return ' AND '.join(where), params


# ── Sayfalar ──
@app.route('/')
def index():
    db = get_db()
    args = {k: (request.args.get(k) or '').strip() for k in ('q', 'marka', 'kat', 'montaj', 'uyari')}
    page = max(1, request.args.get('sayfa', 1, type=int))
    where, params = build_filter(args)
    count = db.execute(f'SELECT COUNT(*) FROM products WHERE {where}', params).fetchone()[0]
    products = db.execute(f'''SELECT id, sku, product_name, price, currency, category, ana_grup, alt_grup,
                              montaj, uyari, source_page, marka, image_blob IS NOT NULL AS has_img, image_data
                              FROM products WHERE {where}
                              ORDER BY ana_grup, category, alt_grup, id LIMIT ? OFFSET ?''',
                          params + [PER_PAGE, (page - 1) * PER_PAGE]).fetchall()

    # Kenar menüsü: marka filtresi dışındaki filtreler sayılara yansımaz (genel görünüm)
    mw, mp = build_filter({'marka': args['marka']})
    rules = kategori.load_rules()
    order = {gname: i for i, gname in enumerate(rules['grup_sirasi'])}
    groups = {}
    for r in db.execute(f'''SELECT COALESCE(ana_grup,'Diğer') g, COALESCE(category,'Kategorisiz') c, COUNT(*) n,
                            SUM(CASE WHEN COALESCE(uyari,'')<>'' THEN 1 ELSE 0 END) w
                            FROM products WHERE {mw} GROUP BY g, c ORDER BY c''', mp):
        groups.setdefault(r['g'], []).append(r)
    groups = sorted(groups.items(), key=lambda kv: order.get(kv[0], 99))
    montaj = {}
    for r in db.execute(f"SELECT montaj, uyari FROM products WHERE {mw} AND COALESCE(montaj,'')<>''", mp):
        for m in r['montaj'].split(','):
            montaj[m] = montaj.get(m, 0) + 1
    montaj_list = [(m, rules['montaj_etiket'].get(m, m), montaj[m])
                   for m in ['Sıva Altı', 'Sıva Üstü', 'Ray Tipi', 'Duvar Tipi'] if m in montaj]
    stats = db.execute(f'''SELECT COUNT(*) n, COUNT(DISTINCT category) k, COUNT(DISTINCT marka) m,
                           SUM(CASE WHEN COALESCE(uyari,'')<>'' THEN 1 ELSE 0 END) w FROM products WHERE {mw}''', mp).fetchone()
    markalar = [r[0] for r in db.execute('SELECT DISTINCT marka FROM products WHERE marka IS NOT NULL ORDER BY marka')]

    if args['montaj']:
        title = rules['montaj_etiket'].get(args['montaj'], args['montaj'])
    elif args['kat']:
        title = args['kat']
    elif args['uyari']:
        title = 'Uyarılı ürünler'
    else:
        title = 'Tüm ürünler'
    pages = (count + PER_PAGE - 1) // PER_PAGE
    return render_template('index.html', products=products, args=args, count=count, page=page, pages=pages,
                           groups=groups, montaj_list=montaj_list, stats=stats, markalar=markalar, title=title)


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
        return redirect(url_for('index'))
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
        msg = f'{marka} ürünleri silindi.'
    else:
        db.execute('DELETE FROM products')
        msg = 'Tüm ürünler silindi.'
    db.commit()
    flash(msg, 'success')
    return redirect(url_for('index'))


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
    return redirect(url_for('index'))


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
        return redirect(url_for('index'))

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
    return render_template('sistem.html', checks=checks)


init_db()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=True)

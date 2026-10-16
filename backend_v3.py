"""
CT Satelital — Backend v3
Cámpora Troncoso Inmobiliaria Rural, Mercedes, Buenos Aires

Novedades v3:
  • Acepta 'geojson' (polígono dibujado en la app) además de archivo KMZ/KML
  • Base de datos SQLite: guarda cada análisis automáticamente
  • Endpoints: POST /analizar | GET /campos | GET /campos/<id> | DELETE /campos/<id>
  • Deploy-ready: lee credenciales GEE desde variable de entorno GEE_CREDENTIALS_JSON
    (Railway/Render) o desde el path local para desarrollo en la PC
"""

import os, re, json, math, zipfile, tempfile, sqlite3, time
from datetime import datetime
from pathlib import Path
from flask import Flask, request, jsonify, g
from flask_cors import CORS
import ee

# ─── Config ──────────────────────────────────────────────────────────────────
app = Flask(__name__)
CORS(app)

DATABASE = os.environ.get('DATABASE_PATH', 'ct_satelital.db')

# Credenciales GEE: env var (Railway) o archivo local (PC)
GEE_CREDENTIALS_JSON = os.environ.get('GEE_CREDENTIALS_JSON')  # JSON string en Railway
GEE_CREDENTIALS_FILE = os.environ.get(
    'GEE_CREDENTIALS_FILE',
    r'C:\Users\felip\ct-satelital\credenciales.json'          # ruta local PC
)
GEE_PROJECT = os.environ.get('GEE_PROJECT', 'proyecto-de-suelos-y-ambientes')

# Assets INTA
ASSET_BA  = f'projects/{GEE_PROJECT}/assets/Suelos_BA_50mil_V2'
ASSET_CBA = f'projects/{GEE_PROJECT}/assets/Suelos_CBA'
ASSET_SF  = f'projects/{GEE_PROJECT}/assets/Suelos_SF'

# ─── Inicializar GEE ─────────────────────────────────────────────────────────
def init_gee():
    if GEE_CREDENTIALS_JSON:
        # Railway: JSON completo en variable de entorno
        import json as _json
        cred_dict = _json.loads(GEE_CREDENTIALS_JSON)
        service_account = cred_dict.get('client_email')
        import google.oauth2.service_account as sa
        scopes = ['https://www.googleapis.com/auth/earthengine']
        credentials = sa.Credentials.from_service_account_info(cred_dict, scopes=scopes)
        ee.Initialize(credentials, project=GEE_PROJECT)
    elif os.path.exists(GEE_CREDENTIALS_FILE):
        # PC local: archivo JSON
        credentials = ee.ServiceAccountCredentials(None, GEE_CREDENTIALS_FILE)
        ee.Initialize(credentials, project=GEE_PROJECT)
    else:
        raise RuntimeError(
            'No se encontraron credenciales GEE. '
            'Configurá GEE_CREDENTIALS_JSON (Railway) o GEE_CREDENTIALS_FILE (PC).'
        )

try:
    _cred_env = os.environ.get('GEE_CREDENTIALS_JSON')
    _proj_env = os.environ.get('GEE_PROJECT')
    print(f'[DEBUG] GEE_CREDENTIALS_JSON presente: {bool(_cred_env)} (len={len(_cred_env) if _cred_env else 0})')
    print(f'[DEBUG] GEE_PROJECT: {_proj_env}')
    init_gee()
    print('✓ GEE inicializado')
except Exception as e:
    print(f'⚠ GEE no inicializado: {e}')

# ─── SQLite ───────────────────────────────────────────────────────────────────
def get_db():
    db = getattr(g, '_database', None)
    if db is None:
        db = g._database = sqlite3.connect(DATABASE)
        db.row_factory = sqlite3.Row
    return db

@app.teardown_appcontext
def close_db(exception):
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()

def init_db():
    db = sqlite3.connect(DATABASE)
    db.execute('''
        CREATE TABLE IF NOT EXISTS campos (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            nombre       TEXT NOT NULL,
            partido      TEXT,
            propietario  TEXT,
            sup_form     TEXT,
            precio       TEXT,
            descripcion  TEXT,
            anio         INTEGER,
            fecha        TEXT NOT NULL,
            superficie   REAL,
            ndvi         REAL,
            url_verano   TEXT,
            url_invierno TEXT,
            url_ndvi     TEXT,
            url_ambientes TEXT,
            url_suelos   TEXT,
            url_topografia TEXT,
            ambientes_json TEXT,
            suelos_json  TEXT
        )
    ''')
    db.commit()
    db.close()

with app.app_context():
    init_db()

# ─── Helpers ──────────────────────────────────────────────────────────────────
def kmz_a_geometry(archivo_kmz: str) -> ee.Geometry:
    """Extrae coordenadas de un archivo KMZ/KML/ZIP y devuelve ee.Geometry."""
    path = Path(archivo_kmz)
    kml_content = None

    if path.suffix.lower() in ('.kmz', '.zip'):
        with zipfile.ZipFile(archivo_kmz) as zf:
            kml_names = [n for n in zf.namelist() if n.lower().endswith('.kml')]
            if not kml_names:
                raise ValueError('No se encontró archivo KML dentro del KMZ')
            with zf.open(kml_names[0]) as f:
                kml_content = f.read().decode('utf-8', errors='replace')
    else:
        kml_content = path.read_text(encoding='utf-8', errors='replace')

    # Extraer bloques <coordinates>
    blocks = re.findall(r'<coordinates[^>]*>(.*?)</coordinates>', kml_content,
                        re.DOTALL | re.IGNORECASE)
    if not blocks:
        raise ValueError('No se encontraron coordenadas en el KML')

    # Tomar el bloque más largo (suele ser el perímetro del polígono)
    raw = max(blocks, key=len).strip()
    coords = []
    for token in raw.split():
        parts = token.split(',')
        if len(parts) >= 2:
            try:
                lng, lat = float(parts[0]), float(parts[1])
                coords.append([lng, lat])
            except ValueError:
                continue

    if len(coords) < 3:
        raise ValueError(f'Se necesitan al menos 3 puntos (se encontraron {len(coords)})')

    # Cerrar el anillo si hace falta
    if coords[0] != coords[-1]:
        coords.append(coords[0])

    return ee.Geometry.Polygon([coords])


def geojson_a_geometry(geojson_str: str) -> ee.Geometry:
    """Convierte un GeoJSON Polygon string a ee.Geometry."""
    gj = json.loads(geojson_str)
    t  = gj.get('type', '')
    if t == 'Polygon':
        return ee.Geometry.Polygon(gj['coordinates'])
    elif t == 'Feature':
        return ee.Geometry(gj['geometry'])
    elif t == 'FeatureCollection':
        return ee.FeatureCollection(gj).geometry()
    else:
        raise ValueError(f'GeoJSON type no soportado: {t}')


def enmascarar_nubes(img):
    """Máscara de nubes con banda SCL de Sentinel-2 SR."""
    scl  = img.select('SCL')
    mask = scl.eq(4).Or(scl.eq(5)).Or(scl.eq(6)).Or(scl.eq(7)).Or(scl.eq(11))
    return img.updateMask(mask).divide(10000)


def get_thumb_url(image, region, vis_params):
    """Genera URL de miniatura JPG 600×600."""
    return image.getThumbURL({
        'dimensions': '600x600',
        'region':     region,
        'format':     'jpg',
        **vis_params
    })


def normalizar_ba(fc):
    return fc.map(lambda f: f.set({
        'simbolo': f.get('SIMBOLO'),
        'nombre':  f.get('NOMBRE'),
        'clase':   f.get('CLASE'),
    }))

def normalizar_cba(fc):
    return fc.map(lambda f: f.set({
        'simbolo': f.get('SIMBOLO'),
        'nombre':  f.get('NOMBRE'),
        'clase':   f.get('CLASE_CAP'),
    }))

def normalizar_sf(fc):
    return fc.map(lambda f: f.set({
        'simbolo': f.get('SIMBOLO'),
        'nombre':  f.get('NOMBRE'),
        'clase':   f.get('CLASE_CAP'),
    }))


# ─── Análisis principal ────────────────────────────────────────────────────────
def analizar_lote(geom: ee.Geometry, num_ambientes: int = 4, anio: int = 2024) -> dict:
    """
    Ejecuta el análisis completo sobre la geometría dada y devuelve un dict
    con superficie, ndvi, URLs de 6 mapas, lista de ambientes y suelos.
    """
    region_bounds = geom.bounds()

    # ── Sentinel-2 SR Harmonized ──────────────────────────────────────────────
    s2 = (ee.ImageCollection('COPERNICUS/S2_SR_HARMONIZED')
          .filterBounds(geom)
          .map(enmascarar_nubes))

    # Verano: diciembre año anterior – febrero año referencia
    s2_ver = (s2.filter(ee.Filter.calendarRange(anio - 1, anio, 'year'))
                .filter(ee.Filter.calendarRange(12, 12, 'month'))
                .merge(
                    s2.filter(ee.Filter.calendarRange(anio, anio, 'year'))
                      .filter(ee.Filter.calendarRange(1, 2, 'month'))
                ))
    img_ver = s2_ver.median().clip(geom)

    # Invierno: junio–agosto año referencia
    s2_inv = (s2.filter(ee.Filter.calendarRange(anio, anio, 'year'))
                .filter(ee.Filter.calendarRange(6, 8, 'month')))
    img_inv = s2_inv.median().clip(geom)

    # ── NDVI multi-año (anio-5 → anio) ────────────────────────────────────────
    anio_ini = anio - 5
    s2_range = (ee.ImageCollection('COPERNICUS/S2_SR_HARMONIZED')
                .filterBounds(geom)
                .filterDate(f'{anio_ini}-01-01', f'{anio}-12-31')
                .filter(ee.Filter.calendarRange(11, 3, 'month'))  # temporada alta
                .map(enmascarar_nubes))

    def add_ndvi(img):
        ndvi = img.normalizedDifference(['B8', 'B4']).rename('NDVI')
        return img.addBands(ndvi)

    s2_ndvi = s2_range.map(add_ndvi)
    img_ndvi_raw = s2_ndvi.select('NDVI').mean().clip(geom)

    # NDVI scalar para stats
    ndvi_stat = img_ndvi_raw.reduceRegion(
        reducer=ee.Reducer.mean(),
        geometry=geom,
        scale=30,
        maxPixels=1e9
    ).getInfo().get('NDVI', 0)

    # ── Superficie ────────────────────────────────────────────────────────────
    area_m2   = geom.area(maxError=1).getInfo()
    superficie = area_m2 / 10000.0

    # ── Ambientes K-means ─────────────────────────────────────────────────────
    img_base = img_ver.addBands(img_ndvi_raw)
    bands    = ['B2', 'B3', 'B4', 'B8', 'NDVI']
    img_clus = img_base.select([b for b in bands if b in img_base.bandNames().getInfo()])

    training  = img_clus.sample(region=geom, scale=30, numPixels=1000, seed=42)
    clusterer = ee.Clusterer.wekaKMeans(num_ambientes).train(training)
    img_amb   = img_clus.cluster(clusterer).clip(geom)

    # ── Suelos INTA ───────────────────────────────────────────────────────────
    # Detectar provincia por centroide
    centroid   = geom.centroid(maxError=1)
    gaul       = ee.FeatureCollection('FAO/GAUL/2015/level1')
    prov_feat  = gaul.filterBounds(centroid).first()
    prov_name  = prov_feat.get('ADM1_NAME').getInfo() if prov_feat else ''
    prov_lower = (prov_name or '').lower()

    suelos_fc = None
    if 'córdoba' in prov_lower or 'cordoba' in prov_lower:
        suelos_fc = normalizar_cba(ee.FeatureCollection(ASSET_CBA).filterBounds(geom))
    elif 'santa fe' in prov_lower:
        suelos_fc = normalizar_sf(ee.FeatureCollection(ASSET_SF).filterBounds(geom))
    else:
        # Buenos Aires y resto
        suelos_fc = normalizar_ba(ee.FeatureCollection(ASSET_BA).filterBounds(geom))

    # Merge de los tres assets como fallback si el primero queda vacío
    if suelos_fc.size().getInfo() == 0:
        suelos_fc = (normalizar_ba(ee.FeatureCollection(ASSET_BA).filterBounds(geom))
                     .merge(normalizar_cba(ee.FeatureCollection(ASSET_CBA).filterBounds(geom)))
                     .merge(normalizar_sf(ee.FeatureCollection(ASSET_SF).filterBounds(geom))))

    suelos_img = suelos_fc.reduceToImage(
        properties=['clase'],
        reducer=ee.Reducer.first()
    ).clip(geom)

    # ── Topografía SRTM ───────────────────────────────────────────────────────
    dem = ee.Image('USGS/SRTMGL1_003').clip(geom)
    elev_stats = dem.reduceRegion(
        reducer=ee.Reducer.minMax(),
        geometry=geom,
        scale=30,
        maxPixels=1e9
    ).getInfo()
    elev_min = elev_stats.get('elevation_min', 0) or 0
    elev_max = elev_stats.get('elevation_max', 100) or 100

    # ── Generar miniaturas ────────────────────────────────────────────────────
    rgb_vis  = {'bands': ['B4', 'B3', 'B2'], 'min': 0.0, 'max': 0.3}
    ndvi_vis = {'palette': ['#d73027','#f46d43','#fdae61','#fee08b',
                             '#d9ef8b','#a6d96a','#66bd63','#1a9850'],
                'min': 0.0, 'max': 0.8}
    amb_vis  = {'min': 0, 'max': num_ambientes - 1,
                'palette': ['#4CAF50','#2196F3','#FF9800','#9C27B0','#F44336','#009688'][:num_ambientes]}
    sue_vis  = {'min': 1, 'max': 8,
                'palette': ['#8B4513','#A0522D','#CD853F','#DEB887','#F4A460','#D2B48C','#BC8B5A','#E8D5B0']}
    top_vis  = {'min': elev_min, 'max': elev_max,
                'palette': ['#006837','#31a354','#78c679','#c2e699','#ffffcc',
                             '#feb24c','#fd8d3c','#f03b20','#bd0026']}

    url_ver  = get_thumb_url(img_ver,     region_bounds, rgb_vis)
    url_inv  = get_thumb_url(img_inv,     region_bounds, rgb_vis)
    url_ndvi = get_thumb_url(img_ndvi_raw, region_bounds, ndvi_vis)
    url_amb  = get_thumb_url(img_amb,     region_bounds, amb_vis)
    url_sue  = get_thumb_url(suelos_img,  region_bounds, sue_vis)
    url_top  = get_thumb_url(dem,         region_bounds, top_vis)

    # ── Tabla de ambientes ────────────────────────────────────────────────────
    ndvi_by_amb = (s2_ndvi.select('NDVI').mean()
                   .addBands(img_amb.rename('cluster'))
                   .reduceRegion(
                       reducer=ee.Reducer.mean().group(groupField=1, groupName='cluster'),
                       geometry=geom,
                       scale=30,
                       maxPixels=1e9
                   ).getInfo().get('groups', []))

    area_by_amb = img_amb.reduceRegion(
        reducer=ee.Reducer.frequencyHistogram(),
        geometry=geom,
        scale=30,
        maxPixels=1e9
    ).getInfo().get('cluster', {})

    total_px  = sum(area_by_amb.values()) or 1
    ambientes = []
    for i in range(num_ambientes):
        px   = area_by_amb.get(str(i), 0) or area_by_amb.get(i, 0)
        ha   = (px * 30 * 30) / 10000
        pct  = (px / total_px) * 100
        ndvi_g = next((g['mean'] for g in ndvi_by_amb if int(g['cluster']) == i), None)
        ambientes.append({'hectareas': round(ha, 1), 'porcentaje': round(pct, 1),
                          'ndvi_promedio': round(ndvi_g, 3) if ndvi_g else None})

    # ── Tabla de suelos ───────────────────────────────────────────────────────
    sue_props = suelos_fc.toList(200).getInfo()
    suelos    = []
    seen      = set()
    for feat in sue_props:
        props = feat.get('properties', {})
        sim   = str(props.get('simbolo') or props.get('SIMBOLO') or '—')
        nom   = str(props.get('nombre')  or props.get('NOMBRE')  or '—')
        cls   = str(props.get('clase')   or props.get('CLASE')   or props.get('CLASE_CAP') or '—')
        if sim in seen:
            continue
        seen.add(sim)

        # Área de intersección
        try:
            geom_sue = ee.Feature(feat).geometry()
            inter    = geom.intersection(geom_sue, maxError=1)
            area_ha  = inter.area(maxError=1).getInfo() / 10000
        except Exception:
            area_ha  = 0.0

        if area_ha < 0.1:
            continue
        suelos.append({
            'simbolo':    sim,
            'nombre':     nom[:60],
            'clase':      cls,
            'hectareas':  round(area_ha, 1),
            'porcentaje': round((area_ha / superficie) * 100, 1),
        })

    suelos.sort(key=lambda x: x['hectareas'], reverse=True)

    return {
        'superficie':   round(superficie, 1),
        'ndvi_promedio': round(float(ndvi_stat or 0), 3),
        'url_verano':    url_ver,
        'url_invierno':  url_inv,
        'url_ndvi':      url_ndvi,
        'url_ambientes': url_amb,
        'url_suelos':    url_sue,
        'url_topografia': url_top,
        'ambientes': ambientes,
        'suelos':    suelos,
    }


# ─── Endpoints ────────────────────────────────────────────────────────────────

@app.route('/ping')
def ping():
    return jsonify({'ok': True, 'msg': 'CT Satelital backend v3 activo 🛰️'})


@app.route('/analizar', methods=['POST'])
def analizar():
    """
    Recibe KMZ (campo 'kmz') O GeoJSON dibujado (campo 'geojson').
    Parámetros opcionales: nombre, num_ambientes, anio.
    Guarda el resultado en SQLite y lo devuelve como JSON.
    """
    nombre        = request.form.get('nombre', 'Campo sin nombre').strip()
    num_ambientes = int(request.form.get('num_ambientes', 4))
    anio          = int(request.form.get('anio', 2024))
    partido       = request.form.get('partido', '').strip()
    propietario   = request.form.get('propietario', '').strip()
    sup_form      = request.form.get('sup', '').strip()
    precio        = request.form.get('precio', '').strip()
    descripcion   = request.form.get('descripcion', '').strip()

    # ── Obtener geometría ──────────────────────────────────────────────────────
    geom = None
    tmp_path = None

    if 'kmz' in request.files:
        f   = request.files['kmz']
        ext = Path(f.filename).suffix
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=ext)
        f.save(tmp.name)
        tmp_path = tmp.name
        try:
            geom = kmz_a_geometry(tmp_path)
        except Exception as e:
            return jsonify({'error': f'Error leyendo KMZ: {str(e)}'}), 400
        finally:
            try: os.unlink(tmp_path)
            except: pass

    elif 'geojson' in request.form:
        try:
            geom = geojson_a_geometry(request.form['geojson'])
        except Exception as e:
            return jsonify({'error': f'Error en GeoJSON: {str(e)}'}), 400
    else:
        return jsonify({'error': 'Se necesita un archivo KMZ o un GeoJSON del perímetro'}), 400

    # ── Análisis GEE ──────────────────────────────────────────────────────────
    try:
        result = analizar_lote(geom, num_ambientes=num_ambientes, anio=anio)
    except Exception as e:
        return jsonify({'error': f'Error en GEE: {str(e)}'}), 500

    # ── Guardar en SQLite ──────────────────────────────────────────────────────
    db    = get_db()
    fecha = datetime.utcnow().isoformat()
    cur   = db.execute('''
        INSERT INTO campos
          (nombre, partido, propietario, sup_form, precio, descripcion,
           anio, fecha, superficie, ndvi,
           url_verano, url_invierno, url_ndvi, url_ambientes, url_suelos, url_topografia,
           ambientes_json, suelos_json)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    ''', (
        nombre, partido, propietario, sup_form, precio, descripcion,
        anio, fecha, result['superficie'], result['ndvi_promedio'],
        result['url_verano'], result['url_invierno'], result['url_ndvi'],
        result['url_ambientes'], result['url_suelos'], result['url_topografia'],
        json.dumps(result['ambientes']),
        json.dumps(result['suelos']),
    ))
    db.commit()
    campo_id = cur.lastrowid

    return jsonify({**result, 'id': campo_id, 'fecha': fecha})


@app.route('/campos', methods=['GET'])
def listar_campos():
    """Devuelve todos los campos guardados (sin las URLs de mapas para ahorrar ancho de banda)."""
    db  = get_db()
    cur = db.execute('''
        SELECT id, nombre, partido, propietario, sup_form, precio, descripcion,
               anio, fecha, superficie, ndvi, url_ndvi
        FROM campos ORDER BY id DESC
    ''')
    campos = []
    for row in cur:
        campos.append({
            'id':          row['id'],
            'nombre':      row['nombre'],
            'partido':     row['partido'],
            'propietario': row['propietario'],
            'sup_form':    row['sup_form'],
            'precio':      row['precio'],
            'descripcion': row['descripcion'],
            'anio':        row['anio'],
            'fecha':       row['fecha'],
            'superficie':  row['superficie'],
            'ndvi':        row['ndvi'],
            'thumb':       row['url_ndvi'],
        })
    return jsonify(campos)


@app.route('/campos/<int:campo_id>', methods=['GET'])
def obtener_campo(campo_id):
    """Devuelve todos los datos de un campo, incluyendo URLs de mapas y tablas."""
    db  = get_db()
    cur = db.execute('SELECT * FROM campos WHERE id = ?', (campo_id,))
    row = cur.fetchone()
    if not row:
        return jsonify({'error': 'Campo no encontrado'}), 404
    d = dict(row)
    d['ambientes'] = json.loads(d['ambientes_json'] or '[]')
    d['suelos']    = json.loads(d['suelos_json']    or '[]')
    del d['ambientes_json'], d['suelos_json']
    return jsonify(d)


@app.route('/campos/<int:campo_id>', methods=['DELETE'])
def eliminar_campo(campo_id):
    db = get_db()
    db.execute('DELETE FROM campos WHERE id = ?', (campo_id,))
    db.commit()
    return jsonify({'ok': True, 'deleted': campo_id})


# ─── Run ──────────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    debug = os.environ.get('FLASK_DEBUG', 'false').lower() == 'true'
    print(f'CT Satelital backend v3 — puerto {port}')
    app.run(host='0.0.0.0', port=port, debug=debug)

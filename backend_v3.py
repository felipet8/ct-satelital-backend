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
from flask import Flask, request, jsonify, g, Response
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
    elif t == 'MultiPolygon':
        return ee.Geometry(gj)
    elif t == 'Feature':
        return ee.Geometry(gj['geometry'])
    elif t == 'FeatureCollection':
        return ee.FeatureCollection(gj).geometry()
    else:
        raise ValueError(f'GeoJSON type no soportado: {t}')


def enmascarar_nubes(img):
    """Máscara de nubes con banda SCL de Sentinel-2 SR.
    Excluye nubes (8,9,10), sombras de nubes (3) y nieve (11).
    Mantiene: saturado(1), oscuro(2), vegetación(4), suelo(5), agua(6),
    prob-nube-baja(7), cirrus(10 excluido), areas no vegetadas(5).
    Estrategia: excluir píxeles malos en vez de solo mantener buenos.
    """
    scl = img.select('SCL')
    # Excluir: 3=sombra nube, 8=nube media, 9=nube alta, 10=cirrus
    bad_mask = scl.eq(3).Or(scl.eq(8)).Or(scl.eq(9)).Or(scl.eq(10))
    # copyProperties: divide() borra system:time_start y rompe los filtros por fecha
    # ee.Image(...) es necesario: copyProperties devuelve un Element genérico
    return ee.Image(img.updateMask(bad_mask.Not()).divide(10000)
                    .copyProperties(img, ['system:time_start', 'CLOUDY_PIXEL_PERCENTAGE']))


def get_thumb_url(image, region, vis_params):
    """Genera URL de miniatura JPG 600×600.
    region puede ser ee.Geometry o dict GeoJSON — se convierte a dict para getThumbURL.
    """
    # getThumbURL acepta GeoJSON dict o ee.Geometry serializada
    # Para evitar que GEE haga zoom raro, pasamos las coordenadas explícitas
    if hasattr(region, 'getInfo'):
        region_param = region.bounds().getInfo()  # dict GeoJSON del bounding box
    else:
        region_param = region
    return image.getThumbURL({
        'dimensions': '600x600',
        'region':     region_param,
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


# ─── Capas reutilizables (análisis + descargas) ───────────────────────────────
S2 = 'COPERNICUS/S2_SR_HARMONIZED'
BANDAS_S2 = ['B2', 'B3', 'B4', 'B8', 'B12']
AMB_COLORS = ['#4CAF50', '#2196F3', '#FF9800', '#9C27B0', '#F44336', '#009688']
SUELO_COLORS = ['#8B4513', '#2E86C1', '#E67E22', '#27AE60', '#8E44AD', '#F1C40F',
                '#C0392B', '#16A085', '#D35400', '#7F8C8D', '#2C3E50', '#E91E63']


def get_best_seasonal(geom, target_anio, tipo, fallback_years=5):
    """Elige UNA imagen real (la de menor nubosidad) de la estación pedida.
    tipo='verano': dic(año-1)–feb(año) | tipo='invierno': jun–ago(año).
    Si no hay imágenes ese año, retrocede a años anteriores.
    Devuelve (imagen_para_mostrar, imagen_enmascarada, año_real, fecha 'YYYY-MM-DD')."""
    hoy = datetime.utcnow()
    for offset in range(fallback_years + 1):
        yr = target_anio - offset
        if tipo == 'verano':
            ini, fin = f'{yr-1}-12-01', f'{yr}-03-01'
        else:
            ini, fin = f'{yr}-06-01', f'{yr}-09-01'
        if datetime.strptime(ini, '%Y-%m-%d') > hoy:
            continue  # estación todavía no empezó
        col = ee.ImageCollection(S2).filterBounds(geom).filterDate(ini, fin)
        # Preferir escenas que cubren todo el campo
        col_full = col.filter(ee.Filter.contains(leftField='.geo', rightValue=geom))
        if col_full.size().getInfo() > 0:
            col = col_full
        if col.size().getInfo() == 0:
            continue
        best = ee.Image(col.sort('CLOUDY_PIXEL_PERCENTAGE').first())
        fecha = ee.Date(best.get('system:time_start')).format('YYYY-MM-dd').getInfo()
        display = best.select(BANDAS_S2).divide(10000).clip(geom)
        masked  = enmascarar_nubes(best).select(BANDAS_S2).clip(geom)
        return display, masked, yr, fecha
    vacia = (ee.Image(0).rename('B8').addBands(ee.Image(0).rename('B4'))
             .addBands(ee.Image(0).rename('B12')).addBands(ee.Image(0).rename('B3'))
             .addBands(ee.Image(0).rename('B2')).clip(geom))
    return vacia, vacia, target_anio, None


def calc_ndvi(geom, anio):
    """NDVI medio de temporada alta (nov–mar) de los últimos 5 años."""
    s2_range = (ee.ImageCollection(S2)
                .filterBounds(geom)
                .filterDate(f'{anio-5}-01-01', f'{anio}-12-31')
                .filter(ee.Filter.calendarRange(11, 3, 'month'))
                .map(enmascarar_nubes))
    s2_ndvi = s2_range.map(lambda img: img.addBands(
        img.normalizedDifference(['B8', 'B4']).rename('NDVI')))
    return s2_ndvi, s2_ndvi.select('NDVI').mean().clip(geom)


def calc_ambientes(geom, img_ver_m, img_ndvi, num_ambientes):
    """Clasificación K-means sobre la imagen de verano + NDVI."""
    img_base = img_ver_m.addBands(img_ndvi)
    bands = ['B2', 'B3', 'B4', 'B8', 'NDVI']
    img_clus = img_base.select([b for b in bands if b in img_base.bandNames().getInfo()])
    training  = img_clus.sample(region=geom, scale=30, numPixels=1000, seed=42)
    clusterer = ee.Clusterer.wekaKMeans(num_ambientes).train(training)
    return img_clus.cluster(clusterer).clip(geom)


def detectar_provincia(geom):
    centroid = geom.centroid(maxError=1)
    for asset in ('FAO/GAUL/2025/level1', 'FAO/GAUL/2015/level1'):
        try:
            feat = ee.FeatureCollection(asset).filterBounds(centroid).first()
            return (feat.get('ADM1_NAME').getInfo() or '').lower()
        except Exception:
            continue
    return ''


def _get_prop(props, *keys):
    for k in keys:
        v = props.get(k)
        if v is not None and str(v).strip() not in ('', 'None', 'null'):
            return str(v).strip()
    return '—'


def _fuzzy(props, *frags):
    for frag in frags:
        for k, v in props.items():
            if frag in k.lower() and v is not None and str(v).strip() not in ('', 'None', 'null'):
                return str(v).strip()
    return '—'


def procesar_suelos(geom, superficie):
    """Suelos INTA dentro del campo.
    Devuelve (lista_para_tabla, ee.FeatureCollection con propiedades normalizadas e 'idx')."""
    prov_lower = detectar_provincia(geom)
    if 'córdoba' in prov_lower or 'cordoba' in prov_lower:
        raw = ee.FeatureCollection(ASSET_CBA).filterBounds(geom)
    elif 'santa fe' in prov_lower:
        raw = ee.FeatureCollection(ASSET_SF).filterBounds(geom)
    else:
        raw = ee.FeatureCollection(ASSET_BA).filterBounds(geom)

    if raw.size().getInfo() == 0:
        raw = (ee.FeatureCollection(ASSET_BA).filterBounds(geom)
               .merge(ee.FeatureCollection(ASSET_CBA).filterBounds(geom))
               .merge(ee.FeatureCollection(ASSET_SF).filterBounds(geom)))

    def _clip(f):
        g = f.geometry().intersection(geom, 1)
        return ee.Feature(g, f.toDictionary()).set('_ha', g.area(1).divide(10000))

    feats = raw.map(_clip).toList(300).getInfo()
    if feats:
        print(f"[DEBUG] Suelos props: {feats[0].get('properties', {})}")

    grupos = {}
    for i, feat in enumerate(feats):
        props = feat.get('properties', {})
        ha = props.get('_ha') or 0
        if ha < 0.05 or not feat.get('geometry'):
            continue
        sim = _get_prop(props, 'SIMBOLO','simbolo','Simbolo','SYMBOL','symbol','SIM','sim','SIMB','simb','COD','cod')
        if sim == '—': sim = _fuzzy(props, 'simb', 'sym', 'cod', 'uc', 'unidad')
        nom = _get_prop(props, 'NOMBRE','nombre','Nombre','NAME','name','NOM','nom','DESCRIPCION','descripcion','DESC','desc')
        if nom == '—': nom = _fuzzy(props, 'nombre', 'nom', 'desc', 'serie', 'name', 'taxon')
        cls = _get_prop(props, 'CLASE','clase','Clase','CLASE_CAP','clase_cap','CAP','cap','CAPACIDAD','capacidad','CLAS','clas')
        if cls == '—': cls = _fuzzy(props, 'clase', 'cap', 'clas', 'ip', 'uso')
        key = sim if sim != '—' else f'{nom}|{cls}|{i}'
        g = grupos.setdefault(key, {'simbolo': sim, 'nombre': nom, 'clase': cls, 'ha': 0.0, 'geoms': []})
        g['ha'] += ha
        g['geoms'].append(feat['geometry'])

    orden = sorted(grupos.values(), key=lambda x: x['ha'], reverse=True)
    suelos, ee_feats = [], []
    for idx, g in enumerate(orden):
        color = SUELO_COLORS[idx % len(SUELO_COLORS)]
        suelos.append({
            'simbolo':    g['simbolo'],
            'nombre':     g['nombre'][:60],
            'clase':      g['clase'],
            'hectareas':  round(g['ha'], 1),
            'porcentaje': round((g['ha'] / superficie) * 100, 1),
            'color':      color,
        })
        for gj in g['geoms']:
            try:
                ee_feats.append(ee.Feature(ee.Geometry(gj), {
                    'simbolo': g['simbolo'], 'nombre': g['nombre'][:60],
                    'clase': g['clase'], 'ha': round(g['ha'], 2), 'idx': idx}))
            except Exception as e:
                print(f'[WARN] geometría de suelo omitida: {e}')
    return suelos, ee.FeatureCollection(ee_feats)


def imagen_suelos(fc, geom, n):
    """Imagen categórica: cada unidad de suelo con su color (por índice numérico)."""
    if n == 0:
        return ee.Image(0).clip(geom), {'min': 0, 'max': 1, 'palette': ['#999999', '#999999']}
    img = fc.reduceToImage(properties=['idx'], reducer=ee.Reducer.first()).clip(geom)
    pal = [SUELO_COLORS[i % len(SUELO_COLORS)] for i in range(max(n, 2))]
    return img, {'min': 0, 'max': max(n - 1, 1), 'palette': pal}


def fc_ambientes(img_amb):
    """Polígonos de ambientes (K-means) con propiedades 'ambiente' (A1..An) y 'ha'."""
    vec = img_amb.select(0).toInt().reduceToVectors(
        geometry=img_amb.geometry(), scale=30, geometryType='polygon',
        eightConnected=False, labelProperty='amb', maxPixels=1e9, bestEffort=True)
    def _p(f):
        n = ee.Number(f.get('amb')).add(1).int()
        return ee.Feature(f.geometry(), {
            'ambiente': ee.String('A').cat(n.format()),
            'num': n,
            'ha': f.geometry().area(1).divide(10000)})
    return vec.map(_p)


# ─── Análisis principal ────────────────────────────────────────────────────────
def analizar_lote(geom: ee.Geometry, num_ambientes: int = 4, anio: int = 2024) -> dict:
    """
    Ejecuta el análisis completo sobre la geometría dada y devuelve un dict
    con superficie, ndvi, URLs de 6 mapas, lista de ambientes y suelos.
    """
    img_ver, img_ver_m, anio_ver_real, fecha_ver = get_best_seasonal(geom, anio, 'verano')
    img_inv, img_inv_m, anio_inv_real, fecha_inv = get_best_seasonal(geom, anio, 'invierno')

    s2_ndvi, img_ndvi_raw = calc_ndvi(geom, anio)
    ndvi_stat = img_ndvi_raw.reduceRegion(
        reducer=ee.Reducer.mean(), geometry=geom, scale=30, maxPixels=1e9
    ).getInfo().get('NDVI', 0)

    superficie = geom.area(maxError=1).getInfo() / 10000.0

    img_amb = calc_ambientes(geom, img_ver_m, img_ndvi_raw, num_ambientes)

    suelos, suelos_fc = procesar_suelos(geom, superficie)
    suelos_img, sue_vis = imagen_suelos(suelos_fc, geom, len(suelos))

    # ── Topografía SRTM ───────────────────────────────────────────────────────
    dem = ee.Image('USGS/SRTMGL1_003').clip(geom)
    elev_stats = dem.reduceRegion(
        reducer=ee.Reducer.minMax(), geometry=geom, scale=30, maxPixels=1e9
    ).getInfo()
    elev_min = elev_stats.get('elevation_min', 0) or 0
    elev_max = elev_stats.get('elevation_max', 100) or 100

    # ── Miniaturas ────────────────────────────────────────────────────────────
    rgb_vis  = {'bands': ['B8', 'B4', 'B12'], 'min': 0.0, 'max': 0.5}  # falso color NIR-R-SWIR
    ndvi_vis = {'palette': ['#d73027','#f46d43','#fdae61','#fee08b',
                             '#d9ef8b','#a6d96a','#66bd63','#1a9850'],
                'min': 0.0, 'max': 0.8}
    amb_vis  = {'min': 0, 'max': max(num_ambientes - 1, 1),
                'palette': AMB_COLORS[:max(num_ambientes, 2)]}
    top_vis  = {'min': elev_min, 'max': elev_max,
                'palette': ['#006837','#31a354','#78c679','#c2e699','#ffffcc',
                             '#feb24c','#fd8d3c','#f03b20','#bd0026']}

    url_ver  = get_thumb_url(img_ver,      geom, rgb_vis)
    url_inv  = get_thumb_url(img_inv,      geom, rgb_vis)
    url_ndvi = get_thumb_url(img_ndvi_raw, geom, ndvi_vis)
    url_amb  = get_thumb_url(img_amb,      geom, amb_vis)
    url_sue  = get_thumb_url(suelos_img,   geom, sue_vis)
    url_top  = get_thumb_url(dem,          geom, top_vis)

    # ── Tabla de ambientes ────────────────────────────────────────────────────
    ndvi_by_amb = (s2_ndvi.select('NDVI').mean()
                   .addBands(img_amb.rename('cluster'))
                   .reduceRegion(
                       reducer=ee.Reducer.mean().group(groupField=1, groupName='cluster'),
                       geometry=geom, scale=30, maxPixels=1e9
                   ).getInfo().get('groups', []))

    area_by_amb = img_amb.reduceRegion(
        reducer=ee.Reducer.frequencyHistogram(), geometry=geom, scale=30, maxPixels=1e9
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

    return {
        'superficie':    round(superficie, 1),
        'ndvi_promedio': round(float(ndvi_stat or 0), 3),
        'url_verano':    url_ver,
        'url_invierno':  url_inv,
        'url_ndvi':      url_ndvi,
        'url_ambientes': url_amb,
        'url_suelos':    url_sue,
        'url_topografia': url_top,
        'ambientes':     ambientes,
        'suelos':        suelos,
        'fecha_verano':   fecha_ver,
        'fecha_invierno': fecha_inv,
        'anio_verano':   anio_ver_real,
        'anio_invierno': anio_inv_real,
        'num_ambientes': num_ambientes,
        'geojson':       geom.getInfo(),   # el frontend lo reenvía para pedir descargas
    }


# ─── Descargas (GeoTIFF, Shapefile, KMZ) ──────────────────────────────────────
def _url_raster(img, geom, scale_base, nbandas, nombre):
    """URL de descarga GeoTIFF. Sube la escala si el archivo superaría ~30 MB."""
    bbox_m2 = geom.bounds(1).area(1).getInfo()
    scale = scale_base
    while (bbox_m2 / (scale * scale)) * nbandas * 4 > 30e6:
        scale *= 2
    return img.getDownloadURL({
        'name': nombre, 'scale': scale, 'region': geom,
        'crs': 'EPSG:4326', 'format': 'GEO_TIFF'})


def _descargar_bytes(url):
    import urllib.request
    return urllib.request.urlopen(url, timeout=90).read()


def _kml_contorno(gj, nombre):
    """KML propio: polígonos con contorno blanco y SIN relleno."""
    from xml.sax.saxutils import escape

    def ring(c):
        return ' '.join(f'{p[0]},{p[1]},0' for p in c)

    def poly(coords):
        out = '<Polygon><outerBoundaryIs><LinearRing><coordinates>' + ring(coords[0]) + \
              '</coordinates></LinearRing></outerBoundaryIs>'
        for h in coords[1:]:
            out += '<innerBoundaryIs><LinearRing><coordinates>' + ring(h) + \
                   '</coordinates></LinearRing></innerBoundaryIs>'
        return out + '</Polygon>'

    def geom(g):
        t = (g or {}).get('type')
        if t == 'Polygon':
            return poly(g['coordinates'])
        if t == 'MultiPolygon':
            return '<MultiGeometry>' + ''.join(poly(p) for p in g['coordinates']) + '</MultiGeometry>'
        if t == 'GeometryCollection':
            inner = ''.join(geom(x) for x in g['geometries'])
            return '<MultiGeometry>' + inner + '</MultiGeometry>' if inner else ''
        return ''

    marcas = []
    for f in gj.get('features', []):
        g = geom(f.get('geometry'))
        if not g:
            continue
        props = f.get('properties') or {}
        nom = next((str(props[k]) for k in ('ambiente', 'simbolo', 'nombre') if props.get(k)), nombre)
        desc = ''.join(f'<b>{escape(str(k))}</b>: {escape(str(v))}<br/>'
                       for k, v in props.items() if v is not None)
        marcas.append(f'<Placemark><name>{escape(nom)}</name>'
                      f'<description><![CDATA[{desc}]]></description>'
                      f'<styleUrl>#contorno</styleUrl>{g}</Placemark>')
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            '<kml xmlns="http://www.opengis.net/kml/2.2"><Document>'
            f'<name>{escape(nombre)}</name>'
            '<Style id="contorno"><LineStyle><color>ffffffff</color><width>2</width></LineStyle>'
            '<PolyStyle><fill>0</fill><outline>1</outline></PolyStyle></Style>'
            + ''.join(marcas) + '</Document></kml>')


def _kmz_bytes(fc, nombre):
    """Descarga las geometrías de GEE y arma un KMZ con contorno blanco sin relleno."""
    import io
    gj = json.loads(_descargar_bytes(fc.getDownloadURL(filetype='geojson', filename=nombre)))
    kml = _kml_contorno(gj, nombre)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        zf.writestr('doc.kml', kml.encode('utf-8'))
    return buf.getvalue()


QML_CONTORNO = """<!DOCTYPE qgis PUBLIC 'http://mrcc.com/qgis.dtd' 'SYSTEM'>
<qgis version="3.28.0" styleCategories="Symbology">
 <renderer-v2 type="singleSymbol" symbollevels="0" forceraster="0" enableorderby="0">
  <symbols>
   <symbol type="fill" name="0" alpha="1" clip_to_extent="1" force_rhr="0">
    <layer class="SimpleFill" enabled="1" locked="0" pass="0">
     <Option type="Map">
      <Option name="color" value="255,255,255,0" type="QString"/>
      <Option name="style" value="no" type="QString"/>
      <Option name="outline_color" value="255,255,255,255" type="QString"/>
      <Option name="outline_style" value="solid" type="QString"/>
      <Option name="outline_width" value="0.6" type="QString"/>
      <Option name="outline_width_unit" value="MM" type="QString"/>
     </Option>
    </layer>
   </symbol>
  </symbols>
 </renderer-v2>
</qgis>
"""


def _shp_zip_con_estilo(fc, nombre):
    """Baja el Shapefile (.zip) de GEE y le agrega un .qml (QGIS): contorno blanco, sin relleno."""
    import io
    datos = _descargar_bytes(fc.getDownloadURL(filetype='SHP', filename=nombre))
    zin = zipfile.ZipFile(io.BytesIO(datos))
    stem = next((n[:-4] for n in zin.namelist() if n.lower().endswith('.shp')), nombre)
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as zout:
        for n in zin.namelist():
            zout.writestr(n, zin.read(n))
        zout.writestr(f'{stem}.qml', QML_CONTORNO)
    return out.getvalue()


def generar_descarga(tipo, geom, anio, num_ambientes, nombre):
    """Devuelve ('url', url) o ('bytes', (archivo, mime, datos))."""
    base = re.sub(r'[^A-Za-z0-9_-]+', '_', nombre or 'campo')[:40] or 'campo'

    if tipo == 'tif_elevacion':
        img = ee.Image('USGS/SRTMGL1_003').select('elevation').clip(geom)
        return 'url', _url_raster(img, geom, 30, 1, f'{base}_elevacion')
    if tipo == 'tif_ndvi':
        _, ndvi = calc_ndvi(geom, anio)
        return 'url', _url_raster(ndvi.toFloat(), geom, 10, 1, f'{base}_ndvi')
    if tipo in ('tif_verano', 'tif_invierno'):
        est = 'verano' if tipo == 'tif_verano' else 'invierno'
        disp, _, _, _ = get_best_seasonal(geom, anio, est)
        return 'url', _url_raster(disp.toFloat(), geom, 10, 5, f'{base}_sentinel2_{est}')
    if tipo == 'tif_ambientes':
        _, img_ver_m, _, _ = get_best_seasonal(geom, anio, 'verano')
        _, ndvi = calc_ndvi(geom, anio)
        amb = calc_ambientes(geom, img_ver_m, ndvi, num_ambientes)
        return 'url', _url_raster(amb.toInt16(), geom, 10, 1, f'{base}_ambientes')

    # Vectores
    if tipo.endswith('_suelos'):
        _, fc = procesar_suelos(geom, geom.area(1).getInfo() / 10000.0)
    elif tipo.endswith('_ambientes'):
        _, img_ver_m, _, _ = get_best_seasonal(geom, anio, 'verano')
        _, ndvi = calc_ndvi(geom, anio)
        fc = fc_ambientes(calc_ambientes(geom, img_ver_m, ndvi, num_ambientes))
    elif tipo.endswith('_perimetro'):
        fc = ee.FeatureCollection([ee.Feature(geom, {'nombre': nombre or 'campo'})])
    else:
        raise ValueError(f'Tipo de descarga desconocido: {tipo}')

    sufijo = tipo.split('_', 1)[1]
    if tipo.startswith('shp_'):
        return 'url', fc.getDownloadURL(filetype='SHP', filename=f'{base}_{sufijo}')
    if tipo.startswith('kmz_'):
        return 'bytes', (f'{base}_{sufijo}.kmz',
                         'application/vnd.google-earth.kmz', _kmz_bytes(fc, f'{base}_{sufijo}'))
    raise ValueError(f'Tipo de descarga desconocido: {tipo}')


# ─── Endpoints ────────────────────────────────────────────────────────────────

@app.route('/ping')
def ping():
    return jsonify({'ok': True, 'msg': 'CT Satelital backend v3 activo 🛰️'})


@app.route('/debug/suelos')
def debug_suelos():
    """Muestra las propiedades reales de los assets INTA (1 feature de cada uno).
    Abrir en el navegador: <backend>/debug/suelos"""
    out = {}
    for nombre, asset in (('BA', ASSET_BA), ('CBA', ASSET_CBA), ('SF', ASSET_SF)):
        try:
            f = ee.FeatureCollection(asset).first().getInfo()
            out[nombre] = f.get('properties', {}) if f else None
        except Exception as e:
            out[nombre] = f'error: {e}'
    return jsonify(out)


@app.route('/descargar', methods=['POST'])
def descargar():
    """Genera una descarga (GeoTIFF / SHP / KMZ). Recibe el geojson del campo,
    el tipo y los parámetros del análisis. Devuelve {'url': ...} o el archivo."""
    tipo   = request.form.get('tipo', '')
    nombre = request.form.get('nombre', 'campo')
    try:
        anio = int(request.form.get('anio', 2024))
        n    = int(request.form.get('num_ambientes', 4))
        geom = geojson_a_geometry(request.form['geojson'])
    except Exception as e:
        return jsonify({'error': f'Parámetros inválidos: {e}'}), 400
    try:
        kind, data = generar_descarga(tipo, geom, anio, n, nombre)
    except Exception as e:
        return jsonify({'error': f'No se pudo generar la descarga: {e}'}), 500
    if kind == 'url':
        return jsonify({'url': data})
    archivo, mime, datos = data
    return Response(datos, mimetype=mime,
                    headers={'Content-Disposition': f'attachment; filename="{archivo}"'})


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

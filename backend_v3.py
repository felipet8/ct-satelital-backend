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
        'dimensions': '700x700',
        'region':     region_param,
        'format':     'png',   # PNG: fuera del campo queda transparente (fondo blanco en la app/PDF)
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


def _dp(pts, tol):
    """Douglas-Peucker iterativo (coordenadas en grados)."""
    n = len(pts)
    if n < 4:
        return pts
    keep = [False] * n
    keep[0] = keep[-1] = True
    stack = [(0, n - 1)]
    while stack:
        a, b = stack.pop()
        ax, ay = pts[a]; bx, by = pts[b]
        dx, dy = bx - ax, by - ay
        L = dx * dx + dy * dy
        dmax, idx = 0.0, -1
        for i in range(a + 1, b):
            px, py = pts[i]
            if L == 0:
                d = ((px - ax) ** 2 + (py - ay) ** 2) ** 0.5
            else:
                t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L))
                d = ((px - (ax + t * dx)) ** 2 + (py - (ay + t * dy)) ** 2) ** 0.5
            if d > dmax:
                dmax, idx = d, i
        if dmax > tol and idx > 0:
            keep[idx] = True
            stack.extend([(a, idx), (idx, b)])
    return [p for p, k in zip(pts, keep) if k]


def _polys_simplificados(g, tol=0.00004):
    """GeoJSON (Polygon/MultiPolygon/GeometryCollection) -> lista de polígonos [[anillo,...],...]
    simplificados (~4 m) y con coordenadas redondeadas."""
    t = (g or {}).get('type')
    if t == 'Polygon':
        crudos = [g['coordinates']]
    elif t == 'MultiPolygon':
        crudos = g['coordinates']
    elif t == 'GeometryCollection':
        crudos = []
        for sub in g.get('geometries', []):
            crudos.extend(_polys_simplificados(sub, tol))
        return crudos
    else:
        return []
    out = []
    for poly in crudos:
        anillos = []
        for ring in poly:
            r = _dp([(p[0], p[1]) for p in ring], tol)
            if len(r) >= 4:
                anillos.append([[round(x, 5), round(y, 5)] for x, y in r])
        if anillos:
            out.append(anillos)
    return out


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
    poligonos_raw = []   # (clave, ha, geometry) para la lámina
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
        if not g.get('attrs'):
            g['attrs'] = {k: v for k, v in props.items()
                          if not k.startswith('_') and v not in (None, '') and isinstance(v, (str, int, float))}
        poligonos_raw.append((key, ha, feat['geometry']))

    orden = sorted(grupos.values(), key=lambda x: x['ha'], reverse=True)
    suelos, ee_feats = [], []
    idx_por_clave = {}
    for idx, g in enumerate(orden):
        color = SUELO_COLORS[idx % len(SUELO_COLORS)]
        suelos.append({
            'simbolo':    g['simbolo'],
            'nombre':     g['nombre'][:60],
            'clase':      g['clase'],
            'hectareas':  round(g['ha'], 1),
            'porcentaje': round((g['ha'] / superficie) * 100, 1),
            'color':      color,
            'atributos':  dict(list((g.get('attrs') or {}).items())[:14]),
        })
        idx_por_clave[next(k for k, v in grupos.items() if v is g)] = idx
        for gj in g['geoms']:
            try:
                ee_feats.append(ee.Feature(ee.Geometry(gj), {
                    'simbolo': g['simbolo'], 'nombre': g['nombre'][:60],
                    'clase': g['clase'], 'ha': round(g['ha'], 2), 'idx': idx}))
            except Exception as e:
                print(f'[WARN] geometría de suelo omitida: {e}')
    poligonos = []
    for clave, ha, gj in poligonos_raw:
        if ha < 0.3 or clave not in idx_por_clave:
            continue
        i = idx_por_clave[clave]
        polys = _polys_simplificados(gj)
        if polys:
            poligonos.append({'ha': round(ha, 1), 'simbolo': suelos[i]['simbolo'],
                              'color': suelos[i]['color'], 'poly': polys})
    return suelos, ee.FeatureCollection(ee_feats), poligonos


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

    suelos, suelos_fc, suelos_poligonos = procesar_suelos(geom, superficie)
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
        'suelos_poligonos': suelos_poligonos,
        'elev_min':      round(float(elev_min), 1),
        'elev_max':      round(float(elev_max), 1),
        'geojson':       geom.getInfo(),   # el frontend lo reenvía para pedir descargas
    }


# ─── Imagen Sentinel-2 de una fecha elegida ───────────────────────────────────
def imagen_por_fecha(geom, fecha, modo='falso', dims=1000, margen=0.12, ventana=12, criterio='cercana'):
    """Miniatura (JPG, sin recortar) de la imagen Sentinel-2 más cercana a `fecha`
    (±ventana días, preferentemente con poca nubosidad), con un margen alrededor del campo."""
    d0 = ee.Date(fecha)
    anillo = geom.bounds(1).getInfo()['coordinates'][0]
    xs = [p[0] for p in anillo]; ys = [p[1] for p in anillo]
    w, e, s_, n = min(xs), max(xs), min(ys), max(ys)
    mx, my = (e - w) * margen, (n - s_) * margen
    bbox = [w - mx, s_ - my, e + mx, n + my]
    region = ee.Geometry.Rectangle(bbox, 'EPSG:4326', False)

    def coleccion(asset):
        col = (ee.ImageCollection(asset).filterBounds(geom)
               .filterDate(d0.advance(-ventana, 'day'), d0.advance(ventana + 1, 'day')))
        full = col.filter(ee.Filter.contains(leftField='.geo', rightValue=geom))
        return full if full.size().getInfo() > 0 else col

    col, fuente = coleccion(S2), 'SR'
    if col.size().getInfo() == 0:
        col, fuente = coleccion('COPERNICUS/S2_HARMONIZED'), 'TOA'   # reflectancia TOA (antes de 2019)
    if col.size().getInfo() == 0:
        raise ValueError(f'No hay imágenes Sentinel-2 a ±{ventana} días de {fecha}. Probá otra fecha.')
    claras = col.filter(ee.Filter.lt('CLOUDY_PIXEL_PERCENTAGE', 60))
    if claras.size().getInfo() > 0:
        col = claras
    col = col.map(lambda i: i.set('dd', ee.Number(i.date().difference(d0, 'day')).abs()))
    orden = 'CLOUDY_PIXEL_PERCENTAGE' if criterio == 'limpia' else 'dd'   # 'limpia': la de menos nubes dentro de la ventana
    best = ee.Image(col.sort(orden).first())
    fecha_real = ee.Date(best.get('system:time_start')).format('YYYY-MM-dd').getInfo()

    if modo == 'real':
        vis = {'bands': ['B4', 'B3', 'B2'], 'min': 0.0, 'max': 0.3}
    else:
        vis = {'bands': ['B8', 'B4', 'B12'], 'min': 0.0, 'max': 0.5}
    img = best.select(['B2', 'B3', 'B4', 'B8', 'B12']).divide(10000)
    url = img.getThumbURL({'dimensions': dims, 'region': region.getInfo(), 'format': 'jpg', **vis})
    return {'url': url, 'fecha': fecha_real, 'bbox': bbox, 'fuente': fuente, 'modo': modo}


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
        _, fc, _ = procesar_suelos(geom, geom.area(1).getInfo() / 10000.0)
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


# ─── Ambientes "modelo Felipe": clasificador entrenado con sus mapas ─────────
ASSET_ENTREN = f'projects/{GEE_PROJECT}/assets/ambientes_entrenamiento'
AMB_IA = {1: ('Loma', '#267300'), 2: ('Tendido alto', '#70A800'), 3: ('Tendido bajo', '#FFD37F'),
          4: ('Bajos encharcables', '#FFFFBE'), 5: ('Cubetas y lagunas semipermanentes', '#73DFFF'),
          6: ('Montes y construcciones', '#730000')}
AMB_IA_BANDAS = ['agua_freq', 'mndwi_p90', 'mndwi_p50', 'ndvi_p10', 'ndvi_p50', 'ndvi_p90', 'ndvi_sd',
                 'b11_p50', 'ndvi_ver', 'ndvi_oto', 'ndvi_inv', 'ndvi_pri', 'tpi300', 'tpi900', 'elev']
# v2: usa las fechas de referencia de Felipe → máxima seca = 2018, máxima agua = fines de 2025,
#     y estaciones de años normales (2019-2024)
AMB_IA_BANDAS_V2 = ['agua_freq', 'ndvi_seca', 'mndwi_seca', 'b11_seca',
                    'ndvi_hum', 'mndwi_hum', 'mndwi_hum_max', 'b11_hum', 'd_ndvi', 'd_mndwi',
                    'ndvi_ver', 'ndvi_oto', 'ndvi_inv', 'ndvi_pri', 'ndvi_sd', 'tpi300', 'tpi900', 'elev']


def pila_ambientes(region, desde='2018-01-01', version='v2'):
    """Capas que describen cuánto se inunda, cuánto verde tiene y dónde está en el relieve cada píxel.
    Usa todas las imágenes Sentinel-2 desde `desde`: los percentiles extremos reemplazan a elegir a mano
    la imagen de 'máxima agua' y 'máxima seca'."""
    hoy = datetime.utcnow().strftime('%Y-%m-%d')

    def indices(i):
        i = enmascarar_nubes(i)
        ndvi  = i.normalizedDifference(['B8', 'B4']).rename('ndvi')
        mndwi = i.normalizedDifference(['B3', 'B11']).rename('mndwi')
        return ndvi.addBands(mndwi).addBands(i.select('B11').rename('b11')) \
                   .set('system:time_start', i.get('system:time_start'))

    col = (ee.ImageCollection(S2).filterBounds(region).filterDate(desde, hoy)
           .filter(ee.Filter.lt('CLOUDY_PIXEL_PERCENTAGE', 60)).map(indices))
    pct = col.select(['ndvi', 'mndwi', 'b11']).reduce(ee.Reducer.percentile([10, 50, 90]))
    agua = col.map(lambda i: i.select('mndwi').gt(0).rename('w')).mean().rename('agua_freq')
    sd = col.select('ndvi').reduce(ee.Reducer.stdDev()).rename('ndvi_sd')

    def estacion(m1, m2, nombre):
        c = col.filter(ee.Filter.calendarRange(m1, m2, 'month')).select('ndvi')
        return c.median().rename(nombre)
    ver = col.filter(ee.Filter.Or(ee.Filter.calendarRange(12, 12, 'month'),
                                  ee.Filter.calendarRange(1, 2, 'month'))).select('ndvi').median().rename('ndvi_ver')
    oto = estacion(3, 5, 'ndvi_oto')
    inv = estacion(6, 8, 'ndvi_inv')
    pri = estacion(9, 11, 'ndvi_pri')

    elev = ee.Image('USGS/SRTMGL1_003').select('elevation').rename('elev')
    tpi300 = elev.subtract(elev.focalMean(300, 'circle', 'meters')).rename('tpi300')
    tpi900 = elev.subtract(elev.focalMean(900, 'circle', 'meters')).rename('tpi900')

    if version != 'v2':
        img = (agua.addBands(pct.select(['mndwi_p90', 'mndwi_p50', 'ndvi_p10', 'ndvi_p50', 'ndvi_p90', 'b11_p50']))
                  .addBands(sd).addBands(ver).addBands(oto).addBands(inv).addBands(pri)
                  .addBands(elev).addBands(tpi300).addBands(tpi900))
        return img.select(AMB_IA_BANDAS)

    # ── v2: fechas de referencia de Felipe ──
    def indices_toa(i):
        # Antes de 2019 Sentinel-2 SR no cubre toda la zona: se usa reflectancia TOA con máscara QA60
        qa = i.select('QA60')
        limpio = qa.bitwiseAnd(1 << 10).eq(0).And(qa.bitwiseAnd(1 << 11).eq(0))
        i = i.updateMask(limpio).divide(10000)
        return (i.normalizedDifference(['B8', 'B4']).rename('ndvi')
                 .addBands(i.normalizedDifference(['B3', 'B11']).rename('mndwi'))
                 .addBands(i.select('B11').rename('b11')))

    def col_robusta(ini, fin):
        sr = (ee.ImageCollection(S2).filterBounds(region).filterDate(ini, fin)
              .filter(ee.Filter.lt('CLOUDY_PIXEL_PERCENTAGE', 60)).map(indices))
        toa = (ee.ImageCollection('COPERNICUS/S2_HARMONIZED').filterBounds(region).filterDate(ini, fin)
               .filter(ee.Filter.lt('CLOUDY_PIXEL_PERCENTAGE', 60)).map(indices_toa))
        return ee.ImageCollection(ee.Algorithms.If(sr.size().gt(0), sr, toa))

    def comp(ini, fin, sufijo, extra_max=False):
        c = col_robusta(ini, fin)
        med = c.median()
        out = med.select('ndvi').rename('ndvi_' + sufijo).addBands(med.select('mndwi').rename('mndwi_' + sufijo)) \
                .addBands(med.select('b11').rename('b11_' + sufijo))
        if extra_max:
            out = out.addBands(c.select('mndwi').reduce(ee.Reducer.percentile([90])).rename('mndwi_' + sufijo + '_max'))
        return out
    seca = comp('2017-12-01', '2018-04-01', 'seca')                        # máxima seca: verano 2017-18
    hum  = comp('2025-10-15', '2026-01-15', 'hum', extra_max=True)         # máxima agua: fines de 2025
    d_ndvi  = hum.select('ndvi_hum').subtract(seca.select('ndvi_seca')).rename('d_ndvi')
    d_mndwi = hum.select('mndwi_hum').subtract(seca.select('mndwi_seca')).rename('d_mndwi')

    def normal(m1, m2, nombre):
        c = (col.filterDate('2019-01-01', '2025-01-01')
                .filter(ee.Filter.calendarRange(m1, m2, 'month')).select('ndvi'))
        return c.median().rename(nombre)
    n_ver = (col.filterDate('2019-01-01', '2025-01-01')
                .filter(ee.Filter.Or(ee.Filter.calendarRange(12, 12, 'month'), ee.Filter.calendarRange(1, 2, 'month')))
                .select('ndvi').median().rename('ndvi_ver'))
    img = (agua.addBands(seca).addBands(hum).addBands(d_ndvi).addBands(d_mndwi)
              .addBands(n_ver).addBands(normal(3, 5, 'ndvi_oto')).addBands(normal(6, 8, 'ndvi_inv'))
              .addBands(normal(9, 11, 'ndvi_pri')).addBands(sd)
              .addBands(elev).addBands(tpi300).addBands(tpi900))
    return img.select(AMB_IA_BANDAS_V2)


AMB_IA_BANDAS_V3 = AMB_IA_BANDAS_V2 + ['agua_freq_abs']
_CAMPOS_CACHE = {}


def normalizar_por_campo(pila, geom):
    """v3: cada capa se expresa respecto del propio campo (cuántos desvíos más alta o más baja que el promedio
    del campo). Imita el método de Felipe: de lo más alto del campo a lo más bajo del mismo campo."""
    media = pila.reduceRegion(ee.Reducer.mean(), geom, 20, maxPixels=1e9, tileScale=4)
    desv  = pila.reduceRegion(ee.Reducer.stdDev(), geom, 20, maxPixels=1e9, tileScale=4)
    m = ee.Dictionary(media).toImage(AMB_IA_BANDAS_V2)
    s = ee.Dictionary(desv).toImage(AMB_IA_BANDAS_V2).max(1e-6)
    z = pila.select(AMB_IA_BANDAS_V2).subtract(m).divide(s)
    return z.addBands(pila.select('agua_freq').rename('agua_freq_abs')).select(AMB_IA_BANDAS_V3).toFloat()


def pila_para_clasificar(pila, geom, version):
    return normalizar_por_campo(pila, geom) if version == 'v3' else pila


def entrenar_ambientes(excluir_campo=None, n_por_clase=500, version='v2'):
    """Entrena un Random Forest con los mapas de Felipe (asset ambientes_entrenamiento).
    Si `excluir_campo`, ese campo queda afuera (para validar contra un mapa que el modelo no vio)."""
    fc = ee.FeatureCollection(ASSET_ENTREN)
    region = fc.geometry().bounds()
    pila = pila_ambientes(region, version='v1' if version == 'v1' else 'v2')
    bandas = {'v1': AMB_IA_BANDAS, 'v2': AMB_IA_BANDAS_V2, 'v3': AMB_IA_BANDAS_V3}.get(version, AMB_IA_BANDAS_V2)
    entren = fc if not excluir_campo else fc.filter(ee.Filter.neq('campo', excluir_campo))
    clase_img = entren.reduceToImage(['clase'], ee.Reducer.first()).rename('clase').toInt()
    pila_train = pila
    if version == 'v3':
        if 'campos' not in _CAMPOS_CACHE:
            _CAMPOS_CACHE['campos'] = sorted(fc.aggregate_array('campo').distinct().getInfo())
        imgs = []
        for c in _CAMPOS_CACHE['campos']:
            if c == excluir_campo:
                continue
            g = fc.filter(ee.Filter.eq('campo', c)).geometry()
            imgs.append(normalizar_por_campo(pila, g).clip(g).toFloat())
        pila_train = ee.ImageCollection(imgs).mosaic()
    muestra = pila_train.addBands(clase_img).stratifiedSample(
        numPoints=n_por_clase, classBand='clase', region=entren.geometry(),
        scale=20, seed=7, dropNulls=True, tileScale=4)
    clf = ee.Classifier.smileRandomForest(150, seed=7).train(muestra, 'clase', bandas)
    return clf, pila, fc


@app.route('/validar_ia', methods=['GET'])
def validar_ia():
    """Compara el modelo contra un mapa de Felipe que NO se usó para entrenar.
    /validar_ia                         → lista de campos disponibles
    /validar_ia?campo=El%20Buzon        → precisión general y por ambiente en ese campo"""
    try:
        fc = ee.FeatureCollection(ASSET_ENTREN)
        campo = request.args.get('campo')
        if not campo:
            return jsonify({'campos': sorted(fc.aggregate_array('campo').distinct().getInfo()),
                            'ejemplo': '/validar_ia?campo=La%20Cigue%C3%B1a%202&modelo=v2   (modelo=v1 y v2 son versiones anteriores; probá modelo=v3)'})
        version = request.args.get('modelo', 'v2')
        clf, pila, fc = entrenar_ambientes(excluir_campo=campo, version=version)
        test = fc.filter(ee.Filter.eq('campo', campo))
        clase_img = test.reduceToImage(['clase'], ee.Reducer.first()).rename('clase').toInt()
        pred = pila_para_clasificar(pila, test.geometry(), version).classify(clf).focalMode(30, 'circle', 'meters').rename('classification')
        muestra = pred.addBands(clase_img).stratifiedSample(
            numPoints=400, classBand='clase', region=test.geometry(), scale=20, seed=3,
            dropNulls=True, tileScale=4)
        cm = muestra.errorMatrix('clase', 'classification', [1, 2, 3, 4, 5, 6])
        acc = cm.accuracy().getInfo()
        # además: precisión "realista" sin balancear por clase (puntos al azar proporcionales a la superficie)
        azar = pred.addBands(clase_img).sample(region=test.geometry(), scale=20, numPixels=3000, seed=5,
                                               dropNulls=True, tileScale=4)
        cm2 = azar.errorMatrix('clase', 'classification', [1, 2, 3, 4, 5, 6])
        mat = cm2.getInfo()                      # filas = mapa de Felipe, columnas = modelo (clases 1..6)
        tot = sum(sum(f) for f in mat) or 1
        ok = sum(mat[i][i] for i in range(len(mat)))
        por_amb = {}
        for i, f in enumerate(mat):
            s = sum(f)
            if s:
                por_amb[AMB_IA[i + 1][0]] = {'puntos': int(s), 'acierto': round(f[i] / s, 2)}
        return jsonify({
            'campo': campo, 'modelo': version,
            'precision_general_proporcional_a_superficie': round(ok / tot, 3),
            'precision_balanceada_por_ambiente': round(acc, 3),
            'por_ambiente(que_parte_del_mapa_de_Felipe_acierta_el_modelo)': por_amb,
            'matriz_filas_Felipe_columnas_modelo': mat,
            'orden_clases': [AMB_IA[k][0] for k in sorted(AMB_IA)],
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/ambientes_ia', methods=['POST'])
def ambientes_ia():
    """Clasifica un campo con el modelo entrenado con TODOS los mapas de Felipe.
    Recibe geojson (igual que /imagen). Devuelve imagen, hectáreas por ambiente y colores."""
    try:
        geom = geojson_a_geometry(request.form['geojson'])
    except Exception as e:
        return jsonify({'error': f'Parámetros inválidos: {e}'}), 400
    try:
        version = request.form.get('modelo', 'v2')
        clf, pila, fc = entrenar_ambientes(version=version)
        pred = pila_para_clasificar(pila, geom, version).classify(clf).focalMode(30, 'circle', 'meters').rename('clase').toInt()
        pred = pred.clip(geom)
        paleta = [AMB_IA[k][1].lstrip('#') for k in sorted(AMB_IA)]
        vis = pred.visualize(min=1, max=6, palette=paleta)
        anillo = geom.bounds(1).getInfo()['coordinates'][0]
        xs = [q[0] for q in anillo]; ys = [q[1] for q in anillo]
        w, e, s_, n = min(xs), max(xs), min(ys), max(ys)
        mx, my = (e - w) * 0.12, (n - s_) * 0.12
        bbox = [w - mx, s_ - my, e + mx, n + my]
        region = ee.Geometry.Rectangle(bbox, 'EPSG:4326', False)
        url = vis.getThumbURL({'dimensions': 1000, 'region': region.getInfo(), 'format': 'png'})
        areas = ee.Image.pixelArea().divide(1e4).addBands(pred).reduceRegion(
            reducer=ee.Reducer.sum().group(groupField=1, groupName='clase'),
            geometry=geom, scale=20, maxPixels=1e9, tileScale=4).getInfo()
        ha = {int(g['clase']): g['sum'] for g in areas.get('groups', [])}
        tot = sum(ha.values()) or 1
        return jsonify({
            'url': url, 'bbox': bbox,
            'ambientes': [{'id': k, 'nombre': AMB_IA[k][0], 'color': AMB_IA[k][1],
                           'ha': round(ha.get(k, 0), 1), 'pct': round(ha.get(k, 0) / tot * 100, 1)}
                          for k in sorted(AMB_IA) if ha.get(k, 0) > 0],
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ─── Ambientes propios: lectura de SHP (.zip) / KMZ / KML dibujados por Felipe ─
# Sin librerías extra: lector de shapefile y reproyección POSGAR (Gauss-Krüger) escritos a mano.
import io, math, re, struct, zipfile
import xml.etree.ElementTree as ET


def _gk_inverso(E, N, faja):
    """POSGAR 94/2007 faja 1-7 (Gauss-Krüger, k0=1, FE=faja*1e6+500000, origen lat -90) → (lon, lat) WGS84."""
    a = 6378137.0; f = 1 / 298.257223563; n = f / (2 - f)
    A = a / (1 + n) * (1 + n**2 / 4 + n**4 / 64)
    b1 = n/2 - 2*n**2/3 + 37*n**3/96 - n**4/360
    b2 = n**2/48 + n**3/15 - 437*n**4/1440
    b3 = 17*n**3/480 - 37*n**4/840
    b4 = 4397*n**4/161280
    d1 = 2*n - 2*n**2/3 - 2*n**3 + 116*n**4/45
    d2 = 7*n**2/3 - 8*n**3/5 - 227*n**4/45
    d3 = 56*n**3/15 - 136*n**4/35
    d4 = 4279*n**4/630
    xi  = (N - A * math.pi / 2) / A                 # N medido desde el polo sur
    eta = (E - (faja * 1e6 + 500000.0)) / A
    xi_p, eta_p = xi, eta
    for j, bj in enumerate((b1, b2, b3, b4), 1):
        xi_p  -= bj * math.sin(2*j*xi) * math.cosh(2*j*eta)
        eta_p -= bj * math.cos(2*j*xi) * math.sinh(2*j*eta)
    chi = math.asin(math.sin(xi_p) / math.cosh(eta_p))
    lat = chi + sum(dj * math.sin(2*j*chi) for j, dj in enumerate((d1, d2, d3, d4), 1))
    lon0 = math.radians(-75 + 3 * faja)
    lon = lon0 + math.atan2(math.sinh(eta_p), math.cos(xi_p))
    return math.degrees(lon), math.degrees(lat)


def _area_anillo(r):
    return sum(r[i][0] * r[i+1][1] - r[i+1][0] * r[i][1] for i in range(len(r) - 1)) / 2.0


def _leer_shp(data):
    """Devuelve lista de registros; cada uno = lista de anillos [(x,y),...] (polígonos)."""
    tipo_arch = struct.unpack('<i', data[32:36])[0]
    if tipo_arch not in (5, 15, 25):
        raise ValueError('El shapefile no es de polígonos')
    out, pos = [], 100
    while pos + 8 <= len(data):
        _, clen = struct.unpack('>ii', data[pos:pos+8]); pos += 8
        cont = data[pos:pos+clen*2]; pos += clen * 2
        t = struct.unpack('<i', cont[:4])[0]
        if t == 0:
            out.append([]); continue
        npart, npts = struct.unpack('<ii', cont[36:44])
        partes = list(struct.unpack('<%di' % npart, cont[44:44+4*npart]))
        o = 44 + 4 * npart
        pts = [struct.unpack('<2d', cont[o+16*i:o+16*i+16]) for i in range(npts)]
        partes.append(npts)
        out.append([pts[partes[i]:partes[i+1]] for i in range(npart)])
    return out


def _leer_dbf(data, enc='utf-8'):
    n = struct.unpack('<I', data[4:8])[0]; hl, rl = struct.unpack('<HH', data[8:12])
    campos, o = [], 32
    while data[o] != 0x0D:
        campos.append((data[o:o+11].split(b'\0')[0].decode('latin1'), chr(data[o+11]), data[o+16])); o += 32
    filas = []
    for i in range(n):
        r = data[hl+i*rl+1: hl+(i+1)*rl]; p = 0; fila = {}
        for nm, t, ln in campos:
            v = r[p:p+ln]; p += ln
            try:
                s = v.decode(enc).strip()
            except UnicodeDecodeError:
                s = v.decode('latin1').strip()
            if t in 'NF' and s:
                try: s = float(s); s = int(s) if s == int(s) else s
                except ValueError: pass
            fila[nm] = s
        filas.append(fila)
    return [c[0] for c in campos], filas


def _faja_de(prj_txt, x):
    if prj_txt:
        m = re.search(r'Central_Meridian["\s,]+(-?\d+(?:\.\d+)?)', prj_txt, re.I)
        if m:
            return int(round((float(m.group(1)) + 75) / 3))
        m = re.search(r'Faja[_ ]?(\d)', prj_txt, re.I)
        if m:
            return int(m.group(1))
    return int(abs(x) // 1e6)


def _es_geografico(prj_txt, x):
    if prj_txt:
        return not re.search(r'PROJCS', prj_txt, re.I)
    return abs(x) <= 360


def _area_ha_lonlat(anillos):
    """Área (ha) de anillos en lon/lat con proyección equirrectangular local."""
    if not anillos or not anillos[0]:
        return 0.0
    lat0 = sum(p[1] for p in anillos[0]) / len(anillos[0])
    kx = 111320.0 * math.cos(math.radians(lat0)); ky = 110574.0
    return abs(sum(_area_anillo([(p[0]*kx, p[1]*ky) for p in r]) for r in anillos)) / 1e4


def _elegir_atributo(campos, filas, pedido=None):
    def unicos(c): return {f.get(c) for f in filas if f.get(c) not in ('', None)}
    cand = [c for c in campos if 1 <= len(unicos(c)) <= 40]
    if pedido and pedido in campos:
        return pedido, cand
    pref = re.compile(r'^(ambiente|amb|ambientes|nombre|name|clase|class|tipo|categoria|categoría|descripcion|descripción)$', re.I)
    for c in cand:
        if pref.match(c) and any(isinstance(v, str) for v in unicos(c)):
            return c, cand
    for c in cand:
        if pref.match(c):
            return c, cand
    for c in cand:
        if re.match(r'^(id|gridcode|grid_code|clase|cod|codigo|código)$', c, re.I):
            return c, cand
    return (cand[0] if cand else None), cand


def leer_ambientes_propios(nombre_archivo, data, atributo=None):
    """Devuelve {'atributo', 'candidatos', 'clases': [{'clave','ha','pct','poly'}], 'bbox'}.
    poly = lista de polígonos [[anillo,...],...] en lon/lat, simplificados."""
    ext = nombre_archivo.lower().rsplit('.', 1)[-1]
    regs = []          # (clave_dict, anillos_lonlat)
    campos, cand, attr_usado = [], [], None
    if ext in ('kmz', 'kml'):
        if ext == 'kmz':
            z = zipfile.ZipFile(io.BytesIO(data))
            nm = next(n for n in z.namelist() if n.lower().endswith('.kml'))
            data = z.read(nm)
        raiz = ET.fromstring(data)
        def local(e): return e.tag.split('}')[-1]
        for pm in raiz.iter():
            if local(pm) != 'Placemark':
                continue
            nombre = next((((c.text or '').strip()) for c in pm if local(c) == 'name'), '')
            polys = [p for p in pm.iter() if local(p) == 'Polygon']
            for pg in polys:
                anillos = []
                for b in pg.iter():
                    if local(b) in ('outerBoundaryIs', 'innerBoundaryIs'):
                        for co in b.iter():
                            if local(co) == 'coordinates' and co.text:
                                pts = [tuple(float(v) for v in t.split(',')[:2]) for t in co.text.split() if ',' in t]
                                if len(pts) >= 4:
                                    anillos.append(pts)
                if anillos:
                    regs.append(({'nombre': nombre or 'Sin nombre'}, anillos))
        campos = ['nombre']; attr_usado = 'nombre'; cand = ['nombre']
    elif ext == 'zip':
        z = zipfile.ZipFile(io.BytesIO(data))
        nombres = z.namelist()
        shp = next((n for n in nombres if n.lower().endswith('.shp')), None)
        if not shp:
            raise ValueError('El .zip no contiene un archivo .shp')
        base = shp[:-4]
        def buscar(e): return next((n for n in nombres if n.lower() == (base + e).lower()), None)
        dbf, prj, cpg = buscar('.dbf'), buscar('.prj'), buscar('.cpg')
        geoms = _leer_shp(z.read(shp))
        enc = 'utf-8'
        if cpg:
            c = z.read(cpg).decode('latin1').strip().lower()
            enc = 'latin1' if ('1252' in c or '8859' in c or 'latin' in c) else 'utf-8'
        campos, filas = _leer_dbf(z.read(dbf), enc) if dbf else ([], [{} for _ in geoms])
        prj_txt = z.read(prj).decode('latin1') if prj else ''
        primero = next((r[0][0] for r in geoms if r and r[0]), (0, 0))
        geo = _es_geografico(prj_txt, primero[0])
        faja = None if geo else _faja_de(prj_txt, primero[0])
        if not geo and not (1 <= faja <= 7):
            raise ValueError('Sistema de coordenadas no soportado: exportá el shape en WGS84 (lat/lon) o POSGAR faja 1-7.')
        attr_usado, cand = _elegir_atributo(campos, filas, atributo)
        for rec, fila in zip(geoms, filas):
            if not rec:
                continue
            if geo:
                anillos = [list(r) for r in rec]
            else:
                anillos = [[_gk_inverso(x, y, faja) for x, y in r] for r in rec]
            clave = fila.get(attr_usado, '') if attr_usado else ''
            regs.append(({'clave': clave}, anillos))
    else:
        raise ValueError('Formato no soportado (usá .zip con el shapefile, .kmz o .kml)')

    grupos = {}
    for info, anillos in regs:
        clave = info.get('clave', info.get('nombre'))
        clave = '' if clave is None else clave
        g = grupos.setdefault(str(clave), {'clave': clave, 'ha': 0.0, 'poly': []})
        g['ha'] += _area_ha_lonlat(anillos)
        g['poly'].extend(_polys_simplificados({'type': 'Polygon', 'coordinates': [[list(p) for p in r] for r in anillos]}))
    if len(grupos) > 40:
        raise ValueError('Demasiados ambientes distintos (%d): elegí otro campo de atributos' % len(grupos))
    tot = sum(g['ha'] for g in grupos.values()) or 1
    clases = []
    xs, ys = [], []
    for k, g in sorted(grupos.items(), key=lambda kv: (isinstance(kv[1]['clave'], str), kv[1]['clave'])):
        for poly in g['poly']:
            for p in poly[0]:
                xs.append(p[0]); ys.append(p[1])
        clases.append({'clave': g['clave'], 'ha': round(g['ha'], 2), 'pct': round(g['ha'] / tot * 100, 1), 'poly': g['poly']})
    return {'atributo': attr_usado, 'candidatos': cand, 'clases': clases,
            'bbox': [min(xs), min(ys), max(xs), max(ys)] if xs else None}


@app.route('/ambientes_propios', methods=['POST'])
def ambientes_propios():
    """Lee los ambientes que dibujó Felipe (shapefile en .zip, .kmz o .kml) y devuelve polígonos + hectáreas por ambiente."""
    try:
        f = request.files['archivo']
        res = leer_ambientes_propios(f.filename or 'archivo.zip', f.read(), request.form.get('atributo') or None)
        return jsonify(res)
    except Exception as e:
        return jsonify({'error': f'No se pudo leer el archivo: {e}'}), 400


@app.route('/proxy_img', methods=['POST'])
def proxy_img():
    """Descarga una imagen generada por Earth Engine y la devuelve (para guardarla en el navegador).
    Solo acepta URLs de Earth Engine."""
    import urllib.request, urllib.parse
    from flask import Response
    try:
        u = request.form['url']
        host = urllib.parse.urlparse(u).hostname or ''
        if not (host == 'earthengine.googleapis.com' or host.endswith('.earthengine.googleapis.com')):
            return jsonify({'error': 'URL no permitida'}), 400
        with urllib.request.urlopen(u, timeout=60) as r:
            data = r.read()
            ctype = r.headers.get('Content-Type', 'image/png')
        return Response(data, mimetype=ctype)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/imagen', methods=['POST'])
def imagen():
    """Imagen Sentinel-2 de una fecha elegida (con margen alrededor del campo)."""
    try:
        geom  = geojson_a_geometry(request.form['geojson'])
        fecha = request.form['fecha']
        datetime.strptime(fecha, '%Y-%m-%d')
        modo  = request.form.get('modo', 'falso')
        dims  = max(300, min(int(request.form.get('dims', 1000)), 2000))
        ventana  = max(1, min(int(request.form.get('ventana', 12)), 60))
        criterio = request.form.get('criterio', 'cercana')
    except Exception as e:
        return jsonify({'error': f'Parámetros inválidos: {e}'}), 400
    try:
        return jsonify(imagen_por_fecha(geom, fecha, modo, dims, ventana=ventana, criterio=criterio))
    except Exception as e:
        return jsonify({'error': str(e)}), 500


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

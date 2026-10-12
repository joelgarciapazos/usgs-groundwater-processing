#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Reproducible download of USGS daily groundwater-depth records through the USGS Water Data OGC API.

The script retrieves parameter 72019 for USGS groundwater sites, preserves raw API pages and tabular exports, records SHA-256 checksums and retrieval metadata, and performs no scientific QA, unit filtering, temporal segmentation, or interpolation.

Python >=3.10; standard library only. Set USGS_API_KEY in the environment or enter it interactively. Use --root to choose the project data directory."""

from __future__ import annotations

import argparse
import csv
import getpass
import gzip
import hashlib
import io
import json
import logging
import os
import platform
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ROOT = Path(os.environ.get('USGS_GW_ROOT', str(REPO_ROOT / 'data')))
API = 'https://api.waterdata.usgs.gov/ogcapi/v1'
SCRIPT_VERSION = '1.0.0'
STATES = dict(zip(
    'AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY'.split(),
    '01 02 04 05 06 08 09 10 12 13 15 16 17 18 19 20 21 22 23 24 25 26 27 28 29 30 31 32 33 34 35 36 37 38 39 40 41 42 44 45 46 47 48 49 50 51 53 54 55 56'.split()
))
STATE_BY_CODE = {v: k for k, v in STATES.items()}
DAILY_FIELDS = [
    'monitoring_location_id', 'site_no', 'estado', 'time_series_id',
    'parameter_code', 'statistic_id', 'date', 'value_original',
    'unit_of_measure', 'approval_status', 'qualifier', 'last_modified',
    'feature_id', 'longitude', 'latitude', 'raw_page', 'retrieved_utc',
]


class DownloadError(RuntimeError):
    pass


class StopDownload(DownloadError):
    """Error global: no repetir la misma falla para todos los pozos."""


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def file_digest(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def atomic_bytes(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp')
    with tmp.open('wb') as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(path)


def save_json(path, data):
    atomic_bytes(path, json.dumps(data, ensure_ascii=False, indent=2).encode('utf-8'))


def load_json(path):
    return json.loads(path.read_text(encoding='utf-8'))


def cell(value):
    if value is None:
        return ''
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(',', ':'))
    return str(value)


def save_csv(path, rows, fields=None):
    rows = list(rows)
    fields = fields or sorted({k for row in rows for k in row}) or ['sin_registros']
    out = io.StringIO(newline='')
    writer = csv.DictWriter(out, fieldnames=fields, extrasaction='raise')
    writer.writeheader()
    for row in rows:
        writer.writerow({k: cell(v) for k, v in row.items()})
    raw = out.getvalue().encode('utf-8-sig')
    atomic_bytes(path, gzip.compress(raw, mtime=0) if path.suffix == '.gz' else raw)


def safe_url(url):
    p = urllib.parse.urlsplit(url)
    if p.scheme != 'https' or p.netloc != 'api.waterdata.usgs.gov':
        raise StopDownload('La API devolvió un enlace fuera del servidor USGS esperado.')
    if not p.path.startswith('/ogcapi/v1/'):
        raise StopDownload('La API cambió de versión. Revisar antes de continuar.')
    # La clave se transporta por cabecera, nunca mediante la URL.
    query = [(k, v) for k, v in urllib.parse.parse_qsl(p.query, keep_blank_values=True)
             if k.lower() != 'api_key']
    return urllib.parse.urlunsplit((p.scheme, p.netloc, p.path,
                                   urllib.parse.urlencode(query), ''))


class Client:
    def __init__(self, api_key, interval=4.0, timeout=120, attempts=6):
        self.api_key = api_key
        self.interval = interval
        self.timeout = timeout
        self.attempts = attempts
        self.last_request = 0.0
        self.opener = urllib.request.build_opener(SafeRedirect())

    def get(self, url):
        url = safe_url(url)
        for attempt in range(self.attempts):
            delay = self.interval - (time.monotonic() - self.last_request)
            if delay > 0:
                time.sleep(delay)
            headers = {'Accept': 'application/geo+json, application/json',
                       'User-Agent': 'GroundwaterDatasetDownload/1.0'}
            if self.api_key:
                headers['X-Api-Key'] = self.api_key
            self.last_request = time.monotonic()
            try:
                req = urllib.request.Request(url, headers=headers)
                with self.opener.open(req, timeout=self.timeout) as response:
                    safe_url(response.url)
                    raw = response.read()
                data = json.loads(raw)
                if not isinstance(data, dict):
                    raise DownloadError('La respuesta no es un objeto JSON.')
                return data, raw
            except urllib.error.HTTPError as exc:
                if exc.code in (401, 403):
                    raise StopDownload(f'HTTP {exc.code}: revisar la API key o el acceso.') from None
                if exc.code not in (408, 429, 500, 502, 503, 504):
                    raise StopDownload(f'HTTP {exc.code}: consulta rechazada; revisar esquema y parámetros.') from None
                wait = min(60, 2 ** (attempt + 1))
                retry_after = exc.headers.get('Retry-After')
                if retry_after:
                    try:
                        wait = max(wait, float(retry_after))
                    except ValueError:
                        try:
                            wait = max(wait, (parsedate_to_datetime(retry_after) -
                                             datetime.now(timezone.utc)).total_seconds())
                        except (TypeError, ValueError):
                            pass
                if wait > 120:
                    raise StopDownload('USGS solicita una espera prolongada; progreso guardado. Reanudar más tarde.') from None
                if attempt + 1 == self.attempts:
                    raise StopDownload(f'HTTP {exc.code} después de los reintentos; reanudar más tarde.') from None
            except (urllib.error.URLError, TimeoutError, socket.timeout,
                    ConnectionError, json.JSONDecodeError) as exc:
                if attempt + 1 == self.attempts:
                    raise DownloadError(f'Fallo de conexión/respuesta después de {self.attempts} intentos ({type(exc).__name__}).') from None
                wait = min(60, 2 ** (attempt + 1))
            logging.warning('Reintento %s/%s en %.0f s.', attempt + 2, self.attempts, wait)
            time.sleep(wait)
        raise DownloadError('No se obtuvo respuesta.')


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Comprobar el destino ANTES de reenviar una cabecera con credenciales.
        destination = safe_url(urllib.parse.urljoin(req.full_url, newurl))
        return super().redirect_request(req, fp, code, msg, headers, destination)


def next_url(data, current, initial):
    links = [x.get('href') for x in data.get('links', []) if x.get('rel') == 'next']
    if len(links) > 1:
        raise DownloadError('La respuesta contiene varios enlaces next.')
    if not links:
        return None
    target = safe_url(urllib.parse.urljoin(current, links[0]))
    a, b = urllib.parse.urlsplit(initial), urllib.parse.urlsplit(target)
    if a.path != b.path:
        raise DownloadError('El enlace next cambió de colección.')
    expected = dict(urllib.parse.parse_qsl(a.query))
    actual = dict(urllib.parse.parse_qsl(b.query))
    for k, v in expected.items():
        if k not in ('f', 'limit') and actual.get(k) != v:
            raise DownloadError(f'El enlace next perdió o cambió el filtro {k}.')
    return target


def fetch_pages(client, folder, collection, params):
    """Cache persistente por consulta. Las páginas completas se reutilizan."""
    folder.mkdir(parents=True, exist_ok=True)
    url = safe_url(API + '/collections/' + collection + '/items?' +
                   urllib.parse.urlencode(params))
    control = folder / 'manifest.json'
    if control.exists():
        manifest = load_json(control)
        if manifest['initial_url'] != url:
            raise StopDownload('La consulta cambió dentro de una versión. Usar --version con otro nombre.')
    else:
        manifest = {'initial_url': url, 'created_utc': utc_now(), 'complete': False,
                    'pages': [], 'next_url': url, 'n_features': 0}
        save_json(control, manifest)
    for page in manifest['pages']:
        path = folder / page['file']
        if not path.exists() or file_digest(path) != page['sha256_gzip']:
            raise DownloadError(f'Archivo ausente o alterado: {path}. Revisar; no se sobrescribió.')
    seen_urls = {p['url'] for p in manifest['pages']}
    seen_content = {p['features_sha256'] for p in manifest['pages'] if p['n_features']}
    while not manifest['complete']:
        current = manifest['next_url']
        if current in seen_urls:
            raise DownloadError('Paginación circular detectada; consulta incompleta.')
        data, raw = client.get(current)
        if data.get('type') != 'FeatureCollection' or not isinstance(data.get('features'), list):
            raise DownloadError('No se recibió una FeatureCollection válida.')
        features = data['features']
        if 'numberReturned' in data and int(data['numberReturned']) != len(features):
            raise DownloadError('numberReturned no coincide con el número de registros.')
        following = next_url(data, current, url)
        if not features and following:
            raise DownloadError('Página vacía con continuación: no se declaró completa.')
        signature = digest(json.dumps(features, sort_keys=True).encode())
        if features and signature in seen_content:
            raise DownloadError('La API repitió una página; consulta incompleta.')
        matched = data.get('numberMatched')
        if isinstance(matched, int):
            if 'number_matched' in manifest and manifest['number_matched'] != matched:
                raise DownloadError('El total de la API cambió durante la consulta. Crear una nueva versión.')
            manifest['number_matched'] = matched
        total = manifest['n_features'] + len(features)
        if following is None and 'number_matched' in manifest and total != manifest['number_matched']:
            raise DownloadError('El total descargado no coincide con numberMatched.')
        name = f'page_{len(manifest["pages"]) + 1:05d}.json.gz'
        compressed = gzip.compress(raw, mtime=0)
        atomic_bytes(folder / name, compressed)
        manifest['pages'].append({'file': name, 'url': current,
            'retrieved_utc': utc_now(), 'api_timestamp': data.get('timeStamp'),
            'n_features': len(features), 'sha256_gzip': digest(compressed),
            'features_sha256': signature})
        manifest.update(next_url=following, complete=following is None,
                        n_features=total, updated_utc=utc_now())
        save_json(control, manifest)
        seen_urls.add(current)
        seen_content.add(signature)
        logging.info('%s: página %s, %s registros; total %s.', collection,
                     len(manifest['pages']), len(features), total)
    return manifest


def iter_features(folder, manifest):
    for page in manifest['pages']:
        with gzip.open(folder / page['file'], 'rt', encoding='utf-8') as f:
            data = json.load(f)
        for feature in data['features']:
            yield feature, page


def coordinates(feature):
    geom = feature.get('geometry') or {}
    coords = geom.get('coordinates') or []
    return coords[:2] if geom.get('type') == 'Point' and len(coords) >= 2 else ('', '')


def flatten_metadata(feature):
    row = dict(feature['properties'])
    row['time_series_id'] = str(row.get('id') or feature.get('id') or '')
    row['longitude'], row['latitude'] = coordinates(feature)
    row['geometry_json'] = feature.get('geometry')
    return row


def build_inventory(run, manifest, wanted_states):
    all_rows = [flatten_metadata(f) for f, _ in iter_features(run / 'raw/catalogo', manifest)]
    if not all_rows:
        raise StopDownload('Inventario vacío: comprobar consulta y disponibilidad; no se considera una descarga completa.')
    selected, excluded = [], []
    by_site = {}
    for row in all_rows:
        if row.get('parameter_code') != '72019' or row.get('data_type') != 'Daily values':
            raise StopDownload('La API no respetó el filtro de variable/tipo de datos.')
        if row.get('agency_code') != 'USGS' or row.get('site_type_code') != 'GW':
            raise StopDownload('La API no respetó el filtro de agencia/tipo de sitio.')
        state = STATE_BY_CODE.get(str(row.get('state_code', '')).zfill(2))
        if state not in wanted_states or row.get('country_code') != 'US':
            excluded.append(dict(row, motivo='fuera_de_estados_seleccionados_o_pais'))
            continue
        location = row['monitoring_location_id']
        if not isinstance(location, str) or not re.fullmatch(r'USGS-\d{8,15}', location):
            raise StopDownload('Identificador de pozo inesperado. Revisar el inventario.')
        site_no = location.removeprefix('USGS-')
        row.update(site_no=site_no, estado=state)
        selected.append(row)
        info = {k: row.get(k) for k in [
            'monitoring_location_id', 'site_no', 'estado', 'state_code', 'state_name',
            'monitoring_location_name', 'site_type_code', 'longitude', 'latitude',
            'aquifer_type_code', 'aquifer_code', 'national_aquifer_code',
            'well_constructed_depth', 'hole_constructed_depth', 'vertical_datum']}
        if location in by_site and by_site[location] != info:
            raise DownloadError(f'Metadatos contradictorios para {location}; revisar antes de descargar.')
        by_site[location] = info
    selected.sort(key=lambda r: (r['estado'], r['site_no'], r['time_series_id']))
    sites = sorted(by_site.values(), key=lambda r: (r['estado'], r['site_no']))
    save_csv(run / 'inventario_series.csv', selected)
    save_csv(run / 'inventario_pozos.csv', sites)
    save_csv(run / 'fuera_ambito.csv', excluded)
    return selected, sites


def export_daily(run, site, folder, manifest, start, end):
    rows, counts = [], Counter()
    natural_keys = set()
    repeated = 0
    for feature, page in iter_features(folder, manifest):
        p = feature['properties']
        if p.get('monitoring_location_id') != site['monitoring_location_id'] or p.get('parameter_code') != '72019':
            raise DownloadError('Se recibió una observación de otro pozo o parámetro.')
        when = p.get('time')
        if not isinstance(when, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', when):
            raise DownloadError('La fecha diaria no tiene el formato YYYY-MM-DD esperado.')
        if not start <= date.fromisoformat(when) <= end:
            raise DownloadError('Se recibió una fecha fuera del periodo solicitado.')
        ts = p.get('time_series_id')
        if not isinstance(ts, str) or not ts:
            raise DownloadError('Falta el identificador de serie; no se puede exportar con trazabilidad.')
        key = (ts, when)
        repeated += key in natural_keys
        natural_keys.add(key)
        lon, lat = coordinates(feature)
        row = dict(monitoring_location_id=p['monitoring_location_id'],
            site_no=site['site_no'], estado=site['estado'], time_series_id=ts,
            parameter_code=p['parameter_code'], statistic_id=p.get('statistic_id'),
            date=when, value_original=p.get('value'), unit_of_measure=p.get('unit_of_measure'),
            approval_status=p.get('approval_status'), qualifier=p.get('qualifier'),
            last_modified=p.get('last_modified'), feature_id=feature.get('id'),
            longitude=lon, latitude=lat,
            raw_page=(folder / page['file']).relative_to(run).as_posix(),
            retrieved_utc=page['retrieved_utc'])
        rows.append(row)
        counts[(cell(row['statistic_id']), cell(row['unit_of_measure']), cell(row['approval_status']))] += 1
    rows.sort(key=lambda r: (r['time_series_id'], r['date'], str(r['feature_id'])))
    out = run / 'series_raw' / site['estado'] / (site['monitoring_location_id'] + '.csv.gz')
    save_csv(out, rows, DAILY_FIELDS)
    stats = [dict(monitoring_location_id=site['monitoring_location_id'],
                  statistic_id=k[0], unit_of_measure=k[1], approval_status=k[2],
                  n_filas=v) for k, v in sorted(counts.items())]
    result = dict(monitoring_location_id=site['monitoring_location_id'],
        site_no=site['site_no'], estado=site['estado'],
        estado_descarga='ok' if rows else 'sin_datos_en_periodo',
        n_filas=len(rows), n_series=len({r['time_series_id'] for r in rows}),
        fecha_min=min((r['date'] for r in rows), default=''),
        fecha_max=max((r['date'] for r in rows), default=''),
        duplicados_clave_natural=repeated,
        archivo_csv=out.relative_to(run).as_posix(), sha256_csv_gz=file_digest(out),
        finalizado_utc=utc_now(), error='', estadisticos=stats)
    save_json(folder / 'resultado.json', result)
    if repeated:
        logging.warning('%s: %s claves serie-fecha repetidas; se conservaron para QA.',
                        site['monitoring_location_id'], repeated)
    return result


def update_reports(run, sites, results, attempted):
    status, stats = [], []
    for site in sites:
        result = results.get(site['monitoring_location_id'])
        if result:
            status.append({k: v for k, v in result.items() if k != 'estadisticos'})
            if result['estado_descarga'] in ('ok', 'sin_datos_en_periodo'):
                stats.extend(result.get('estadisticos', []))
        else:
            status.append(dict(monitoring_location_id=site['monitoring_location_id'],
                               site_no=site['site_no'], estado=site['estado'],
                               estado_descarga='pendiente'))
    counts = Counter(r['estado_descarga'] for r in status)
    save_csv(run / 'estado_descarga.csv', status)
    save_csv(run / 'resumen_estadisticos.csv', stats)
    summary = dict(actualizado_utc=utc_now(), pozos_inventariados=len(sites),
        pozos_programados_esta_ejecucion=attempted, conteos=dict(counts),
        filas_exportadas=sum(r.get('n_filas', 0) for r in status if r['estado_descarga'] == 'ok'),
        descarga_total_completa=(counts['pendiente'] == 0 and counts['error'] == 0 and len(sites) > 0),
        qa_aplicado=False)
    save_json(run / 'resumen.json', summary)
    return summary


def arguments(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--root', type=Path, default=ROOT)
    p.add_argument('--inicio', type=date.fromisoformat, default=date(1900, 1, 1))
    p.add_argument('--fin', type=date.fromisoformat, default=date(2025, 12, 31))
    p.add_argument('--estados', default=','.join(STATES))
    p.add_argument('--sitio', help='Un solo USGS-ID (prueba técnica opcional).')
    p.add_argument('--max-pozos', type=int, help='Límite de pozos; omitir para todos.')
    p.add_argument('--solo-inventario', action='store_true')
    p.add_argument('--version', help='Nombre de una descarga nueva o existente.')
    p.add_argument('--tamano-pagina', type=int, default=50000)
    p.add_argument('--pausa', type=float, default=4.0, help='Segundos entre peticiones (predeterminado: 4).')
    args = p.parse_args(argv)
    if args.inicio > args.fin:
        p.error('--inicio debe ser anterior o igual a --fin.')
    if args.max_pozos is not None and args.max_pozos < 1:
        p.error('--max-pozos debe ser positivo.')
    if not 1 <= args.tamano_pagina <= 50000 or args.pausa < 0:
        p.error('Tamaño de página permitido: 1..50000; pausa no negativa.')
    if args.sitio and not re.fullmatch(r'USGS-\d{8,15}', args.sitio):
        p.error('--sitio debe tener el formato USGS- seguido del identificador del pozo.')
    args.states = sorted({s.strip().upper() for s in args.estados.split(',')})
    if not args.states or set(args.states) - STATES.keys():
        p.error('--estados debe contener abreviaturas de los 50 estados, separadas por comas.')
    if args.version and not re.fullmatch(r'[A-Za-z0-9_-]+', args.version):
        p.error('--version solo admite letras, números, guiones y guiones bajos.')
    return args


def main(argv=None):
    args = arguments(argv)
    if os.name != 'posix':
        print('Ejecuta este script desde Ubuntu/WSL (python3), no desde Python de Windows.', file=sys.stderr)
        return 1
    import fcntl
    spec = dict(api=API, parameter_code='72019', data_type='Daily values',
                agency_code='USGS', site_type_code='GW', states=args.states,
                inicio=str(args.inicio), fin=str(args.fin), sitio=args.sitio,
                tamano_pagina=args.tamano_pagina, qa_aplicado=False, formato_script=1)
    spec_hash = digest(json.dumps(spec, sort_keys=True).encode())[:10]
    name = args.version or f'v1_{args.inicio:%Y%m%d}_{args.fin:%Y%m%d}_{spec_hash}'
    run = args.root.expanduser().resolve() / '01_descarga' / name
    run.mkdir(parents=True, exist_ok=True)
    (args.root / '02_qa').mkdir(parents=True, exist_ok=True)
    lock = (run / '.descarga.lock').open('a')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        print('Ya existe otra ejecución usando esta descarga.', file=sys.stderr)
        return 1
    try:
        logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s',
                            handlers=[logging.StreamHandler(), logging.FileHandler(run / 'descarga.log', encoding='utf-8')], force=True)
        config = run / 'configuracion.json'
        if config.exists() and load_json(config)['spec'] != spec:
            raise StopDownload('Cambió la configuración. Usa otro --version para evitar mezclar descargas.')
        if not config.exists():
            save_json(config, {'spec': spec, 'created_utc': utc_now()})
        key = os.environ.get('USGS_API_KEY', '').strip()
        if not key and sys.stdin.isatty():
            print('Obtén tu clave en https://api.waterdata.usgs.gov/signup/')
            key = getpass.getpass('API key (entrada oculta; no se guarda): ').strip()
        if not key:
            raise StopDownload('Falta USGS_API_KEY. Define la variable o ejecuta desde una terminal para introducirla.')
        client = Client(key, interval=args.pausa)
        with (run / 'ejecuciones.jsonl').open('a', encoding='utf-8') as f:
            f.write(json.dumps(dict(started_utc=utc_now(), script_version=SCRIPT_VERSION,
                script_sha256=file_digest(Path(__file__)), python=platform.python_version(),
                max_pozos=args.max_pozos, solo_inventario=args.solo_inventario)) + '\n')
        logging.info('Carpeta de esta descarga: %s', run)
        params = dict(f='json', parameter_code='72019', data_type='Daily values',
                      agency_code='USGS', site_type_code='GW', limit=args.tamano_pagina)
        if args.sitio:
            params['monitoring_location_id'] = args.sitio
        catalog = fetch_pages(client, run / 'raw/catalogo', 'combined-metadata', params)
        series, sites = build_inventory(run, catalog, set(args.states))
        logging.info('Inventario: %s pozos; %s series diarias (varios estadísticos posibles).', len(sites), len(series))
        if not sites:
            raise StopDownload('No hay pozos dentro del ámbito solicitado; revisar estados y sitio.')
        results = {}
        for site in sites:
            path = run / 'raw/diarios' / site['estado'] / site['monitoring_location_id'] / 'resultado.json'
            if path.exists():
                results[site['monitoring_location_id']] = load_json(path)
        selected = sites[:args.max_pozos] if args.max_pozos else sites
        update_reports(run, sites, results, 0 if args.solo_inventario else len(selected))
        if args.solo_inventario:
            logging.info('Inventario guardado. Aún no se descargaron valores diarios en esta ejecución.')
            return 0
        consecutive_errors = 0
        try:
            for i, site in enumerate(selected, 1):
                location = site['monitoring_location_id']
                folder = run / 'raw/diarios' / site['estado'] / location
                logging.info('[%s/%s] %s (%s)', i, len(selected), location, site['estado'])
                try:
                    query = dict(f='json', monitoring_location_id=location,
                        parameter_code='72019', datetime=f'{args.inicio}/{args.fin}', limit=args.tamano_pagina)
                    manifest = fetch_pages(client, folder, 'daily', query)
                    results[location] = export_daily(run, site, folder, manifest, args.inicio, args.fin)
                    consecutive_errors = 0
                except DownloadError as exc:
                    results[location] = dict(monitoring_location_id=location, site_no=site['site_no'],
                        estado=site['estado'], estado_descarga='error', n_filas=0, error=str(exc))
                    folder.mkdir(parents=True, exist_ok=True)
                    save_json(folder / 'resultado.json', results[location])
                    logging.error('%s: %s', location, exc)
                    consecutive_errors += 1
                    if isinstance(exc, StopDownload) or consecutive_errors >= 3:
                        raise StopDownload('Se detuvo la descarga; progreso conservado. Corregir el error y reanudar.') from None
                if i % 10 == 0 or i == len(selected):
                    update_reports(run, sites, results, len(selected))
        finally:
            summary = update_reports(run, sites, results, len(selected))
        logging.info('Resumen: %s', json.dumps(summary['conteos'], ensure_ascii=False))
        if summary['descarga_total_completa']:
            logging.info('Descarga de todo el inventario seleccionado terminada. QA todavía pendiente.')
        else:
            logging.info('Descarga parcial o con pendientes. Revisar estado_descarga.csv; repetir sin --max-pozos para todos.')
        return 2 if summary['conteos'].get('error', 0) else 0
    except KeyboardInterrupt:
        logging.warning('Interrumpido. Repite el mismo comando para reanudar las páginas guardadas.')
        return 130
    except (DownloadError, OSError, ValueError, KeyError) as exc:
        logging.error('%s', exc)
        return 1
    finally:
        lock.close()


if __name__ == '__main__':
    sys.exit(main())

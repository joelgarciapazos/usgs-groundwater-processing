#!/usr/bin/env python3
"""Shared utilities for the USGS groundwater QA workflow.

The study-specific QA rules implemented here are not USGS certifications. Python >=3.10; standard library only."""
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import io
import json
import math
import os
import re
import sys
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

VERSION = "1.0.0"
CODE_DIR = Path(__file__).resolve().parent
REPO_ROOT = CODE_DIR.parent
DEFAULT_ROOT = Path(os.environ.get('USGS_GW_ROOT', str(REPO_ROOT / 'data')))
RAW_FIELDS = ['monitoring_location_id', 'site_no', 'estado', 'time_series_id',
              'parameter_code', 'statistic_id', 'date', 'value_original',
              'unit_of_measure', 'approval_status', 'qualifier', 'last_modified',
              'feature_id', 'longitude', 'latitude', 'raw_page', 'retrieved_utc']
NORM_FIELDS = RAW_FIELDS + ['source_row', 'date_original', 'depth_to_water_m',
                          'normalization_errors']
QA_FIELDS = NORM_FIELDS + ['source_rows', 'feature_ids', 'n_source_rows',
                          'qualifiers_source', 'source_estimated_flag',
                          'qa_flags', 'requires_review', 'interpolated_flag']
AUDIT_FIELDS = ['monitoring_location_id', 'site_no', 'time_series_id',
                'date', 'source_row', 'feature_id', 'reason', 'details']
TYPE_NAMES = {'U': 'Unconfined single aquifer', 'N': 'Unconfined multiple aquifer',
              'C': 'Confined single aquifer', 'M': 'Confined multiple aquifers',
              'X': 'Mixed (confined and unconfined) multiple aquifers'}


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def load_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def save_json(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp')
    with tmp.open('w', encoding='utf-8') as f:
        json.dump(obj, f, indent=2, ensure_ascii=False, allow_nan=False)
        f.write('\n')
    tmp.replace(path)


def read_csv(path, required=()):
    path = Path(path)
    opener = gzip.open if path.suffix == '.gz' else open
    with opener(path, 'rt', encoding='utf-8-sig', newline='') as f:
        reader = csv.DictReader(f)
        if not set(required).issubset(reader.fieldnames or []):
            raise ValueError(f'Faltan columnas en {path}: {set(required) - set(reader.fieldnames or [])}')
        for row in reader:
            if None in row:
                raise ValueError(f'CSV con columnas adicionales sin encabezado: {path}')
            yield {k: (v if v is not None else '') for k, v in row.items()}


@contextmanager
def csv_writer(path, fields):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp')
    try:
        with tmp.open('wb') as raw:
            if path.suffix == '.gz':
                binary = gzip.GzipFile(fileobj=raw, mode='wb', filename='', mtime=0,
                                       compresslevel=3)
            else:
                binary = raw
            with io.TextIOWrapper(binary, encoding='utf-8-sig', newline='') as f:
                writer = csv.DictWriter(f, fieldnames=fields, extrasaction='raise')
                writer.writeheader()
                yield writer
        tmp.replace(path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def save_csv(path, rows, fields=None):
    rows = list(rows)
    fields = fields or sorted({key for row in rows for key in row}) or ['sin_registros']
    with csv_writer(path, fields) as writer:
        writer.writerows(rows)


def safe_child(root, relative):
    path = (Path(root) / relative).resolve()
    if not path.is_relative_to(Path(root).resolve()):
        raise ValueError(f'Ruta fuera de la carpeta de entrada: {relative}')
    return path


def decimal_value(text):
    try:
        v = Decimal(str(text).strip())
    except InvalidOperation:
        raise ValueError('valor_no_numerico') from None
    if not v.is_finite() or not math.isfinite(float(v)):
        raise ValueError('valor_no_finito')
    return v


def metres(value, unit):
    factors = {'ft': Decimal('0.3048'), 'm': Decimal('1')}
    unit = unit.strip().lower()
    if unit not in factors:
        raise ValueError('unidad_no_admitida')
    return format(decimal_value(value) * factors[unit], 'f')


def valid_id(location, site):
    return bool(re.fullmatch(r'USGS-[0-9]{8,15}', location)
                and location == 'USGS-' + site)


def unique_index(rows, key, description):
    out = {}
    for row in rows:
        value = row[key].strip()
        if not value or value in out:
            raise ValueError(f'Identificador vacío/repetido en {description}: {value}')
        out[value] = row
    return out


class Context:
    def __init__(self, description):
        parser = argparse.ArgumentParser(description=description)
        parser.add_argument('--root', type=Path, default=DEFAULT_ROOT)
        parser.add_argument('--descarga', help='Nombre de subcarpeta en 01_descarga o ruta completa')
        parser.add_argument('--config', type=Path, default=REPO_ROOT / 'config' / 'qa.json')
        self.args = parser.parse_args()
        self.root = self.args.root.expanduser().resolve()
        if self.args.descarga:
            p = Path(self.args.descarga).expanduser()
            self.source = p.resolve() if p.is_absolute() else safe_child(self.root / '01_descarga', str(p))
        else:
            candidates = [p.parent for p in (self.root / '01_descarga').glob('*/resumen.json')
                          if load_json(p).get('descarga_total_completa') is True]
            if len(candidates) != 1:
                raise ValueError('Indica --descarga NOMBRE: se requiere una única descarga completa; '
                                 f'se encontraron {len(candidates)}.')
            self.source = candidates[0].resolve()
        self.cfg = load_json(self.args.config)
        expected = {'parameter_code', 'statistic_id', 'aquifer_codes_included', 'estimated_policy',
                    'series_selection_csv', 'jump_min_pairs', 'jump_mad_multiplier',
                    'jump_floor_m', 'well_depth_tolerance_m'}
        if set(self.cfg) != expected:
            raise ValueError('La configuración no tiene exactamente las claves esperadas.')
        if self.cfg['parameter_code'] != '72019' or self.cfg['statistic_id'] != '00003':
            raise ValueError('Este flujo está definido para 72019 y la media diaria 00003, como texto.')
        if not self.cfg['aquifer_codes_included'] or not set(self.cfg['aquifer_codes_included']) <= {'U', 'N'}:
            raise ValueError('Los códigos de acuíferos libres admitidos son U y N.')
        if self.cfg['estimated_policy'] not in {'flag', 'exclude'}:
            raise ValueError('estimated_policy debe ser flag o exclude.')
        for name in ['jump_mad_multiplier', 'jump_floor_m', 'well_depth_tolerance_m']:
            if not isinstance(self.cfg[name], (float, int)) or not math.isfinite(self.cfg[name]) or self.cfg[name] < 0:
                raise ValueError(f'Umbral inválido: {name}')
        if not isinstance(self.cfg['jump_min_pairs'], int) or self.cfg['jump_min_pairs'] < 3:
            raise ValueError('jump_min_pairs debe ser un entero >= 3.')
        summary = load_json(self.source / 'resumen.json')
        if summary.get('descarga_total_completa') is not True:
            raise ValueError('Primero debe terminar la descarga.')
        self.status = unique_index(read_csv(self.source / 'estado_descarga.csv',
                                           ['monitoring_location_id', 'site_no', 'estado_descarga',
                                            'n_filas', 'archivo_csv', 'sha256_csv_gz']),
                                   'monitoring_location_id', 'estado_descarga')
        self.wells = unique_index(read_csv(self.source / 'inventario_pozos.csv',
                                          ['monitoring_location_id', 'site_no', 'estado', 'aquifer_type_code']),
                                  'monitoring_location_id', 'inventario_pozos')
        self.series = unique_index(read_csv(self.source / 'inventario_series.csv',
                                           ['time_series_id', 'monitoring_location_id', 'parameter_code',
                                            'statistic_id', 'aquifer_type_code']),
                                   'time_series_id', 'inventario_series')
        self.series_by_location = defaultdict(list)
        for series in self.series.values():
            self.series_by_location[series['monitoring_location_id']].append(series)
        if set(self.status) != set(self.wells) or len(self.wells) != summary['pozos_inventariados']:
            raise ValueError('El inventario de pozos y el estado/resumen no coinciden.')
        if sum(int(r['n_filas']) for r in self.status.values()) != summary['filas_exportadas']:
            raise ValueError('Las filas del estado no coinciden con el resumen.')
        for mid, row in self.wells.items():
            if not valid_id(mid, row['site_no']) or not re.fullmatch('[A-Z]{2}', row['estado']):
                raise ValueError(f'Identificador o estado inválido en inventario: {mid}')
            if self.status[mid]['site_no'] != row['site_no']:
                raise ValueError(f'Identificador inconsistente en estado: {mid}')
            if self.status[mid]['estado_descarga'] not in {'ok', 'sin_datos_en_periodo'}:
                raise ValueError(f'Descarga sin completar: {mid}')
        self.manual = {}
        source_hashes = {name: digest(self.source / name) for name in
                         ['estado_descarga.csv', 'inventario_pozos.csv', 'inventario_series.csv']}
        if self.cfg['series_selection_csv']:
            path = Path(self.cfg['series_selection_csv']).expanduser()
            if not path.is_absolute():
                path = self.args.config.resolve().parent / path
            self.manual = unique_index(read_csv(path, ['monitoring_location_id', 'time_series_id', 'justification']),
                                       'monitoring_location_id', 'selección manual')
            for mid, row in self.manual.items():
                if mid not in self.wells or not row['justification'].strip():
                    raise ValueError(f'Selección manual sin justificación o pozo desconocido: {mid}')
                meta = self.series.get(row['time_series_id'], {})
                if (meta.get('monitoring_location_id') != mid or meta.get('statistic_id') != '00003'
                        or meta.get('parameter_code') != '72019'):
                    raise ValueError(f'Selección manual no es una serie de medias válida para {mid}')
            source_hashes['seleccion_manual'] = digest(path)
        code_names = ['qa_common.py', '02_normalize.py',
                      '03_quality_control.py', '04_select_unconfined.py']
        code_hashes = {name: digest(CODE_DIR / name) for name in code_names}
        self.identity = {'version': VERSION, 'config': self.cfg, 'source_hashes': source_hashes,
                         'code_hashes': code_hashes}
        token = hashlib.sha256(json.dumps(self.identity, sort_keys=True).encode()).hexdigest()[:12]
        self.home = self.root / '02_qa' / self.source.name / ('qa_' + token)
        self.home.mkdir(parents=True, exist_ok=True)
        save_json(self.home / 'configuracion_y_procedencia.json',
                  dict(self.identity, descarga=str(self.source)))
        print(f'Descarga: {self.source}\nResultados: {self.home}', flush=True)

    def records(self, stage):
        summary = load_json(self.home / stage / 'resumen.json')
        if summary.get('etapa_completa') is not True:
            raise ValueError(f'Completa primero la etapa {stage}.')
        for mid in self.wells:
            rec = load_json(self.home / stage / 'progreso' / (mid + '.json'))
            if not self.check_record(rec, self.home / stage):
                raise ValueError(f'Archivo modificado o incompleto: {stage}/{mid}. Repite esa etapa.')
            yield rec

    @staticmethod
    def check_record(rec, stage_dir):
        if rec.get('status') != 'ok':
            return False
        for relative, h in rec.get('output_hashes', {}).items():
            path = safe_child(stage_dir, relative)
            if not path.is_file() or digest(path) != h:
                return False
        return True


@contextmanager
def run_lock(home):
    """Bloqueo real de WSL/Linux; se libera incluso al interrumpir el proceso."""
    try:
        import fcntl
    except ImportError:
        raise RuntimeError('Ejecuta estos scripts con Python de Ubuntu/WSL, no de Windows.') from None
    with (home / '.ejecucion.lock').open('a') as f:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('Ya hay otra etapa ejecutándose sobre esta versión.') from None
        try:
            yield
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def process_stage(ctx, stage, worker, inputs=None):
    """Checkpoint por pozo; errores técnicos se reintentan al repetir la orden."""
    directory = ctx.home / stage
    directory.mkdir(parents=True, exist_ok=True)
    out = []
    records_in = inputs if inputs is not None else ctx.status.values()
    started = now()
    save_json(directory / 'resumen.json', {'etapa_completa': False, 'inicio_utc': started})
    for i, previous in enumerate(records_in, 1):
        mid = previous['monitoring_location_id']
        checkpoint = directory / 'progreso' / (mid + '.json')
        old = load_json(checkpoint) if checkpoint.exists() else None
        # Normalización comprueba SIEMPRE el archivo original, incluso al reanudar.
        if stage == '01_normalizados' and old:
            source_path = safe_child(ctx.source, previous['archivo_csv'])
            if not source_path.is_file() or digest(source_path) != previous['sha256_csv_gz']:
                old = None
        if old and ctx.check_record(old, directory):
            rec = old
        else:
            try:
                rec = worker(ctx, directory, previous)
                paths = rec.pop('_outputs', [])
                rec.update(status='ok', monitoring_location_id=mid,
                           site_no=ctx.wells[mid]['site_no'], estado=ctx.wells[mid]['estado'],
                           output_hashes={str(p.relative_to(directory)): digest(p) for p in paths},
                           finalizado_utc=now())
            except Exception as exc:
                rec = {'status': 'error', 'monitoring_location_id': mid,
                       'site_no': ctx.wells[mid]['site_no'], 'estado': ctx.wells[mid]['estado'],
                       'error': f'{type(exc).__name__}: {exc}', 'finalizado_utc': now()}
                print(f'  ERROR {mid}: {rec["error"]}', flush=True)
            save_json(checkpoint, rec)
        out.append(rec)
        if i % 25 == 0 or i == len(ctx.wells):
            print(f'{stage}: {i}/{len(ctx.wells)} pozos', flush=True)
    counts = Counter(r['status'] for r in out)
    reasons = Counter()
    for r in out:
        reasons.update(r.get('row_reasons', {}))
    summary = {'etapa': stage, 'etapa_completa': counts['error'] == 0 and len(out) == len(ctx.wells),
               'inicio_utc': started, 'finalizado_utc': now(), 'pozos_esperados': len(ctx.wells),
               'pozos_procesados': len(out), 'estados_proceso': dict(counts),
               'conteos_decision_pozo': dict(Counter(r.get('decision', 'error_tecnico') for r in out)),
               'filas_entrada': sum(r.get('n_input', 0) for r in out),
               'filas_salida': sum(r.get('n_output', 0) for r in out),
               'motivos_filas': dict(reasons)}
    fields = ['monitoring_location_id', 'site_no', 'estado', 'status', 'decision', 'reason',
              'n_input', 'n_output', 'selected_time_series_id', 'archivo_csv', 'error']
    save_csv(directory / 'estado_etapa.csv', [{k: r.get(k, '') for k in fields} for r in out], fields)
    save_json(directory / 'resumen.json', summary)
    print(json.dumps(summary, indent=2, ensure_ascii=False), flush=True)
    if not summary['etapa_completa']:
        raise RuntimeError('Hay errores técnicos. Revisa estado_etapa.csv y repite esta etapa.')
    return out


def temporal_metrics(rows):
    if not rows:
        return {'n_days': 0}
    days = sorted(date.fromisoformat(r['date']) for r in rows)
    if len(days) != len(set(days)):
        raise ValueError('Se intentaron calcular métricas con fechas repetidas.')
    span = (days[-1] - days[0]).days
    gaps = [(b - a).days - 1 for a, b in zip(days, days[1:]) if (b - a).days > 1]
    values = [float(r['depth_to_water_m']) for r in rows]
    return {'n_days': len(days), 'date_min': days[0].isoformat(), 'date_max': days[-1].isoformat(),
            'elapsed_days': span, 'duration_years': round(span / 365.25, 6),
            'expected_days': span + 1, 'missing_days': span + 1 - len(days),
            'completeness': round(len(days) / (span + 1), 8),
            'n_gaps': len(gaps), 'max_gap_missing_days': max(gaps, default=0),
            'min_depth_m': min(values), 'max_depth_m': max(values),
            'mean_depth_m': sum(values) / len(values)}


def entry(main):
    try:
        main()
    except KeyboardInterrupt:
        print('\nInterrumpido. Repite el mismo comando para reanudar.', file=sys.stderr)
        sys.exit(130)
    except Exception as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        sys.exit(1)

"""Normalize identifiers, ISO daily dates, and groundwater depth to metres.

No Approved-status or statistic filtering is applied here. Invalid rows are retained with normalization_errors so exclusion occurs explicitly in the QA stage.
"""
from datetime import date
import re

from qa_common import (
    Context, NORM_FIELDS, RAW_FIELDS, csv_writer, digest, entry,
    metres, process_stage, read_csv, run_lock, safe_child,
)
from collections import Counter


def normalize_row(source, number, well):
    row = {k: source[k] for k in RAW_FIELDS}
    row.update(source_row=str(number), date_original=source['date'], depth_to_water_m='')
    errors = []
    for key in ['monitoring_location_id', 'site_no', 'estado', 'time_series_id',
                'parameter_code', 'statistic_id']:
        row[key] = row[key].strip()
    for key in ['monitoring_location_id', 'site_no', 'estado']:
        if row[key] != well[key]:
            errors.append(key + '_inconsistente')
    if not row['time_series_id']:
        errors.append('time_series_id_vacio')
    for key in ['parameter_code', 'statistic_id']:
        if not re.fullmatch(r'[0-9]{5}', row[key]):
            errors.append(key + '_invalido')
    row['date'] = row['date'].strip()
    try:
        if not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', row['date']):
            raise ValueError
        row['date'] = date.fromisoformat(row['date']).isoformat()
    except ValueError:
        errors.append('fecha_diaria_invalida')
    # Una fecha diaria no se desplaza a UTC ni se interpreta día/mes ambiguo.
    if not row['value_original'].strip():
        errors.append('valor_ausente')
    else:
        try:
            row['depth_to_water_m'] = metres(row['value_original'], row['unit_of_measure'])
        except ValueError as exc:
            errors.append(str(exc))
    row['normalization_errors'] = '|'.join(errors)
    return row


def normalize_well(ctx, directory, source_record):
    mid = source_record['monitoring_location_id']
    well = ctx.wells[mid]
    source = safe_child(ctx.source, source_record['archivo_csv'])
    if digest(source) != source_record['sha256_csv_gz']:
        raise ValueError('El SHA-256 del CSV original no coincide con estado_descarga.csv.')
    target = directory / 'series' / well['estado'] / (mid + '.csv.gz')
    n = bad = 0
    units, errors, approvals, statistics = Counter(), Counter(), Counter(), Counter()
    with csv_writer(target, NORM_FIELDS) as writer:
        for n, original in enumerate(read_csv(source, RAW_FIELDS), 1):
            row = normalize_row(original, n, well)
            writer.writerow(row)
            units[original['unit_of_measure']] += 1
            approvals[original['approval_status']] += 1
            statistics[row['statistic_id']] += 1
            if row['normalization_errors']:
                bad += 1
                errors.update(row['normalization_errors'].split('|'))
    if n != int(source_record['n_filas']):
        raise ValueError('El número de filas no coincide con estado_descarga.csv.')
    return {'decision': 'normalizado' if n else 'sin_datos_en_periodo',
            'n_input': n, 'n_output': n, 'n_invalid': bad, 'normalization_error_counts': dict(errors),
            'unit_counts': dict(units), 'approval_counts': dict(approvals),
            'statistic_counts': dict(statistics),
            'archivo_csv': str(target.relative_to(directory)), '_outputs': [target]}


def main():
    ctx = Context(__doc__)
    with run_lock(ctx.home):
        records = process_stage(ctx, '01_normalizados', normalize_well)
        from qa_common import load_json, save_json, save_csv
        path = ctx.home / '01_normalizados' / 'resumen.json'
        summary = load_json(path)
        summary['filas_con_error_normalizacion'] = sum(r['n_invalid'] for r in records)
        for name in ['unit_counts', 'approval_counts', 'statistic_counts', 'normalization_error_counts']:
            counts = Counter()
            for rec in records:
                counts.update(rec[name])
            summary[name] = dict(counts)
        save_json(path, summary)
        # Inventario sin inferencias de confinamiento; unidades constructivas USGS: pies.
        inventory = []
        for well in ctx.wells.values():
            row = dict(well)
            flags = []
            for old, new in [('well_constructed_depth', 'well_constructed_depth_m'),
                             ('hole_constructed_depth', 'hole_constructed_depth_m')]:
                row[new] = ''
                if row.get(old, '').strip():
                    try:
                        row[new] = metres(row[old], 'ft')
                    except ValueError:
                        flags.append(old + '_invalido')
            row['metadata_flags'] = '|'.join(flags)
            inventory.append(row)
        save_csv(ctx.home / '01_normalizados' / 'inventario_pozos_normalizado.csv', inventory)
        print(f'Filas con errores de normalización: {summary["filas_con_error_normalizacion"]}')


if __name__ == '__main__':
    entry(main)

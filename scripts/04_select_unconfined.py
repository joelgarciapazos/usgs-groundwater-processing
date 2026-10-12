"""Select unconfined wells from USGS metadata with a well-level audit trail.

By default the public release uses aquifer type code U (unconfined single aquifer). Confinement is not inferred from groundwater-depth sign."""
import shutil
from collections import Counter

from qa_common import (
    Context, TYPE_NAMES, entry, metres, process_stage, run_lock, save_csv, save_json, load_json,
)


def aquifer_decision(well, series_metadata, included):
    site_code = well.get('aquifer_type_code', '').strip().upper()
    catalog_codes = {s.get('aquifer_type_code', '').strip().upper() for s in series_metadata}
    codes = (catalog_codes | {site_code}) - {''}
    if len(codes) > 1:
        return '', 'pendiente_revision', 'clasificacion_acuifero_contradictoria', sorted(codes)
    if not site_code:
        return '', 'excluido', 'clasificacion_ausente_en_inventario_pozo', sorted(codes)
    if site_code not in TYPE_NAMES:
        return site_code, 'excluido', 'codigo_acuifero_no_reconocido', sorted(codes)
    if site_code in included:
        return site_code, 'incluido', 'acuifero_libre_dentro_del_alcance', sorted(codes)
    reasons = {'C': 'acuifero_confinado_unico', 'M': 'acuiferos_confinados_multiples',
               'N': 'multiples_acuiferos_libres_fuera_del_alcance_U',
               'U': 'acuifero_libre_unico_fuera_del_alcance_configurado',
               'X': 'acuiferos_mixtos_confinados_y_libres'}
    return site_code, 'excluido', reasons[site_code], sorted(codes)


def select_well(ctx, directory, previous):
    mid = previous['monitoring_location_id']
    well = ctx.wells[mid]
    code, aq_decision, aq_reason, codes = aquifer_decision(
        well, ctx.series_by_location[mid], ctx.cfg['aquifer_codes_included'])
    qa_ok = previous['decision'] == 'candidata_qa'
    included = qa_ok and aq_decision == 'incluido'
    outputs = []
    file_out = ''
    if included:
        original = ctx.home / '02_qa' / previous['archivo_csv']
        target = directory / 'series' / well['estado'] / (mid + '.csv.gz')
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + '.tmp')
        shutil.copyfile(original, temporary)
        temporary.replace(target)
        outputs.append(target)
        file_out = str(target.relative_to(directory))
    all_reasons = []
    if not qa_ok:
        all_reasons.append(previous['decision'] + ':' + previous['reason'])
    if aq_decision != 'incluido':
        all_reasons.append(aq_reason)
    decision = ('incluido_candidato_libre' if included else
                'pendiente_revision' if previous['decision'] == 'pendiente_seleccion'
                or aq_decision == 'pendiente_revision' else 'excluido')
    return {'decision': decision, 'reason': '|'.join(all_reasons) or aq_reason,
            'qa_decision': previous['decision'], 'qa_reason': previous['reason'],
            'aquifer_type_code_checked': code, 'aquifer_type_description': TYPE_NAMES.get(code, ''),
            'aquifer_decision': aq_decision, 'aquifer_reason': aq_reason,
            'aquifer_codes_found': '|'.join(codes),
            'n_input': previous['n_output'], 'n_output': previous['n_output'] if included else 0,
            'selected_time_series_id': previous['selected_time_series_id'],
            'n_review_days': previous['n_review_days'], 'n_estimated_days': previous['n_estimated_days'],
            'n_negative_days': previous['n_negative_days'], 'metrics': previous['metrics'],
            'no_blocking_review_flags': int(included and previous['n_review_days'] == 0),
            'archivo_csv': file_out, '_outputs': outputs}


def main():
    ctx = Context(__doc__)
    with run_lock(ctx.home):
        records = process_stage(ctx, '03_libres', select_well, ctx.records('02_qa'))
        directory = ctx.home / '03_libres'
        table = []
        for rec in records:
            row = dict(ctx.wells[rec['monitoring_location_id']])
            row.update({k: v for k, v in rec.items() if k not in {'output_hashes', 'metrics'}})
            row.update(rec['metrics'])
            for old, new in [('well_constructed_depth', 'well_constructed_depth_m'),
                             ('hole_constructed_depth', 'hole_constructed_depth_m')]:
                row[new] = ''
                if row.get(old, '').strip():
                    try:
                        row[new] = metres(row[old], 'ft')
                    except ValueError:
                        pass
            table.append(row)
        fields = sorted({k for row in table for k in row})
        save_csv(directory / 'auditoria_todos_los_pozos.csv', table, fields)
        save_csv(directory / 'pozos_libres_candidatos.csv',
                 [row for row in table if row['decision'] == 'incluido_candidato_libre'], fields)
        save_csv(directory / 'exclusiones_y_pendientes.csv',
                 [row for row in table if row['decision'] != 'incluido_candidato_libre'], fields)
        save_csv(directory / 'pozos_N_libres_multiples.csv',
                 [row for row in table if row['aquifer_type_code_checked'] == 'N'], fields)
        summary = load_json(directory / 'resumen.json')
        summary.update(aquifer_codes_included=ctx.cfg['aquifer_codes_included'],
                       clasificacion_inventario=dict(Counter(w['aquifer_type_code'].strip().upper()
                                                            or 'SIN_CLASIFICACION' for w in ctx.wells.values())),
                       decisiones_clasificacion=dict(Counter(r['aquifer_reason'] for r in records)),
                       pozos_candidatos_con_marcas_de_revision=sum(
                           r['decision'] == 'incluido_candidato_libre' and r['n_review_days'] > 0 for r in records),
                       pozos_candidatos_sin_marcas_de_revision=sum(r['no_blocking_review_flags'] for r in records),
                       nota='Producto intermedio. No se aplicaron duración mínima, segmentación ni interpolación.')
        save_json(directory / 'resumen.json', summary)
        print(f'Inventario de candidatos: {directory / "pozos_libres_candidatos.csv"}')


if __name__ == '__main__':
    entry(main)

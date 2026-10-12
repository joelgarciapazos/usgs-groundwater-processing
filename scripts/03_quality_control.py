#!/usr/bin/env python3
"""Apply QA to USGS approved daily-mean groundwater-depth records.

The workflow restricts processing to parameter 72019 and statistic 00003, handles duplicate records deterministically, selects the candidate daily-mean series, and flags potential anomalies without automatically removing hydrological extremes."""
from collections import Counter, defaultdict
from datetime import date
from decimal import Decimal
import json
import math
import re
from statistics import median

from qa_common import (
    AUDIT_FIELDS, Context, NORM_FIELDS, QA_FIELDS, csv_writer, entry, metres,
    process_stage, read_csv, run_lock, save_csv, save_json, load_json, temporal_metrics,
)


def is_estimated(text):
    # Modern API: ESTIMATED; e se reconoce para datos antiguos conservados como calificadores.
    return bool(re.search(r'(?i)(?<![a-z])(estimated|e)(?![a-z])', text))


def qualifier_tokens(text):
    if not text.strip():
        return []
    try:
        obj = json.loads(text)
    except (ValueError, TypeError):
        obj = text
    if obj is None:
        return []
    values = obj if isinstance(obj, list) else [obj]
    return [str(v).strip().upper() for v in values if v is not None and str(v).strip()]


def add_flag(row, flag, review=False):
    flags = set(filter(None, row['qa_flags'].split('|')))
    flags.add(flag)
    row['qa_flags'] = '|'.join(sorted(flags))
    if review:
        row['requires_review'] = '1'


def flag_anomalies(rows, well, cfg):
    well_depth = None
    if well.get('well_constructed_depth', '').strip():
        try:
            converted = float(metres(well['well_constructed_depth'], 'ft'))
            if converted > 0:
                well_depth = converted
        except ValueError:
            pass
    missing_depth = well_depth is None
    coord_ok = True
    try:
        lat, lon = float(well['latitude']), float(well['longitude'])
        coord_ok = math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180
    except (ValueError, KeyError):
        coord_ok = False
    for row in rows:
        v = float(row['depth_to_water_m'])
        if v < 0:
            add_flag(row, 'negative_depth_above_land_surface')
        if well_depth is not None and v > well_depth + cfg['well_depth_tolerance_m']:
            add_flag(row, 'depth_exceeds_reported_well_depth', review=True)
        if row['source_estimated_flag'] == '1':
            add_flag(row, 'source_estimated')
        tokens = [token for q in json.loads(row['qualifiers_source']) for token in qualifier_tokens(q)]
        if tokens:
            add_flag(row, 'source_qualifier_present')
        if any(token not in {'E', 'ESTIMATED'} for token in tokens):
            add_flag(row, 'source_qualifier_needs_review', review=True)
        if not coord_ok:
            add_flag(row, 'coordinates_missing_or_invalid', review=True)
    pairs = []
    for i in range(1, len(rows)):
        if (date.fromisoformat(rows[i]['date']) - date.fromisoformat(rows[i - 1]['date'])).days == 1:
            delta = float(rows[i]['depth_to_water_m']) - float(rows[i - 1]['depth_to_water_m'])
            pairs.append((i, delta))
    threshold = center = None
    if len(pairs) >= cfg['jump_min_pairs']:
        differences = [d for _, d in pairs]
        center = median(differences)
        mad = median(abs(d - center) for d in differences)
        threshold = max(cfg['jump_floor_m'], cfg['jump_mad_multiplier'] * 1.4826 * mad)
        for i, delta in pairs:
            if abs(delta - center) > threshold:
                # Se señalan AMBOS extremos; el algoritmo no sabe cuál es el anómalo.
                add_flag(rows[i - 1], 'unusual_consecutive_day_change', review=True)
                add_flag(rows[i], 'unusual_consecutive_day_change', review=True)
    return {'jump_n_consecutive_pairs': len(pairs), 'jump_center_m': center,
            'jump_threshold_m': threshold, 'well_depth_check_unavailable': missing_depth,
            'coordinates_valid': coord_ok}


def select_series(mid, candidates, ctx):
    if mid in ctx.manual:
        selected = ctx.manual[mid]['time_series_id']
        if selected not in candidates:
            return '', 'seleccion_manual_sin_datos_qa'
        return selected, 'seleccion_manual_documentada'
    if len(candidates) == 1:
        return next(iter(candidates)), 'unica_serie_con_medias_aprobadas'
    if not candidates:
        return '', 'sin_medias_aprobadas_utilizables'
    primary = [ts for ts in candidates if ctx.series[ts].get('primary', '').strip().lower() == 'primary']
    if len(primary) == 1:
        return primary[0], 'unica_serie_primary_entre_candidatas'
    return '', 'multiples_series_de_medias_requieren_revision'


def qa_well(ctx, directory, previous):
    mid = previous['monitoring_location_id']
    well = ctx.wells[mid]
    source = ctx.home / '01_normalizados' / previous['archivo_csv']
    sub = directory / 'series' / well['estado']
    target = sub / (mid + '.csv.gz')
    audit = directory / 'auditoria' / well['estado'] / (mid + '.csv.gz')
    candidates_file = directory / 'candidatas_por_serie' / well['estado'] / (mid + '.csv.gz')
    flags_file = directory / 'revision' / well['estado'] / (mid + '.csv.gz')
    n = 0
    reasons = Counter()
    buckets = defaultdict(list)
    with csv_writer(audit, AUDIT_FIELDS) as writer:
        def reject(row, reason, details=''):
            reasons[reason] += 1
            record = {k: row.get(k, '') for k in AUDIT_FIELDS}
            record.update(reason=reason, details=details)
            writer.writerow(record)

        for n, row in enumerate(read_csv(source, NORM_FIELDS), 1):
            if row['normalization_errors']:
                reject(row, 'normalizacion_invalida', row['normalization_errors'])
            elif row['parameter_code'] != ctx.cfg['parameter_code']:
                reject(row, 'parametro_fuera_del_estudio')
            elif row['statistic_id'] != ctx.cfg['statistic_id']:
                reject(row, 'estadistico_distinto_de_media', row['statistic_id'])
            elif row['approval_status'].strip().lower() != 'approved':
                approval = row['approval_status'].strip().lower()
                reject(row, 'registro_provisional' if approval == 'provisional'
                       else 'aprobacion_ausente_o_no_reconocida', row['approval_status'])
            else:
                ts = row['time_series_id']
                meta = ctx.series.get(ts)
                if meta is None:
                    reject(row, 'serie_ausente_del_catalogo')
                elif (meta['monitoring_location_id'] != mid
                      or meta['parameter_code'].strip() != row['parameter_code']
                      or meta['statistic_id'].strip() != row['statistic_id']):
                    reject(row, 'metadatos_serie_inconsistentes')
                elif ctx.cfg['estimated_policy'] == 'exclude' and is_estimated(row['qualifier']):
                    reject(row, 'estimado_excluido_por_configuracion')
                else:
                    buckets[(ts, row['date'])].append(row)
        candidates = defaultdict(list)
        representative_sources = {}
        for (ts, day), group in sorted(buckets.items()):
            if len({Decimal(r['depth_to_water_m']) for r in group}) > 1:
                for row in group:
                    reject(row, 'duplicado_conflictivo', 'Misma serie/fecha con distintos valores aprobados')
                continue
            group.sort(key=lambda r: int(r['source_row']))
            rep = dict(group[0])
            qualifiers = sorted({r['qualifier'] for r in group})
            rep.update(source_rows=json.dumps([r['source_row'] for r in group]),
                       feature_ids=json.dumps([r['feature_id'] for r in group]),
                       n_source_rows=str(len(group)), qualifiers_source=json.dumps(qualifiers),
                       source_estimated_flag=str(int(any(is_estimated(q) for q in qualifiers))),
                       qa_flags='', requires_review='0', interpolated_flag='0')
            if len(group) > 1:
                add_flag(rep, 'identical_value_duplicates_collapsed')
                for row in group[1:]:
                    reject(row, 'duplicado_valor_identico', 'Conservada source_row=' + rep['source_row'])
            representative_sources[(ts, day)] = group[0]
            candidates[ts].append(rep)
        diagnostics = {}
        for ts, rows in candidates.items():
            rows.sort(key=lambda r: r['date'])
            diagnostics[ts] = flag_anomalies(rows, well, ctx.cfg)
        selected, rule = select_series(mid, candidates, ctx)
        chosen = candidates.get(selected, [])
        for ts, rows in candidates.items():
            if ts != selected:
                reason = 'otra_serie_media_no_seleccionada' if selected else 'serie_media_pendiente_seleccion'
                for row in rows:
                    reject(representative_sources[(ts, row['date'])], reason, rule)
        # Conciliación exclusiva: cada fila de entrada es una salida o tiene UN motivo principal.
        if n != len(chosen) + sum(reasons.values()):
            raise RuntimeError('Error interno de conciliación de filas QA.')
    with csv_writer(target, QA_FIELDS) as writer:
        writer.writerows(chosen)
    with csv_writer(candidates_file, QA_FIELDS) as writer:
        for ts in sorted(candidates):
            writer.writerows(candidates[ts])
    with csv_writer(flags_file, QA_FIELDS) as writer:
        writer.writerows(row for row in chosen if row['qa_flags'])
    series_summary = []
    for ts, rows in sorted(candidates.items()):
        meta = ctx.series[ts]
        series_summary.append(dict(monitoring_location_id=mid, site_no=well['site_no'],
                                   time_series_id=ts, selected=int(ts == selected),
                                   primary=meta.get('primary', ''),
                                   aquifer_type_code=meta.get('aquifer_type_code', ''),
                                   parent_time_series_id=meta.get('parent_time_series_id', ''),
                                   sublocation_identifier=meta.get('sublocation_identifier', ''),
                                   n_flags=sum(bool(r['qa_flags']) for r in rows),
                                   n_requires_review=sum(r['requires_review'] == '1' for r in rows),
                                   **temporal_metrics(rows), **diagnostics[ts]))
    decision = ('candidata_qa' if chosen else 'pendiente_seleccion' if candidates else
                'sin_datos_en_periodo' if not n else 'excluido_qa')
    return {'decision': decision, 'reason': rule, 'n_input': n, 'n_output': len(chosen),
            'n_series_candidates': len(candidates), 'selected_time_series_id': selected,
            'selection_justification': ctx.manual.get(mid, {}).get('justification', ''),
            'row_reasons': dict(reasons), 'series_summary': series_summary,
            'n_flagged_days': sum(bool(r['qa_flags']) for r in chosen),
            'n_review_days': sum(r['requires_review'] == '1' for r in chosen),
            'n_estimated_days': sum(r['source_estimated_flag'] == '1' for r in chosen),
            'n_negative_days': sum(Decimal(r['depth_to_water_m']) < 0 for r in chosen),
            'metrics': temporal_metrics(chosen),
            'archivo_csv': str(target.relative_to(directory)),
            '_outputs': [target, audit, candidates_file, flags_file]}


def main():
    ctx = Context(__doc__)
    with run_lock(ctx.home):
        records = process_stage(ctx, '02_qa', qa_well, ctx.records('01_normalizados'))
        directory = ctx.home / '02_qa'
        fields = ['monitoring_location_id', 'site_no', 'estado', 'decision', 'reason',
                  'selected_time_series_id', 'selection_justification', 'n_input', 'n_output',
                  'n_series_candidates', 'n_flagged_days', 'n_review_days', 'n_estimated_days',
                  'n_negative_days', 'archivo_csv']
        inventory = []
        for rec in records:
            row = dict(ctx.wells[rec['monitoring_location_id']])
            row.update({k: rec.get(k, '') for k in fields})
            row.update(rec['metrics'])
            inventory.append(row)
        save_csv(directory / 'inventario_qa.csv', inventory)
        save_csv(directory / 'comparacion_series_medias.csv',
                 [row for rec in records for row in rec['series_summary']],
                 None if any(r['series_summary'] for r in records) else
                 ['monitoring_location_id', 'time_series_id', 'selected', 'n_days'])
        save_csv(directory / 'pozos_pendientes_seleccion.csv',
                 [row for row in inventory if row['decision'] == 'pendiente_seleccion'],
                 sorted({k for row in inventory for k in row}))
        summary = load_json(directory / 'resumen.json')
        summary.update(estadistico='00003: media diaria publicada por USGS',
                       filtrado_aprobacion='Approved',
                       dias_marcados_revision=sum(r['n_review_days'] for r in records),
                       pozos_con_dias_a_revisar=sum(r['n_review_days'] > 0 for r in records),
                       dias_estimados_fuente=sum(r['n_estimated_days'] for r in records),
                       dias_con_profundidad_negativa=sum(r['n_negative_days'] for r in records),
                       conciliacion_filas_correcta=(sum(r['n_input'] for r in records)
                           == sum(r['n_output'] + sum(r['row_reasons'].values()) for r in records)))
        save_json(directory / 'resumen.json', summary)
        print('QA terminado. Las anomalías marcadas requieren revisión antes de segmentar.')


if __name__ == '__main__':
    entry(main)

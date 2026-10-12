#!/usr/bin/env python3
"""Compare maximum internal-gap scenarios of 15, 30, and 45 days and export temporal segments. No interpolation is performed in this stage."""
from continuity_common import *
from collections import Counter
from statistics import median
SEG_FIELDS=['monitoring_location_id','site_no','estado','gap_scenario','segment_id','date_min','date_max','elapsed_days','duration_years_approx','n_days','expected_days','missing_days','completeness','max_gap_missing_days','eligible_duration','truth_days','estimated_days','jump_days','selected','segment_file','segment_sha256']

def main():
 args=parser(__doc__).parse_args();ex,c,m=experiment(args);require_stage(ex,'01_preparados')
 out=ex/'02_segmentacion';out.mkdir(parents=True,exist_ok=True)
 writejson(out/'resumen.json',dict(etapa_completa=False,inicio_utc=now()))
 wells=list(readcsv(ex/'01_preparados'/'inventario_preparado.csv'))
 allsegs=[];choices=[];gaps=[];status=[];outputs=[];cached={}
 for wi,w in enumerate(wells,1):
  rows=prepared(ex,w);mid=w['monitoring_location_id']
  for limit in c['gap_scenarios_days']:
   parts=segment(rows,limit);records=[];data={}
   for part in parts:
    met=metrics(part,c['min_years'])
    sid=hashlib.sha256((mid+'|'+part[0]['date']+'|'+part[-1]['date']+'|'+w['prepared_sha256']).encode()).hexdigest()[:20]
    rec={**{k:w[k] for k in ['monitoring_location_id','site_no','estado']},**met,'gap_scenario':limit,
         'segment_id':sid,'truth_days':sum(boolean(r['eligible_truth']) for r in part),
         'estimated_days':sum(boolean(r['source_estimated_effective']) for r in part),
         'jump_days':sum(boolean(r['jump_alert']) for r in part),'selected':0,'segment_file':'','segment_sha256':''}
    records.append(rec);data[sid]=part
   candidates=[r for r in records if r['eligible_duration']]
   # Jerarquia temporal fija; el desempeno de interpolacion no interviene.
   candidates.sort(key=lambda r:(-r['elapsed_days'],-r['completeness'],r['date_min'],r['segment_id']))
   if candidates:
    chosen=candidates[0];chosen['selected']=1;sid=chosen['segment_id'];part=data[sid]
    p=out/'segmentos'/f'{mid}_{sid}.csv.gz'
    if sid not in cached:
     writecsv(p,part,list(part[0]));cached[sid]=sha(p);outputs.append(p)
    chosen.update(segment_file=str(p.relative_to(ex)),segment_sha256=cached[sid]);choices.append(chosen)
    for a,b in zip(part,part[1:]):
     n=(date.fromisoformat(b['date'])-date.fromisoformat(a['date'])).days-1
     if n:gaps.append(dict(monitoring_location_id=mid,segment_id=sid,gap_scenario=limit,
                          left_date=a['date'],right_date=b['date'],missing_days=n))
   allsegs.extend(records)
   direct=bool(candidates) and len(parts)==1 and len(parts[0])==len(rows)
   status.append(dict(monitoring_location_id=mid,gap_scenario=limit,n_segments=len(records),
                     n_eligible_segments=len(candidates),selected=int(bool(candidates)),
                     origin='directo' if direct else ('recuperado_segmentacion' if candidates else 'sin_segmento_elegible')))
  if wi%25==0 or wi==len(wells):print(f'Segmentados {wi}/{len(wells)}',flush=True)
 summaries=[]
 for limit in c['gap_scenarios_days']:
  selected=[r for r in choices if r['gap_scenario']==limit];st=[r for r in status if r['gap_scenario']==limit]
  total=sum(r['expected_days'] for r in selected);miss=sum(r['missing_days'] for r in selected)
  summaries.append(dict(gap_days=limit,wells_evaluated=len(wells),wells_selected=len(selected),
    direct_wells=sum(r['origin']=='directo' for r in st),recovered_wells=sum(r['origin']=='recuperado_segmentacion' for r in st),
    wells_without_segment=sum(not r['selected'] for r in st),eligible_segments=sum(r['n_eligible_segments'] for r in st),
    median_duration_years=median(r['duration_years_approx'] for r in selected) if selected else '',
    observed_or_source_estimated_days=sum(r['n_days'] for r in selected),
    retained_source_estimated_days=sum(r['estimated_days'] for r in selected),
    eligible_truth_days=sum(r['truth_days'] for r in selected),expected_days=total,missing_days=miss,
    percent_to_interpolate=100*miss/total if total else '',
    median_completeness=median(r['completeness'] for r in selected) if selected else ''))
 for name,rs,fields in [('segmentos_todos.csv',allsegs,SEG_FIELDS),('segmentos_seleccionados.csv',choices,SEG_FIELDS),
   ('estado_por_pozo.csv',status,['monitoring_location_id','gap_scenario','n_segments','n_eligible_segments','selected','origin']),
   ('huecos_reales.csv',gaps,['monitoring_location_id','segment_id','gap_scenario','left_date','right_date','missing_days']),
   ('resumen_sensibilidad.csv',summaries,list(summaries[0]))]:
  p=out/name;writecsv(p,rs,fields);outputs.append(p)
 # Comparacion adicional sobre pozos comunes (las fechas de sus segmentos pueden cambiar).
 common=set.intersection(*[{r['monitoring_location_id'] for r in choices if r['gap_scenario']==g} for g in c['gap_scenarios_days']])
 common_rows=[r for r in choices if r['monitoring_location_id'] in common]
 p=out/'seleccion_pozos_comunes.csv';writecsv(p,common_rows,SEG_FIELDS);outputs.append(p)
 finish(ex,'02_segmentacion',dict(etapa_completa=True,tipo='EXPLORATORIO',qa_cerrado=False,
        final_gap_selected=False,unique_selected_segments=len(cached),common_wells=len(common),
        nota='Sensibilidad condicionada a politica de barreras. Conteos no fijan por si solos el gap definitivo.'),outputs)
 print('RESUMEN:')
 for r in summaries:print(r)
 print('Resultados:',out);print('Optional analysis: ../analysis/compare_interpolation_methods.py')
if __name__=='__main__':run(main)

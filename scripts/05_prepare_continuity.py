#!/usr/bin/env python3
"""Prepare QA-selected unconfined records for temporal continuity analysis.

The stage preserves source values, translates study QA flags into usable-input / validation-truth indicators, and records temporal barriers before segmentation."""
from continuity_common import *
from collections import Counter

def main():
 args=parser(__doc__,True).parse_args();home=args.carpeta.expanduser().resolve();c=config(args.config)
 inv=home/'03_libres'/'pozos_libres_candidatos.csv';wells=list(readcsv(inv))
 if len({w['monitoring_location_id'] for w in wells})!=len(wells):raise ValueError('Pozos duplicados')
 if any(w['aquifer_type_code_checked']!='U' or w['decision']!='incluido_candidato_libre' for w in wells):raise ValueError('Inventario fuera de alcance U')
 if args.pozos:
  unknown=set(args.pozos)-{w['monitoring_location_id'] for w in wells}
  if unknown:raise ValueError(f'Pozos desconocidos: {unknown}')
  wells=[w for w in wells if w['monitoring_location_id'] in set(args.pozos)]
 codes={n:sha(HERE/n) for n in SCRIPTS}
 spec=dict(carpeta_qa=str(home),inventory_sha256=sha(inv),config=c,script_hashes=codes,
           ids=[w['monitoring_location_id'] for w in wells],version=VERSION)
 fingerprint=hashlib.sha256(json.dumps(spec,sort_keys=True).encode()).hexdigest()[:16]
 ex=home/'07_continuidad_exploratoria'/('exp_'+fingerprint);stage=ex/'01_preparados';stage.mkdir(parents=True,exist_ok=True)
 writejson(ex/'manifiesto.json',spec)
 summary=dict(etapa_completa=False,inicio_utc=now(),tipo='EXPLORATORIO',qa_cerrado=False,
              min_years=c['min_years'],pozos_esperados=len(wells),regla_duracion='ultima_fecha >= aniversario de primera_fecha; Feb29->Feb28')
 writejson(stage/'resumen.json',summary)
 results=[];errors=[];outputs=[];counts=Counter()
 for i,w in enumerate(wells,1):
  try:
   rows,source,digest,verified=load_source(home,w)
   for r in rows:r.update(flags_policy(r,c))
   met=metrics(rows,c['min_years']);usable=[r for r in rows if r['usable_input']]
   target=stage/'series'/w['estado']/(w['monitoring_location_id']+'.csv.gz')
   writecsv(target,rows,list(rows[0]));outputs.append(target)
   barriers=sum(not r['usable_input'] for r in rows)
   group='extension_menor_minimo' if not met['eligible_duration'] else ('directo_gap30_sin_barreras' if not barriers and met['max_gap_missing_days']<=30 else 'requiere_segmentacion_o_revision')
   counts[group]+=1
   rec={**{k:w[k] for k in ['monitoring_location_id','site_no','estado']},**met,
     'grupo_exploratorio_gap30':group,'usable_days':len(usable),'barrier_days':barriers,
     'eligible_truth_days':sum(r['eligible_truth'] for r in rows),
     'estimated_days_retained':sum(r['source_estimated_effective'] and r['usable_input'] for r in rows),
     'jump_alert_days_retained':sum(r['jump_alert'] and r['usable_input'] for r in rows),
     'barrier_reasons_json':json.dumps(dict(Counter(r['barrier_reason'] for r in rows if r['barrier_reason'])),sort_keys=True),
     'source_file':str(source),'source_sha256':digest,'source_hash_verified':int(verified),
     'prepared_file':str(target.relative_to(ex)),'prepared_sha256':sha(target)}
   results.append(rec)
  except (ValueError,OSError,KeyError) as e:errors.append(dict(monitoring_location_id=w['monitoring_location_id'],error=str(e)))
  if i%25==0 or i==len(wells):print(f'Preparados {i}/{len(wells)}; errores {len(errors)}',flush=True)
 inventory=stage/'inventario_preparado.csv';err=stage/'errores.csv'
 writecsv(inventory,results,list(results[0]) if results else ['monitoring_location_id']);writecsv(err,errors,['monitoring_location_id','error']);outputs.extend([inventory,err])
 unchanged=sha(inv)==spec['inventory_sha256']
 summary.update(etapa_completa=not errors and unchanged,pozos_procesados=len(results),errores=len(errors),
                inventario_sin_cambios=unchanged,grupos=dict(counts),
                dias_barrera=sum(r['barrier_days'] for r in results),
                nota='Las barreras conservan valores originales pero impiden cruzarlos al segmentar. No son errores confirmados.')
 finish(ex,'01_preparados',summary,outputs)
 if not summary['etapa_completa']:raise ValueError(f'Preparacion incompleta: {err}')
 pointer=home/'07_continuidad_exploratoria'/'ultimo_experimento.txt';tmp=pointer.with_suffix('.tmp');tmp.write_text(str(ex)+'\n');tmp.replace(pointer)
 print('EXPERIMENTO:',ex);print(json.dumps(summary['grupos'],indent=2));print('Next: 06_gap_sensitivity_and_segmentation.py')
if __name__=='__main__':run(main)

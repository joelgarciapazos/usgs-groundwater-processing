#!/usr/bin/env python3
"""Optional paired comparison of linear, PCHIP, and Akima interpolation using temporal masking.

This analysis does not fill natural gaps and does not accept or reject final series; it supports method-selection evidence."""
import sys
from pathlib import Path
SCRIPTS_DIR = Path(__file__).resolve().parents[1] / 'scripts'
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
from continuity_common import *
from collections import defaultdict
import numpy as np
import scipy
from scipy.interpolate import PchipInterpolator, Akima1DInterpolator
METHODS=['lineal','pchip','akima']
MASK_FIELDS=['monitoring_location_id','segment_id','phase','rep','season_quarter','start_date','max_gap_days','end_date_max','guard_start','guard_end']
DETAIL_FIELDS=['monitoring_location_id','segment_id','rep','start_date','gap_days','season_quarter','method','success','paired_success','n_test','rmse_m','mae_m','bias_m','sse_m2','sae_m','sum_error_m','nse','std_ratio','error']
POINT_FIELDS=['monitoring_location_id','segment_id','rep','gap_days','method','date','observed_m','predicted_m','error_m']

def stencil_predict(x,y,start,length):
 """Solo predice el bloque ocultado. Tres vecinos por lado cubren las
 pendientes locales PCHIP/Akima en ambos extremos del hueco. No extrapola.
 x,y contienen observaciones elegibles para esta prueba, en dias ordinales.
 """
 test=np.arange(start,start+length,dtype=float)
 train=(x<start)|(x>=start+length)
 xx=x[train];yy=y[train]
 k=int(np.searchsorted(xx,start))
 if k<3 or len(xx)-k<3:raise ValueError('Faltan tres vecinos por lado')
 xx=xx[k-3:k+3];yy=yy[k-3:k+3]
 if xx[2]!=start-1 or xx[3]!=start+length:raise ValueError('Hueco no acotado por dias adyacentes')
 return {'lineal':np.interp(test,xx,yy),
         'pchip':PchipInterpolator(xx,yy,extrapolate=False)(test),
         'akima':Akima1DInterpolator(xx,yy,extrapolate=False)(test)}

def scores(truth,pred):
 err=pred-truth;sse=float(np.sum(err**2));den=float(np.sum((truth-np.mean(truth))**2))
 st=float(np.std(truth));n=len(truth)
 return dict(n_test=n,rmse_m=float(np.sqrt(sse/n)),mae_m=float(np.mean(np.abs(err))),
             bias_m=float(np.mean(err)),sse_m2=sse,sae_m=float(np.sum(np.abs(err))),sum_error_m=float(np.sum(err)),
             nse=1-sse/den if n>=2 and den>1e-24 else '',
             std_ratio=float(np.std(pred))/st if n>=2 and st>1e-12 else '')

def masks_for(rows,mid,sid,c):
 # Compartimentos temporales consistentes por pozo incluso entre escenarios:
 # 1/5 de los anos calendario reservado; las mascaras y vecinos no cruzan anos.
 ds=np.array([date.fromisoformat(r['date']).toordinal() for r in rows],dtype=int)
 first,last=int(ds[0]),int(ds[-1]);full=np.zeros(last-first+1,dtype=np.int32)
 for d,r in zip(ds,rows):full[d-first]=int(boolean(r['eligible_truth']))
 L=max(c['pilot_gap_lengths_days']);width=L+6
 if len(full)<width:return [],{'piloto':0,'reservada':0}
 sums=np.r_[0,np.cumsum(full)];starts=np.flatnonzero(sums[width:]-sums[:-width]==width)+first+3
 offset=int(hashlib.sha256(mid.encode()).hexdigest()[:8],16)%5
 pools={'piloto':defaultdict(list),'reservada':defaultdict(list)}
 for s in starts:
  left=date.fromordinal(int(s)-3);right=date.fromordinal(int(s)+L+2);d=date.fromordinal(int(s))
  if left.year!=right.year:continue
  phase='reservada' if left.year%5==offset else 'piloto'
  pools[phase][(d.month-1)//3+1].append(int(s))
 available={p:sum(map(len,groups.values())) for p,groups in pools.items()}
 result=[]
 for phase,target in [('piloto',c['pilot_repetitions']),('reservada',c['reserved_repetitions'])]:
  seed=int(hashlib.sha256(f'{c["seed"]}|{mid}|{sid}|{phase}'.encode()).hexdigest()[:16],16)
  rng=np.random.default_rng(seed);groups=pools[phase]
  for q in range(1,5):rng.shuffle(groups[q])
  picked=[]
  while len(picked)<target:
   added=False
   for q in range(1,5):
    while groups[q]:
     s=groups[q].pop()
     if all(abs(s-t)>=width for t in picked):
      picked.append(s);added=True
      result.append(dict(monitoring_location_id=mid,segment_id=sid,phase=phase,rep=len(picked),
        season_quarter=q,start_date=date.fromordinal(s).isoformat(),max_gap_days=L,
        end_date_max=date.fromordinal(s+L-1).isoformat(),guard_start=date.fromordinal(s-3).isoformat(),
        guard_end=date.fromordinal(s+L+2).isoformat()))
      break
    if len(picked)>=target:break
   if not added:break
 return result,available

def evaluate_one(ex,chosen,c,folder):
 sid=chosen['segment_id'];mid=chosen['monitoring_location_id'];p=ex/chosen['segment_file']
 if sha(p)!=chosen['segment_sha256']:raise ValueError('Segmento modificado')
 rows=list(readcsv(p));masks,avail=masks_for(rows,mid,sid,c)
 x=np.array([date.fromisoformat(r['date']).toordinal() for r in rows],dtype=float)
 y=np.array([float(r['depth_to_water_m']) for r in rows]);lookup={int(d):v for d,v in zip(x,y)}
 details=[];points=[]
 for mask in masks:
  if mask['phase']!='piloto':continue
  start=date.fromisoformat(mask['start_date']).toordinal()
  for length in c['pilot_gap_lengths_days']:
   truth=np.array([lookup[d] for d in range(start,start+length)])
   predictions={};errtext=''
   try:predictions=stencil_predict(x,y,start,length)
   except (ValueError,FloatingPointError) as e:errtext=str(e)
   good={m:m in predictions and np.isfinite(predictions[m]).all() for m in METHODS};paired=all(good.values())
   for method in METHODS:
    rec=dict(monitoring_location_id=mid,segment_id=sid,rep=mask['rep'],start_date=mask['start_date'],
             gap_days=length,season_quarter=mask['season_quarter'],method=method,
             success=int(good[method]),paired_success=int(paired),error=errtext or ('' if good[method] else 'prediccion_no_finita'))
    if good[method]:
     pred=predictions[method];rec.update(scores(truth,pred))
     points.extend(dict(monitoring_location_id=mid,segment_id=sid,rep=mask['rep'],gap_days=length,method=method,
                        date=date.fromordinal(start+j).isoformat(),observed_m=float(v),predicted_m=float(vhat),error_m=float(vhat-v))
                   for j,(v,vhat) in enumerate(zip(truth,pred)))
    details.append(rec)
 folder.mkdir(parents=True,exist_ok=True)
 files=[folder/'mascaras.csv',folder/'metricas.csv',folder/'predicciones.csv.gz']
 writecsv(files[0],masks,MASK_FIELDS);writecsv(files[1],details,DETAIL_FIELDS);writecsv(files[2],points,POINT_FIELDS)
 report=dict(monitoring_location_id=mid,segment_id=sid,etapa_completa=True,
             pilot_masks=sum(r['phase']=='piloto' for r in masks),reserved_masks=sum(r['phase']=='reservada' for r in masks),
             available_starts=avail,segment_sha256=chosen['segment_sha256'],
             hashes={p.name:sha(p) for p in files},created_utc=now(),
             scipy_version=scipy.__version__,numpy_version=np.__version__,
             failed_method_evaluations=sum(not r['success'] for r in details),
             unpaired_method_evaluations=sum(not r['paired_success'] for r in details))
 writejson(folder/'checkpoint.json',report)
 return report

def aggregate(ex,c,selected,reports,folder):
 bysegment={}
 for sid,report in reports.items():
  rs=list(readcsv(folder/'por_segmento'/sid/'metricas.csv'));groups=defaultdict(list)
  for r in rs:
   if r['paired_success']=='1':groups[(int(r['gap_days']),r['method'])].append(r)
  for (length,method),vals in groups.items():
   n=sum(int(v['n_test']) for v in vals);sse=sum(float(v['sse_m2']) for v in vals)
   bysegment[(sid,length,method)]=dict(n_masks=len(vals),n_test=n,rmse_m=math.sqrt(sse/n),
      mae_m=sum(float(v['sae_m']) for v in vals)/n,bias_m=sum(float(v['sum_error_m']) for v in vals)/n)
 well_results=[]
 for r in selected:
  for length in c['pilot_gap_lengths_days']:
   for method in METHODS:
    key=(r['segment_id'],length,method)
    if key in bysegment:well_results.append(dict(monitoring_location_id=r['monitoring_location_id'],segment_id=r['segment_id'],
          gap_scenario=int(r['gap_scenario']),gap_days=length,within_scenario=int(length<=int(r['gap_scenario'])),
          method=method,**bysegment[key]))
 outputs=[]
 fields=['monitoring_location_id','segment_id','gap_scenario','gap_days','within_scenario','method','n_masks','n_test','rmse_m','mae_m','bias_m']
 p=folder/'metricas_por_pozo.csv';writecsv(p,well_results,fields);outputs.append(p)
 grouped=defaultdict(list)
 for r in well_results:grouped[(r['gap_scenario'],r['gap_days'],r['method'])].append(r)
 summary=[]
 for (g,L,method),rs in sorted(grouped.items()):
  summary.append(dict(gap_scenario=g,gap_days=L,within_scenario=int(L<=g),method=method,n_wells=len(rs),
    n_masks=sum(r['n_masks'] for r in rs),mean_well_rmse_m=float(np.mean([r['rmse_m'] for r in rs])),
    median_well_rmse_m=float(np.median([r['rmse_m'] for r in rs])),p90_well_rmse_m=float(np.quantile([r['rmse_m'] for r in rs],.9)),
    mean_well_mae_m=float(np.mean([r['mae_m'] for r in rs])),mean_well_bias_m=float(np.mean([r['bias_m'] for r in rs]))))
 p=folder/'resumen_metodo_gap.csv';writecsv(p,summary,['gap_scenario','gap_days','within_scenario','method','n_wells','n_masks','mean_well_rmse_m','median_well_rmse_m','p90_well_rmse_m','mean_well_mae_m','mean_well_bias_m']);outputs.append(p)
 index={(r['gap_scenario'],r['gap_days'],r['monitoring_location_id'],r['method']):r for r in well_results}
 paired=[]
 for r in well_results:
  if r['method']=='lineal':continue
  base=index[(r['gap_scenario'],r['gap_days'],r['monitoring_location_id'],'lineal')]
  paired.append(dict(gap_scenario=r['gap_scenario'],gap_days=r['gap_days'],monitoring_location_id=r['monitoring_location_id'],method=r['method'],
                delta_rmse_vs_lineal_m=r['rmse_m']-base['rmse_m'],delta_mae_vs_lineal_m=r['mae_m']-base['mae_m']))
 p=folder/'comparacion_pareada_lineal.csv';writecsv(p,paired,['gap_scenario','gap_days','monitoring_location_id','method','delta_rmse_vs_lineal_m','delta_mae_vs_lineal_m']);outputs.append(p)
 common=set.intersection(*[{r['monitoring_location_id'] for r in well_results if r['gap_scenario']==g} for g in c['gap_scenarios_days']])
 p=folder/'metricas_pozos_comunes.csv';writecsv(p,[r for r in well_results if r['monitoring_location_id'] in common],fields);outputs.append(p)
 # Ranking exploratorio: igual peso por longitud ensayada <= limite y por pozo.
 # No representa la frecuencia natural de huecos ni fija el metodo definitivo.
 rank=[]
 for scenario in c['gap_scenarios_days']:
  lengths=[L for L in c['pilot_gap_lengths_days'] if L<=scenario]
  bucket=defaultdict(dict)
  for r in well_results:
   if r['gap_scenario']==scenario and r['gap_days'] in lengths:
    bucket[(r['monitoring_location_id'],r['method'])][r['gap_days']]=r
  cohort={mid for mid,method in bucket if all(len(bucket.get((mid,m),{}))==len(lengths) for m in METHODS)}
  table=[]
  for method in METHODS:
   maes=[];rmses=[]
   for mid in sorted(cohort):
    vals=bucket[(mid,method)]
    maes.append(float(np.mean([vals[L]['mae_m'] for L in lengths])))
    rmses.append(float(np.mean([vals[L]['rmse_m'] for L in lengths])))
   if maes:table.append(dict(gap_scenario=scenario,method=method,n_wells=len(cohort),
      tested_lengths='|'.join(map(str,lengths)),mean_well_length_mae_m=float(np.mean(maes)),
      mean_well_length_rmse_m=float(np.mean(rmses)),criterion='MAE; desempate RMSE; peso igual por pozo y longitud'))
  table.sort(key=lambda r:(r['mean_well_length_mae_m'],r['mean_well_length_rmse_m'],r['method']))
  for place,r in enumerate(table,1):r['rank']=place;rank.append(r)
 p=folder/'ranking_metodos_exploratorio.csv'
 writecsv(p,rank,['gap_scenario','rank','method','n_wells','tested_lengths','mean_well_length_mae_m','mean_well_length_rmse_m','criterion']);outputs.append(p)
 return outputs,summary,len(common)

def main():
 args=parser(__doc__).parse_args();ex,c,m=experiment(args);require_stage(ex,'02_segmentacion')
 out=ex/'03_piloto';out.mkdir(parents=True,exist_ok=True)
 writejson(out/'resumen.json',dict(etapa_completa=False,inicio_utc=now()))
 chosen=list(readcsv(ex/'02_segmentacion'/'segmentos_seleccionados.csv'))
 unique={r['segment_id']:r for r in chosen};reports={};errors=[];outputs=[]
 for i,(sid,r) in enumerate(unique.items(),1):
  folder=out/'por_segmento'/sid;cp=folder/'checkpoint.json'
  try:
   if cp.exists():
    saved=readjson(cp)
    ok=saved.get('scipy_version')==scipy.__version__ and saved.get('numpy_version')==np.__version__ and saved.get('etapa_completa') and saved.get('segment_sha256')==r['segment_sha256'] and all((folder/n).exists() and sha(folder/n)==d for n,d in saved.get('hashes',{}).items())
   else:ok=False
   report=saved if ok else evaluate_one(ex,r,c,folder)
   reports[sid]=report;outputs.extend([cp]+[folder/n for n in report['hashes']])
  except (OSError,ValueError,KeyError) as e:errors.append(dict(segment_id=sid,monitoring_location_id=r['monitoring_location_id'],error=str(e)))
  if i%10==0 or i==len(unique):print(f'Piloto {i}/{len(unique)}; errores {len(errors)}',flush=True)
 cov=[]
 for r in chosen:
  report=reports.get(r['segment_id'],{})
  cov.append(dict(monitoring_location_id=r['monitoring_location_id'],segment_id=r['segment_id'],gap_scenario=r['gap_scenario'],
    pilot_masks=report.get('pilot_masks',0),reserved_masks=report.get('reserved_masks',0),target_pilot=c['pilot_repetitions'],
    target_reserved=c['reserved_repetitions'],full_quota=int(report.get('pilot_masks',0)==c['pilot_repetitions'] and report.get('reserved_masks',0)==c['reserved_repetitions'])))
 for name,rs,fields in [('cobertura_validacion.csv',cov,['monitoring_location_id','segment_id','gap_scenario','pilot_masks','reserved_masks','target_pilot','target_reserved','full_quota']),
                        ('errores.csv',errors,['segment_id','monitoring_location_id','error'])]:
  p=out/name;writecsv(p,rs,fields);outputs.append(p)
 extra,summary,common=aggregate(ex,c,chosen,reports,out);outputs.extend(extra)
 total_masks=sum(r['pilot_masks'] for r in reports.values())
 finish(ex,'03_piloto',dict(etapa_completa=not errors,tipo='PILOTO_EXPLORATORIO',
    scipy_version=scipy.__version__,numpy_version=np.__version__,python=sys.version,
    unique_segments=len(unique),processed_segments=len(reports),segments_with_pilot=sum(r['pilot_masks']>0 for r in reports.values()),
    pilot_masks=total_masks,reserved_masks=sum(r['reserved_masks'] for r in reports.values()),common_wells=common,
    errors=len(errors),failed_method_evaluations=sum(r['failed_method_evaluations'] for r in reports.values()),
    unpaired_method_evaluations=sum(r['unpaired_method_evaluations'] for r in reports.values()),scientific_validation_complete=False,final_method_selected=False,final_gap_selected=False,
    selection_rule='comparar errores por longitud; cada pozo tiene igual peso; solo replicas exitosas para los tres metodos',
    limitations=['Las replicas de distintas longitudes usan bloques anidados y no son independientes.',
      'Se evalua un panel comun que admite el hueco maximo y 3 vecinos limpios por lado; puede subrepresentar extremos marcados.',
      'Periodos reservados: 1 de cada 5 anos por pozo; mascaras piloto y reservadas nunca comparten ano.',
      'No es evaluacion final ni autorizacion para rellenar huecos reales. Informar fallos y cobertura junto con errores.']),outputs)
 print('Resultados:',out)
 print(f'Mascaras piloto: {total_masks}; reservadas SIN evaluar: {sum(r["reserved_masks"] for r in reports.values())}')
 print('Comparte resumen_metodo_gap.csv, comparacion_pareada_lineal.csv, cobertura_validacion.csv y resumen.json.')
 if errors:raise ValueError('Piloto incompleto; consulta errores.csv. Reejecutar reutiliza segmentos completados.')
if __name__=='__main__':run(main)

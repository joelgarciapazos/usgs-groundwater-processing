"""Shared utilities for temporal continuity, segmentation, and validation stages."""
import argparse, csv, gzip, hashlib, io, json, math, os, sys
from datetime import date, datetime, timezone
from pathlib import Path
VERSION='1.0.0'
HERE=Path(__file__).resolve().parent
REPO_ROOT=HERE.parent
DEFAULT_QA=Path(os.environ.get('USGS_QA_DIR', str(REPO_ROOT/'data'/'qa')))
SCRIPTS=['continuity_common.py','05_prepare_continuity.py','06_gap_sensitivity_and_segmentation.py']
def now():return datetime.now(timezone.utc).isoformat()
def sha(path):
 h=hashlib.sha256()
 with Path(path).open('rb') as f:
  for b in iter(lambda:f.read(1048576),b''):h.update(b)
 return h.hexdigest()
def readjson(p):return json.loads(Path(p).read_text(encoding='utf-8-sig'))
def writejson(p,obj):
 p=Path(p);p.parent.mkdir(parents=True,exist_ok=True);tmp=p.with_suffix(p.suffix+'.tmp')
 tmp.write_text(json.dumps(obj,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8');tmp.replace(p)
def readcsv(p):
 p=Path(p);op=gzip.open if p.suffix=='.gz' else open
 with op(p,'rt',encoding='utf-8-sig',newline='') as f:
  rd=csv.DictReader(f)
  if not rd.fieldnames or len(set(rd.fieldnames))!=len(rd.fieldnames):raise ValueError(f'Cabecera invalida: {p}')
  for r in rd:
   if None in r or any(v is None for v in r.values()):raise ValueError(f'Fila CSV mal formada: {p}')
   yield r
def writecsv(p,rows,fields):
 p=Path(p);p.parent.mkdir(parents=True,exist_ok=True);tmp=p.with_suffix(p.suffix+'.tmp')
 if p.suffix=='.gz':
  with tmp.open('wb') as raw:
   with gzip.GzipFile(filename='',mode='wb',fileobj=raw,mtime=0) as gz:
    with io.TextIOWrapper(gz,encoding='utf-8-sig',newline='') as f:
     w=csv.DictWriter(f,fieldnames=fields,extrasaction='ignore');w.writeheader();w.writerows(rows)
 else:
  with tmp.open('w',encoding='utf-8-sig',newline='') as f:
   w=csv.DictWriter(f,fieldnames=fields,extrasaction='ignore');w.writeheader();w.writerows(rows)
 tmp.replace(p)
def tokens(x,depth=0):
 if depth>12:raise ValueError('Calificador excesivamente anidado')
 if x is None:return set()
 if isinstance(x,list):return set().union(*(tokens(v,depth+1) for v in x)) if x else set()
 if isinstance(x,dict):raise ValueError('Calificador objeto no soportado')
 s=str(x).strip()
 if not s:return set()
 if s[0] in '["' or s=='null':
  v=json.loads(s)
  if v!=s:return tokens(v,depth+1)
 return {s.upper()}
def boolean(s):
 if str(s).lower() in {'1','true'}:return True
 if str(s).lower() in {'0','false'}:return False
 raise ValueError(f'Bandera invalida: {s}')
def config(path):
 c=readjson(path)
 for key in ['min_years','pilot_repetitions','reserved_repetitions']:
  if type(c[key])!=int or c[key]<1:raise ValueError(f'{key} debe ser entero positivo')
 for key in ['gap_scenarios_days','pilot_gap_lengths_days']:
  v=c[key]
  if not v or any(type(x)!=int or x<1 for x in v) or len(v)!=len(set(v)):raise ValueError(key)
  c[key]=sorted(v)
 if c['unknown_qualifier_policy']!='barrier' or c['other_review_flag_policy']!='barrier':raise ValueError('Solo se implementa politica barrier')
 return c
def flags_policy(r,c):
 q=tokens(r['qualifiers_source'])|tokens(r['qualifier']);f=set(filter(None,r['qa_flags'].split('|')))
 estimated=boolean(r['source_estimated_flag']) or bool(q&set(c['estimated_qualifiers']))
 unknown=q-set(c['nonblocking_qualifiers'])-set(c['estimated_qualifiers'])
 other=f-set(c['informational_flags'])-set(c['jump_flags'])
 reasons=[]
 if unknown:reasons.append('calificadores_pendientes:'+','.join(sorted(unknown)))
 if other:reasons.append('marcas_pendientes:'+','.join(sorted(other)))
 if estimated and not c['estimated_as_input']:reasons.append('estimado_excluido_por_configuracion')
 if boolean(r['requires_review']) and not f:reasons.append('revision_sin_motivo')
 if 'source_qualifier_needs_review' in f and not q:reasons.append('calificador_revision_sin_etiqueta')
 usable=not reasons
 truth=usable and not estimated and not bool(f&set(c['jump_flags']))
 return dict(usable_input=int(usable),eligible_truth=int(truth),source_estimated_effective=int(estimated),
             barrier_reason='|'.join(reasons),qualifiers_decoded='|'.join(sorted(q)),
             jump_alert=int(bool(f&set(c['jump_flags']))))
def load_source(home,w):
 stage=(home/'03_libres').resolve();p=(stage/w['archivo_csv']).resolve()
 if not p.is_relative_to(stage):raise ValueError('Ruta fuera de 03_libres')
 digest=sha(p);prior=stage/'progreso'/(w['monitoring_location_id']+'.json')
 priorhash=readjson(prior).get('output_hashes',{}).get(w['archivo_csv']) if prior.exists() else None
 if priorhash and priorhash!=digest:raise ValueError('SHA256 no coincide con QA original')
 rows=list(readcsv(p));seen=set()
 if not rows:raise ValueError('Serie vacia')
 for r in rows:
  if r['monitoring_location_id']!=w['monitoring_location_id'] or r['site_no']!=w['site_no']:raise ValueError('Pozo inconsistente')
  if r['time_series_id']!=w['selected_time_series_id']:raise ValueError('Serie inconsistente')
  if r['approval_status']!='Approved' or r['parameter_code']!='72019' or r['statistic_id']!='00003':raise ValueError('Datos fuera del alcance')
  d=date.fromisoformat(r['date'])
  if d.isoformat()!=r['date'] or d in seen:raise ValueError('Fecha invalida/duplicada')
  seen.add(d)
  if not math.isfinite(float(r['depth_to_water_m'])) or r.get('normalization_errors',''):raise ValueError('Valor invalido')
  if boolean(r['interpolated_flag']):raise ValueError('Datos ya interpolados')
  # La conversion se verifica; unit_of_measure conserva la unidad de value_original.
  if r['unit_of_measure']=='ft' and not math.isclose(float(r['value_original'])*.3048,float(r['depth_to_water_m']),abs_tol=1e-8,rel_tol=1e-8):raise ValueError('Conversion ft->m inconsistente')
 rows.sort(key=lambda r:r['date'])
 if len(rows)!=int(w['n_days']) or sum(boolean(r['requires_review']) for r in rows)!=int(w['n_review_days']):raise ValueError('Conteos difieren del inventario')
 if sha(p)!=digest:raise ValueError('Fuente modificada durante lectura')
 return rows,p,digest,bool(priorhash)
def metrics(rows,years):
 if not rows:return dict(n_days=0,date_min='',date_max='',elapsed_days=0,expected_days=0,missing_days=0,completeness=0,max_gap_missing_days=0,duration_years_approx=0,eligible_duration=0)
 d=[date.fromisoformat(r['date']) for r in rows];span=(d[-1]-d[0]).days
 try:ann=d[0].replace(year=d[0].year+years)
 except ValueError:ann=d[0].replace(year=d[0].year+years,day=28)
 return dict(n_days=len(rows),date_min=d[0].isoformat(),date_max=d[-1].isoformat(),elapsed_days=span,
             expected_days=span+1,missing_days=span+1-len(rows),completeness=len(rows)/(span+1),
             max_gap_missing_days=max(((b-a).days-1 for a,b in zip(d,d[1:])),default=0),
             duration_years_approx=span/365.25,eligible_duration=int(d[-1]>=ann))
def segment(rows,gap):
 parts=[];part=[]
 for r in rows:
  if not boolean(r['usable_input']):
   if part:parts.append(part);part=[]
   continue
  if part and (date.fromisoformat(r['date'])-date.fromisoformat(part[-1]['date'])).days-1>gap:
   parts.append(part);part=[]
  part.append(r)
 if part:parts.append(part)
 return parts
def parser(desc,first=False):
 p=argparse.ArgumentParser(description=desc)
 p.add_argument('--carpeta',type=Path,default=DEFAULT_QA)
 if first:
  p.add_argument('--config',type=Path,default=REPO_ROOT/'config'/'continuity.json')
  p.add_argument('--pozos',nargs='+',help='Solo para una muestra explicita; omitir en corrida nacional')
 else:p.add_argument('--experimento',type=Path,help='Por defecto usa ultimo_experimento.txt de la carpeta QA')
 return p
def experiment(args):
 home=args.carpeta.expanduser().resolve()
 ex=args.experimento.expanduser().resolve() if args.experimento else Path((home/'07_continuidad_exploratoria'/'ultimo_experimento.txt').read_text().strip())
 m=readjson(ex/'manifiesto.json')
 if m['carpeta_qa']!=str(home):raise ValueError('El experimento pertenece a otra carpeta QA')
 for name,digest in m['script_hashes'].items():
  if sha(HERE/name)!=digest:raise ValueError('Los scripts cambiaron. Ejecuta nuevamente 07 para crear otra version.')
 return ex,m['config'],m
def require_stage(ex,stage):
 p=ex/stage/'resumen.json';s=readjson(p)
 if not s.get('etapa_completa'):raise ValueError(f'{stage} no completo')
 for rel,digest in s['output_hashes'].items():
  if sha(ex/rel)!=digest:raise ValueError(f'Archivo modificado: {rel}')
 return s
def finish(ex,stage,s,outputs):
 s.update(finalizado_utc=now(),output_hashes={str(p.relative_to(ex)):sha(p) for p in outputs})
 writejson(ex/stage/'resumen.json',s)
def prepared(ex,w):
 p=ex/w['prepared_file']
 if sha(p)!=w['prepared_sha256']:raise ValueError(f'Archivo preparado modificado: {p}')
 return list(readcsv(p))
def run(main):
 try:main()
 except (ValueError,OSError,KeyError) as e:print(f'ERROR: {e}',file=sys.stderr);sys.exit(2)

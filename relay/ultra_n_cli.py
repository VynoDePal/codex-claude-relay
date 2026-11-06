"""Local settings and run inspection for Ultra N; no credentials displayed."""
import argparse
import datetime
import json
import os
from dataclasses import asdict
from pathlib import Path

from ultra_n_engine import ResearchConfig
from ultra_n_relay import MODEL_LABELS, config_path, load_config, with_research_model


def atomic_json(path,value):
 path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
 temp=path.with_name(path.name+'.'+str(os.getpid())+'.tmp')
 try:
  fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
  with os.fdopen(fd,'w') as f:json.dump(value,f,ensure_ascii=False,indent=2);f.write('\n')
  os.replace(temp,path)
 finally:temp.unlink(missing_ok=True)


def main(argv=None,root=None):
 root=Path(root) if root else Path.home()/'.codex'
 parser=argparse.ArgumentParser(description='Configurer Ultra N et consulter ses recherches locales.')
 commands=parser.add_subparsers(dest='command',required=True)
 status_parser=commands.add_parser('status',help='Afficher la configuration et la dernière recherche.')
 runs_parser=commands.add_parser('runs',help='Lister les rapports locaux.')
 config_parser=commands.add_parser('config',help='Changer les plafonds (prochaines recherches).')
 for subparser in (status_parser,runs_parser,config_parser):
  subparser.add_argument('--model',choices=tuple(MODEL_LABELS),default='deepseek-v4.1',help='Modèle à configurer ou consulter (par défaut : DeepSeek).')
 config_parser.add_argument('--max-agents',type=int)
 config_parser.add_argument('--concurrency',type=int)
 config_parser.add_argument('--max-seconds',type=int)
 config_parser.add_argument('--max-tokens',type=int)
 config_parser.add_argument('--model-timeout-seconds',type=int,help='Attente maximale de réponse par appel au modèle (défaut : 120 secondes).')
 args=parser.parse_args(argv)
 config=load_config(root,args.model) or ResearchConfig(model=args.model)
 if args.command=='config':
  data=asdict(config)
  for arg,field in [('max_agents','max_agents'),('concurrency','concurrency'),('max_seconds','max_seconds'),('max_tokens','max_total_tokens'),('model_timeout_seconds','model_timeout_seconds')]:
   value=getattr(args,arg)
   if value is not None:data[field]=value
  # Lowering N should also lower its worker limit unless explicitly supplied.
  if args.max_agents is not None and args.concurrency is None:data['concurrency']=min(data['concurrency'],max(1,data['max_agents']))
  try:config=ResearchConfig.from_dict(data)
  except (ValueError,TypeError) as exc:parser.error(str(exc))
  atomic_json(config_path(root,args.model),asdict(config))
  cache_path=root/'models_cache.json'
  if cache_path.exists():
   cache=json.loads(cache_path.read_text());cache['models']=with_research_model(cache['models'],root)
   cache['etag']=None;cache['fetched_at']=datetime.datetime.now(datetime.timezone.utc).isoformat().replace('+00:00','Z')
   atomic_json(cache_path,cache)
  print(f'Ultra N ({MODEL_LABELS[args.model]}) configuré : {config.max_agents} sous-agents maximum, {config.concurrency} simultanés, xhigh.')
  print(f'Attente maximale par réponse modèle : {config.model_timeout_seconds} secondes.')
  print('Les prochaines recherches utilisent ces réglages. Rouvrez le sélecteur pour actualiser son libellé.')
 elif args.command=='status':
  print(json.dumps(asdict(config),ensure_ascii=False,indent=2))
 paths=sorted((root/'ultra-n/runs').glob('*/state.json'),reverse=True)
 if args.command in ('status','runs'):
  shown=0
  for path in paths:
   try:state=json.loads(path.read_text())
   except (ValueError,OSError):continue
   if (state.get('config') or {}).get('model','deepseek-v4.1')!=args.model:continue
   print(json.dumps({'run_id':path.parent.name,'status':state.get('status'),'metrics':state.get('metrics'),
    'report':str(path.parent/'report.md')},ensure_ascii=False))
   shown+=1
   if shown >= (1 if args.command=='status' else 20):break
  if not shown:print('Aucune recherche Ultra N enregistrée pour ce modèle.')


if __name__=='__main__':main()

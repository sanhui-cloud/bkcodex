import argparse
import json
from pathlib import Path
import subprocess

from .core import Config, Queue, doctor, install_services, start_services, status, unit_name, worker


def main():
    p=argparse.ArgumentParser(description='Server-owned Codex workers with independent task sessions')
    p.add_argument('--config',default='config.local.toml')
    commands=p.add_subparsers(dest='command',required=True)
    for name in ('init','doctor','login','install','start','resume','status'):
        commands.add_parser(name)
    add=commands.add_parser('enqueue');add.add_argument('--id',required=True)
    add.add_argument('--prompt-file',type=Path,required=True);add.add_argument('--payload',type=Path)
    batch=commands.add_parser('import');batch.add_argument('manifest',type=Path)
    run=commands.add_parser('worker');run.add_argument('--name',required=True)
    stop=commands.add_parser('stop');stop.add_argument('--now',action='store_true')
    retry=commands.add_parser('retry');retry.add_argument('task_id')
    logs=commands.add_parser('logs');logs.add_argument('--name',default='worker-01')
    args=p.parse_args();cfg=Config.read(args.config);cfg.init();result=None
    try:
        if args.command=='init': result={'state_dir':str(cfg.state),'codex_home':str(cfg.home)}
        elif args.command=='doctor': result=doctor(cfg)
        elif args.command=='login':
            raise SystemExit(subprocess.call([cfg.binary,*cfg.codex.get('global_args',[]),'login','--device-auth'],env=cfg.env()))
        elif args.command=='enqueue':
            payload=json.loads(args.payload.read_text()) if args.payload else None
            result=Queue(cfg).add(args.id,args.prompt_file.read_text(),payload)
        elif args.command=='import':
            rows=[json.loads(line) for line in args.manifest.read_text().splitlines() if line.strip()]
            result=[Queue(cfg).add(row['id'],(args.manifest.parent/row['prompt_file']).read_text(),row.get('payload')) for row in rows]
        elif args.command=='worker': worker(cfg,args.name);result=status(cfg)
        elif args.command=='status': result=status(cfg)
        elif args.command=='install': result=install_services(cfg)
        elif args.command=='start': result=start_services(cfg)
        elif args.command=='resume':
            (cfg.state/'STOP').unlink(missing_ok=True);result=start_services(cfg)
        elif args.command=='stop':
            (cfg.state/'STOP').write_text('User paused\n')
            if args.now:
                subprocess.run(['systemctl','--user','stop',*[unit_name(cfg,r) for r in cfg.roles]],check=True)
            result={'paused':True,'mode':'interrupt' if args.now else 'after_current_task'}
        elif args.command=='retry': Queue(cfg).retry(args.task_id);result=status(cfg)
        elif args.command=='logs':
            raise SystemExit(subprocess.call(['journalctl','--user','-u',unit_name(cfg,args.name),'-n','50','-f','-o','cat']))
    except (ValueError,FileNotFoundError,subprocess.CalledProcessError) as exc:
        p.exit(2,str(exc)+'\n')
    if result is not None: print(json.dumps(result,ensure_ascii=False,indent=2))

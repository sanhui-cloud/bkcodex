import argparse
import json
from pathlib import Path
import subprocess
import time

from .core import IS_WINDOWS, Config, Queue, doctor, install_services, name_checked, start_services, status, stop_services, unit_name, worker


def _tail_log(path: Path, lines: int, follow: bool) -> int:
    if not path.exists():
        print(f'No log file yet: {path}')
        return 0
    content = path.read_text(encoding='utf-8', errors='replace').splitlines()
    for line in (content[-lines:] if lines > 0 else []):
        print(line)
    if not follow:
        return 0
    with path.open('r', encoding='utf-8', errors='replace') as stream:
        stream.seek(0, 2)
        try:
            while True:
                line = stream.readline()
                if line:
                    print(line, end='', flush=True)
                else:
                    time.sleep(0.5)
        except KeyboardInterrupt:
            return 0


def main():
    p=argparse.ArgumentParser(description='Persistent Codex workers with independent task sessions')
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
    logs.add_argument('--tail',type=int,default=50)
    logs.add_argument('-f','--follow',action='store_true')
    args=p.parse_args();cfg=Config.read(args.config);cfg.init();result=None
    try:
        if args.command=='init': result={'state_dir':str(cfg.state),'codex_home':str(cfg.home)}
        elif args.command=='doctor': result=doctor(cfg)
        elif args.command=='login':
            raise SystemExit(subprocess.call([cfg.binary,*cfg.codex.get('global_args',[]),'login','--device-auth'],env=cfg.env()))
        elif args.command=='enqueue':
            payload=json.loads(args.payload.read_text(encoding='utf-8')) if args.payload else None
            result=Queue(cfg).add(args.id,args.prompt_file.read_text(encoding='utf-8'),payload)
        elif args.command=='import':
            rows=[json.loads(line) for line in args.manifest.read_text(encoding='utf-8').splitlines() if line.strip()]
            result=[Queue(cfg).add(row['id'],(args.manifest.parent/row['prompt_file']).read_text(encoding='utf-8'),row.get('payload')) for row in rows]
        elif args.command=='worker': worker(cfg,args.name);result=status(cfg)
        elif args.command=='status': result=status(cfg)
        elif args.command=='install': result=install_services(cfg)
        elif args.command=='start': result=start_services(cfg)
        elif args.command=='resume':
            (cfg.state/'STOP').unlink(missing_ok=True);result=start_services(cfg)
        elif args.command=='stop':
            result=stop_services(cfg,immediate=args.now)
        elif args.command=='retry': Queue(cfg).retry(args.task_id);result=status(cfg)
        elif args.command=='logs':
            role=name_checked(args.name)
            if IS_WINDOWS:
                raise SystemExit(_tail_log(cfg.state/'logs'/(role+'.log'),args.tail,args.follow))
            raise SystemExit(subprocess.call(['journalctl','--user','-u',unit_name(cfg,role),'-n',str(args.tail),'-f','-o','cat']))
    except (ValueError,FileNotFoundError,subprocess.CalledProcessError,OSError) as exc:
        p.exit(2,str(exc)+'\n')
    if result is not None: print(json.dumps(result,ensure_ascii=False,indent=2))

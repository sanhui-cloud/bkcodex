from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from bkcodex.core import Config, Queue, cli_args, execute, failure_kind, load, save, status, unit_text, validate_result, worker


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.project=self.root/'project';self.project.mkdir()
        self.cli=self.root/'fake-codex';self.cli.write_text('''#!/usr/bin/env python3
import json,sys,re,pathlib,time
text=sys.stdin.read()
taskid=re.search(r'任务编号：(.*)',text).group(1)
out=pathlib.Path(re.search(r'本轮输出目录：(.*)',text).group(1))
print(json.dumps({'type':'thread.started','thread_id':taskid}),flush=True)
if 'SLEEP_FAKE' in text: time.sleep(10)
if 'AUTH_FAKE' in text:
 print(json.dumps({'type':'turn.failed','error':{'message':'401 unauthorized'}}),flush=True);sys.exit(1)
(out/'answer.md').write_text('- done')
result={'task_id':taskid,'status':'completed','summary':'done','artifacts':['answer.md']}
if 'BAD_RESULT' in text: result['task_id']='wrong'
(out/'result.json').write_text(json.dumps(result))
print(json.dumps({'type':'turn.completed','usage':{'input_tokens':10,'output_tokens':3}}),flush=True)
''');self.cli.chmod(0o700)
        self.path=self.root/'config.toml'
        self.path.write_text(f'''[project]
name="test"
workspace={json.dumps(str(self.project))}
state_dir={json.dumps(str(self.root/'state'))}
[codex]
binary={json.dumps(str(self.cli))}
model="gpt-6.1-sol"
[workers]
count=3
max_attempts=2
task_timeout_seconds=2
retry_delay_seconds=0
''')
        self.cfg=Config.read(self.path);self.cfg.init();self.q=Queue(self.cfg)

    def test_idempotent_enqueue_and_changed_content(self):
        self.q.add('one','task');self.q.add('one','task');self.assertEqual(len(self.q.rows()),1)
        with self.assertRaises(ValueError):self.q.add('one','changed')

    def test_concurrent_owners_claim_different_jobs(self):
        for i in range(10):self.q.add(f't{i}','task')
        with ThreadPoolExecutor(max_workers=3) as pool:
            rows=list(pool.map(self.q.claim,['w1','w2','w3']))
        self.assertEqual(len({r['id'] for r in rows}),3)
        with self.assertRaises(ValueError):self.q.finish(rows[0]['id'],'wrong','completed','x')

    def test_restart_keeps_artifact_handoff_and_attempt_limit(self):
        self.q.add('one','task');a=self.q.claim('w1')
        self.q.running_dir('one','w1','/previous/run')
        b=self.q.claim('w1');self.assertEqual(a['id'],b['id']);self.assertEqual(b['run_dir'],'/previous/run')
        self.assertEqual(b['attempts'],2);self.assertTrue(self.q.claim('w1')['skip'])
        self.assertEqual(self.q.rows()[0]['status'],'blocked')

    def test_end_to_end_worker_finishes_two_jobs(self):
        self.q.add('a','task');self.q.add('b','task');worker(self.cfg,'w1')
        self.assertEqual(status(self.cfg)['counts'],{'completed':2})
        runs=list((self.cfg.state/'runs').glob('*/*'))
        self.assertEqual(len(runs),2)
        for run in runs:
            self.assertEqual(load(run/'execution.json')['usage']['input_tokens'],10)
            self.assertTrue((run/'receipt.json').exists())

    def test_stop_marker_prevents_claim(self):
        self.q.add('a','task');(self.cfg.state/'STOP').touch();worker(self.cfg,'w1')
        self.assertEqual(self.q.rows()[0]['status'],'queued')

    def test_bad_result_is_blocked(self):
        self.q.add('a','BAD_RESULT');worker(self.cfg,'w1')
        self.assertEqual(self.q.rows()[0]['status'],'blocked')

    def test_auth_failure_stops_worker(self):
        self.q.add('a','AUTH_FAKE');self.q.add('b','task');worker(self.cfg,'w1')
        self.assertEqual(status(self.cfg)['counts'],{'blocked':1,'queued':1})

    def test_timeout_saves_execution(self):
        self.q.add('a','SLEEP_FAKE');worker(self.cfg,'w1')
        row=self.q.rows()[0];self.assertEqual(row['status'],'blocked')
        self.assertTrue(load(Path(row['run_dir'])/'execution.json')['timed_out'])

    def test_validator_failure_blocks_completion(self):
        self.path.write_text(self.path.read_text()+f'\n[validation]\ncommand=[{json.dumps(sys.executable)},"-c","import sys;sys.exit(3)"]\n')
        cfg=Config.read(self.path);self.q.add('a','task');worker(cfg,'w1')
        self.assertEqual(self.q.rows()[0]['status'],'blocked')
        self.assertIn('validator failed',self.q.rows()[0]['error'])

    def test_isolated_home_fresh_sessions_and_service_quoting(self):
        self.assertNotEqual(self.cfg.env()['CODEX_HOME'],str(Path.home()/'.codex'))
        self.assertIn('cli_auth_credentials_store = "file"',(self.cfg.home/'config.toml').read_text())
        args=cli_args(self.cfg,self.root/'run');self.assertNotIn('resume',args)
        self.assertIn('model_reasoning_effort="high"',args)
        text=unit_text(self.cfg,'worker-01')
        self.assertIn('KillMode=control-group',text)
        self.assertIn('StartLimitBurst=3\n[Service]',text)
        self.assertIn('WorkingDirectory='+str(self.project)+'\n',text)

    def test_artifacts_must_exist_and_stay_in_project(self):
        run=self.cfg.state/'runs'/'a';run.mkdir()
        save(run/'result.json',dict(task_id='a',status='completed',summary='done',artifacts=['missing']))
        with self.assertRaisesRegex(ValueError,'Artifact missing'):validate_result(self.cfg,run,{'id':'a'})
        outside=self.root/'outside';outside.write_text('data')
        save(run/'result.json',dict(task_id='a',status='completed',summary='done',artifacts=[str(outside)]))
        with self.assertRaisesRegex(ValueError,'within state or workspace'):validate_result(self.cfg,run,{'id':'a'})

    def test_manual_retry_preserves_old_logs(self):
        self.q.add('a','BAD_RESULT');worker(self.cfg,'w1')
        old=self.q.rows()[0]['run_dir'];self.q.retry('a')
        self.assertEqual(self.q.rows()[0]['status'],'queued');self.assertTrue(Path(old).exists())


if __name__=='__main__':unittest.main()

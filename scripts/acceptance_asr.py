"""Single-host ASR checks: stopped worker backlog and old-version replay after activation."""
import json,os,sqlite3,subprocess,sys,time,urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4
from scripts import acceptance_load as http
ROOT=Path(__file__).resolve().parents[1];OUT=ROOT/'evidence/acceptance-20261119';RUNTIME=ROOT/'data/runtime/final-validation'
def main():
 raw=json.loads((ROOT/'examples/A2-score-input.json').read_text(encoding='utf8'))
 secret=(ROOT/'data/runtime/jwt_secret.txt').read_text().strip()
 isolated=RUNTIME/'offline-worker.db'
 with sqlite3.connect(ROOT/'data/runtime/demo.db') as src,sqlite3.connect(isolated) as dest:src.backup(dest)
 env={**os.environ,'DATABASE_URL':'sqlite:///'+isolated.as_posix(),'JWT_SECRET':secret,'PYTHONIOENCODING':'utf8'}
 log=(RUNTIME/'offline-api.log').open('w',encoding='utf8')
 api=subprocess.Popen([sys.executable,'-m','uvicorn','app.main:create_app','--factory','--host','127.0.0.1','--port','18101'],cwd=ROOT,env=env,stdout=log,stderr=log)
 http.BASE='http://127.0.0.1:18101'
 try:
  for _ in range(60):
   try:
    with urllib.request.urlopen(http.BASE+'/health',timeout=1):break
   except Exception:time.sleep(.5)
  token=http.req('/api/v1/auth/token',{'username':'developer','password':'Acceptance2026Local'})[1]['access_token']
  v=next(x for x in http.req('/api/v1/model-versions',token=token)[1]['items'] if x['active'])['model_version_id']
  def score(i):
   code,data=http.req('/api/v1/predictions',{'model_version_id':v,'input':raw},token,str(uuid4()))
   assert code==201 and data['status']=='success';return data['event_id']
  with ThreadPoolExecutor(max_workers=5) as pool:events=list(pool.map(score,range(100)))
  placeholders=','.join('?'*100)
  with sqlite3.connect(isolated) as db:before=db.execute('SELECT COUNT(*) FROM outbox_events WHERE status=\'pending\' AND event_id IN ('+placeholders+')',events).fetchone()[0]
  assert before==100
 finally:
  api.terminate();api.wait(timeout=15);log.close();http.BASE='http://127.0.0.1:18100'
 for _ in range(2):
  run=subprocess.run([sys.executable,'-m','app.worker','--once','--max-events','200'],cwd=ROOT,env=env,capture_output=True,text=True,encoding='utf8')
  assert run.returncode==0
 with sqlite3.connect(isolated) as db:
  done=db.execute('SELECT COUNT(*) FROM outbox_events WHERE status=\'done\' AND event_id IN ('+placeholders+')',events).fetchone()[0]
  counts=db.execute('SELECT COUNT(*),COUNT(DISTINCT event_id) FROM processed_events WHERE event_id IN ('+placeholders+')',events).fetchone()
 offline={'worker_stopped_requests':100,'pending_before_restart':before,'done_after_restart':done,'processed_rows':counts[0],'unique_processed_events':counts[1],'passed':before==done==counts[0]==counts[1]==100}
 assert offline['passed'];print(json.dumps(offline),flush=True)
 admin=http.req('/api/v1/auth/token',{'username':'admin','password':'Acceptance2026Local'})[1]['access_token']
 auditor=http.req('/api/v1/auth/token',{'username':'auditor','password':'Acceptance2026Local'})[1]['access_token']
 versions=http.req('/api/v1/model-versions',token=admin)[1]['items'];original=next(v for v in versions if v['active'])['model_version_id'];predictions=[]
 try:
  for version in versions:
   vid=version['model_version_id'];http.req('/api/v1/model-versions/'+vid+'/activate',{},admin,str(uuid4()))
   for _ in range(10):
    _,p=http.req('/api/v1/predictions',{'model_version_id':vid,'input':raw},admin,str(uuid4()));predictions.append((vid,p['prediction_id']))
  replays=[]
  for vid,pid in predictions:
   _,r=http.req('/api/v1/replay-runs',{'prediction_id':pid},auditor,str(uuid4()));replays.append((vid,r['replay_id']))
  deadline=time.time()+60;tasks=[]
  while time.time()<deadline:
   tasks=[http.req('/api/v1/replay-runs/'+rid,token=auditor)[1] for vid,rid in replays]
   if all(t['status'] not in ['pending','running'] for t in tasks):break
   time.sleep(1)
  passed=all(t['status']=='passed' and t['absolute_error']<=1e-8 and t['model_version_id']==vid for (vid,rid),t in zip(replays,tasks))
  result={'worker_offline':offline,'version_replay':{'versions':len(versions),'samples_per_version':10,'active_during_replay':versions[-1]['model_version_id'],'tasks':tasks,'passed':passed},'passed':offline['passed'] and passed}
  (OUT/'asr-results.json').write_text(json.dumps(result,indent=2),encoding='utf8');assert result['passed'];print('20 original-version replays passed',flush=True)
 finally:http.req('/api/v1/model-versions/'+original+'/activate',{},admin,str(uuid4()))
if __name__=='__main__':main()

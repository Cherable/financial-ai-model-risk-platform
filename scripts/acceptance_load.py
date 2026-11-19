"""Real HTTP acceptance: original ASR scenario and higher-concurrency stress."""
import json,time,urllib.request,statistics,sqlite3,math
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4
from datetime import datetime,timezone
ROOT=Path(__file__).resolve().parents[1];OUT=ROOT/'evidence/acceptance-20261119'
BASE='http://127.0.0.1:18100'
def req(path,body=None,token=None,key=None):
 h={'Content-Type':'application/json'}
 if token:h['Authorization']='Bearer '+token
 if key:h['Idempotency-Key']=key
 with urllib.request.urlopen(urllib.request.Request(BASE+path,data=json.dumps(body).encode() if body is not None else None,headers=h),timeout=60) as r:return r.status,json.load(r)
def main():
 OUT.mkdir(parents=True,exist_ok=True)
 started=datetime.now(timezone.utc)
 token=req('/api/v1/auth/token',{'username':'developer','password':'Acceptance2026Local'})[1]['access_token']
 v=next(x for x in req('/api/v1/model-versions',token=token)[1]['items'] if x['active'])['model_version_id']
 raw=json.loads((ROOT/'examples/A2-score-input.json').read_text(encoding='utf8'))
 def score(i):
  t=time.perf_counter()
  try:
   code,data=req('/api/v1/predictions',{'model_version_id':v,'input':raw},token,str(uuid4()))
   ok=code==201 and data.get('status')=='success';error=None if ok else str(data)
  except Exception as exc:ok=False;error=str(exc)
  return {'ms':(time.perf_counter()-t)*1000,'success':ok,'error':error}
 for i in range(5):assert score(i)['success']
 rounds=[]
 for name,concurrency,count in [('asr-baseline',5,500),('stress-1',20,100),('stress-2',20,100),('stress-3',20,100)]:
  t=time.perf_counter()
  with ThreadPoolExecutor(max_workers=concurrency) as pool:samples=list(pool.map(score,range(count)))
  elapsed=time.perf_counter()-t;times=sorted(s['ms'] for s in samples);failures=sum(not s['success'] for s in samples)
  p95=times[math.ceil(count*.95)-1]
  r={'name':name,'requests':count,'concurrency':concurrency,'successes':count-failures,'failures':failures,'error_rate':failures/count,'p50_ms':statistics.median(times),'p95_ms':p95,'max_ms':times[-1],'rps':count/elapsed,'samples':samples,'passed':p95<=500 and failures/count<.01}
  rounds.append(r);print(json.dumps({k:v for k,v in r.items() if k!='samples'}),flush=True)
 deadline=time.time()+100
 while True:
  with sqlite3.connect(ROOT/'data/runtime/demo.db') as db:
   pending=db.execute("SELECT COUNT(*) FROM outbox_events WHERE status != 'done'").fetchone()[0]
   rows=db.execute('SELECT window_start,window_end,computed_at FROM monitor_windows WHERE window_end > ?',(started.isoformat().replace('+00:00','Z'),)).fetchall()
  lags=[{'window_start':s,'window_end':e,'computed_at':c,'lag_seconds':(datetime.fromisoformat(c)-datetime.fromisoformat(e)).total_seconds()} for s,e,c in rows if c]
  if pending==0 and rows and len(lags)==len(rows):break
  if time.time()>=deadline:break
  time.sleep(1)
 stress=sorted(s['ms'] for r in rounds[1:] for s in r['samples'])
 result={'started_at':started.isoformat(),'executed_at':datetime.now(timezone.utc).isoformat(),'rounds':rounds,'aggregate_requests':800,'stress_aggregate_p95_ms':stress[284],'monitoring_lags':lags,'pending_events':pending,'baseline_passed':rounds[0]['passed'],'stress_stability_passed':all(r['passed'] for r in rounds[1:]),'monitoring_passed':bool(rows) and len(lags)==len(rows) and pending==0 and all(x['lag_seconds']<=15 for x in lags),'scope':'Windows single host, real HTTP, synthetic data; nearest-rank P95; each round retains every sample.'}
 result['passed']=all(result[x] for x in ['baseline_passed','stress_stability_passed','monitoring_passed'])
 (OUT/'load-results.json').write_text(json.dumps(result,indent=2),encoding='utf8');print(json.dumps({k:v for k,v in result.items() if k!='rounds'}),flush=True)
if __name__=='__main__':main()

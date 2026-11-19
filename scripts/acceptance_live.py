"""Loopback-only live acceptance; synthetic records, real HTTP and worker."""
import urllib.request, urllib.error, json, time, hashlib, statistics, subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from pathlib import Path
from uuid import uuid4

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'evidence/acceptance-20261119'
OUT.mkdir(parents=True,exist_ok=True)
BASE='http://127.0.0.1:18100'
checks=[]
def req(path,method='GET',body=None,token=None,key=None):
    headers={'Content-Type':'application/json'}
    if token: headers['Authorization']='Bearer '+token
    if key: headers['Idempotency-Key']=key
    r=urllib.request.Request(BASE+path,data=json.dumps(body).encode() if body is not None else None,headers=headers,method=method)
    try:
        with urllib.request.urlopen(r,timeout=60) as response: return response.status,response.read(),dict(response.headers)
    except urllib.error.HTTPError as e: return e.code,e.read(),dict(e.headers)
def check(name,ok,details=None):
    checks.append({'name':name,'passed':bool(ok),'details':details}); assert ok,(name,details)
tokens={}
for user in ['admin','developer','risk','auditor']:
    code,b,_=req('/api/v1/auth/token','POST',{'username':user,'password':'Acceptance2026Local'})
    check(user+' login',code==200);tokens[user]=json.loads(b)['access_token']
check('anonymous rejected',req('/api/v1/model-versions')[0]==401)
check('wrong password rejected',req('/api/v1/auth/token','POST',{'username':'admin','password':'wrong-password'})[0]==401)
versions=json.loads(req('/api/v1/model-versions',token=tokens['admin'])[1])['items']
v=next(x for x in versions if x['version']=='credit-logreg-v1')['model_version_id']
check('admin activates version',req('/api/v1/model-versions/'+v+'/activate','POST',{},tokens['admin'],str(uuid4()))[0]==200)
raw=json.loads((ROOT/'examples/A2-score-input.json').read_text())
key='acceptance-'+str(uuid4()); payload={'model_version_id':v,'input':raw}
first=req('/api/v1/predictions','POST',payload,tokens['developer'],key)
check('score created',first[0]==201,first[1].decode())
prediction=json.loads(first[1]); pid=prediction['prediction_id']
repeat=req('/api/v1/predictions','POST',payload,tokens['developer'],key)
check('score idempotency',json.loads(repeat[1])['prediction_id']==pid)
bad={**payload,'input':{**raw,'age':999}}
check('invalid input rejected',req('/api/v1/predictions','POST',bad,tokens['developer'],str(uuid4()))[0]==422)
for role in ['risk','auditor']:
    b=json.loads(req('/api/v1/predictions/'+pid,token=tokens[role])[1])
    check(role+' raw visibility',b.get('raw_input_withheld',False)==(role=='risk'))
check('developer export forbidden',req('/api/v1/audit/records/'+pid+'/export',token=tokens['developer'])[0]==403)
for fmt in ['json','csv']:
    code,b,h=req('/api/v1/audit/records/'+pid+'/export?format='+fmt,token=tokens['auditor'])
    digest=hashlib.sha256(b).hexdigest()
    check(fmt+' export digest',code==200 and digest=={k.lower():v for k,v in h.items()}.get('x-file-sha256'))
    (OUT/('export.'+fmt)).write_bytes(b)
rk='live-replay-'+str(uuid4())
code,b,_=req('/api/v1/replay-runs','POST',{'prediction_id':pid},tokens['auditor'],rk)
check('replay creation',code==201,b.decode()); rid=json.loads(b)['replay_id']
for _ in range(30):
    task=json.loads(req('/api/v1/replay-runs/'+rid,token=tokens['auditor'])[1])
    if task['status'] not in ['pending','running']:break
    time.sleep(1)
check('real model replay',task['status']=='passed' and task['absolute_error']<=1e-8,task)
check('replay receipt after finish',json.loads(req('/api/v1/replay-runs','POST',{'prediction_id':pid},tokens['auditor'],rk)[1])['replay_id']==rid)
effective=datetime.now(timezone.utc).replace(second=0,microsecond=0)+timedelta(minutes=1)
rule={'model_version_id':v,'metric_name':'missing_rate','feature':'age','threshold':0.0,'min_samples':1,'effective_at':effective.isoformat()}
response=req('/api/v1/monitor-rules','POST',rule,tokens['risk'],str(uuid4()))
check('risk rule creation',response[0]==200,response[1].decode())
time.sleep(max(0,(effective-datetime.now(timezone.utc)).total_seconds())+1)
def score(i):
    inp={**raw,'customer_ref':f'SYN-LIVE-{i}'}
    if i%2==0:inp['age']=None
    t=time.perf_counter(); code,b,_=req('/api/v1/predictions','POST',{'model_version_id':v,'input':inp},tokens['developer'],str(uuid4()))
    return {'status':code,'milliseconds':(time.perf_counter()-t)*1000}
for i in range(5): score(-i-1)
start=time.perf_counter()
with ThreadPoolExecutor(max_workers=20) as pool: samples=list(pool.map(score,range(100)))
elapsed=time.perf_counter()-start
latencies=sorted(s['milliseconds'] for s in samples)
performance={'concurrency':20,'requests':100,'successes':sum(s['status']==201 for s in samples),'elapsed_seconds':elapsed,'requests_per_second':100/elapsed,'p50_ms':statistics.median(latencies),'p95_ms':latencies[94],'max_ms':max(latencies),'samples':samples}
check('HTTP load no failures',performance['successes']==100,performance)
deadline=time.time()+95
while time.time()<deadline:
    metric=json.loads(req('/api/v1/metrics?model_version_id='+v+'&limit=100',token=tokens['risk'])[1])
    alerts=json.loads(req('/api/v1/alerts?model_version_id='+v,token=tokens['risk'])[1])
    current=[x for x in metric.get('items',[]) if datetime.fromisoformat(x['window_start'].replace('Z','+00:00'))>=effective]
    current_windows={x['window_id'] for x in current}
    current_alerts=[x for x in alerts.get('items',[]) if x['window_id'] in current_windows]
    if current and current_alerts:break
    time.sleep(2)
check('real worker metrics and alert',bool(current) and bool(current_alerts),{'metric_count':len(current),'alerts':current_alerts})
lags=[(datetime.fromisoformat(x['computed_at'].replace('Z','+00:00'))-datetime.fromisoformat(x['window_end'].replace('Z','+00:00'))).total_seconds() for x in current]
check('final window closes within 15 seconds',max(lags)<=15,{'max_lag_seconds':max(lags),'worker_poll_seconds':2})
alert=current_alerts[0]['alert_id']
for action in ['acknowledge','close']:
    code,b,_=req('/api/v1/alerts/'+alert+'/'+action,'POST',{'note':'Synthetic live acceptance '+action},tokens['risk'],str(uuid4()))
    check('alert '+action,code==200,b.decode())
result={'executed_at':datetime.now(timezone.utc).isoformat(),'source_commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),'working_diff_sha256':hashlib.sha256(subprocess.check_output(['git','diff','HEAD'],cwd=ROOT)).hexdigest(),'scope':'real loopback HTTP; synthetic data; single Windows host','prediction_id':pid,'replay_id':rid,'checks':checks,'performance':performance,'passed':all(c['passed'] for c in checks)}
(OUT/'live-results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8')
print(json.dumps({k:v for k,v in result.items() if k not in ['checks','performance']},indent=2))
print('P95(ms):',performance['p95_ms'],'RPS:',performance['requests_per_second'])

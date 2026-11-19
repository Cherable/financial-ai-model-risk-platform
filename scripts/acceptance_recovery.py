"""Kill a claimed replay process, then restart real single worker on an isolated DB copy."""
import os,sys,sqlite3,subprocess,time,json
from pathlib import Path
from uuid import uuid4
from sqlalchemy import select
from app.storage.sqlite_store import SqlitePredictionStore
from app.modules.replay.service import ReplayService
from app.db.schema import replay_runs
ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'evidence/acceptance-20261119'
DB=ROOT/'data/runtime/acceptance-recovery.db'
if '--claim' in sys.argv:
    base=SqlitePredictionStore('sqlite:///'+DB.as_posix())
    service=ReplayService(base.engine,ROOT/'artifacts')
    task=service._claim()
    print(json.dumps(task),flush=True)
    time.sleep(3600)
else:
    with sqlite3.connect(ROOT/'data/runtime/demo.db') as source,sqlite3.connect(DB) as dest: source.backup(dest)
    with sqlite3.connect(DB) as con:
        row=con.execute("SELECT prediction_id,model_version_id,requested_by,created_at FROM replay_runs WHERE status='passed' LIMIT 1").fetchone()
        rid=str(uuid4())
        con.execute("INSERT INTO replay_runs(replay_id,prediction_id,model_version_id,requested_by,created_at,status,environment_check) VALUES(?,?,?,?,?,'pending','pending')",(rid,*row))
    p=subprocess.Popen([sys.executable,'-m','scripts.acceptance_recovery','--claim'],cwd=ROOT,stdout=subprocess.PIPE,text=True)
    claimed=json.loads(p.stdout.readline()); assert claimed['replay_id']==rid
    p.kill(); p.wait()
    with sqlite3.connect(DB) as con: assert con.execute('SELECT status FROM replay_runs WHERE replay_id=?',(rid,)).fetchone()[0]=='running'
    env={**os.environ,'DATABASE_URL':'sqlite:///'+DB.as_posix(),'JWT_SECRET':'recovery-isolated-fixture-'+'x'*40}
    resumed=subprocess.run([sys.executable,'-m','app.worker','--once'],cwd=ROOT,env=env,capture_output=True,text=True,encoding='utf8')
    (OUT/'recovery-worker.log').write_text(resumed.stdout+resumed.stderr,encoding='utf8')
    with sqlite3.connect(DB) as con:
        status,error=con.execute('SELECT status,absolute_error FROM replay_runs WHERE replay_id=?',(rid,)).fetchone()
        audits=con.execute("SELECT COUNT(*) FROM audit_logs WHERE resource_id=? AND action='replay.recovered'",(rid,)).fetchone()[0]
    result={'method':'isolated database backup; child claims durable task; OS kill; real worker --once restarts','replay_id':rid,'killed_process_returncode':p.returncode,'restart_returncode':resumed.returncode,'final_status':status,'absolute_error':error,'recovery_audits':audits,'passed':status=='passed' and error==0 and audits==1 and resumed.returncode==0}
    (OUT/'recovery-results.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    print(json.dumps(result,indent=2));assert result['passed']

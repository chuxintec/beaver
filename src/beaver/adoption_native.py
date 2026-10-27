# Derived from frozen monkey source; MIT notice in licenses/MONKEY-MIT.txt.
"""Bounded, owned Editor child for adoption; only the Editor mutates assets."""
from pathlib import Path
from datetime import datetime,timezone,timedelta
import os
import subprocess
import signal
import time
import threading
import plistlib
from .io import safe,inside,read,load,sha,encoded,write_json
from .errors import fail
from .output_limits import digest_file
from .adoption import prepare,commit_state,project_files,checked_plan,STATE,native_lock

HELPER_ROOT = 'Assets/BeaverAdoption'
HELPER = HELPER_ROOT+'/Editor/BeaverAdoption.cs'


def helper(target):
    source=Path(__file__).parent/'targets/BeaverAdoption.cs';dest=inside(target,HELPER)
    if inside(target,HELPER_ROOT).exists() or inside(target,HELPER_ROOT+'.meta').exists():
        fail('ADOPT_HELPER_CONFLICT',str(dest),'Temporary helper directory already exists')
    dest.parent.mkdir(parents=True);dest.write_bytes(source.read_bytes());return sha(source.read_bytes())


def cleanup(target,identity):
    dest=inside(target,HELPER)
    if not dest.exists():
        if inside(target,HELPER_ROOT).exists():fail('ADOPT_HELPER_DRIFT',HELPER_ROOT,'Temporary helper disappeared but its directory remains')
        return
    if digest_file(dest)!=identity:fail('ADOPT_HELPER_DRIFT',str(dest),'Temporary helper changed; retained for inspection')
    directory=inside(target,HELPER_ROOT)
    allowed={HELPER,HELPER+'.meta',HELPER_ROOT+'/Editor.meta'}
    if any(p.is_file() and p.relative_to(target).as_posix() not in allowed for p in directory.rglob('*')):
        fail('ADOPT_HELPER_DRIFT',str(directory),'Unknown helper directory content')
    for p in sorted(directory.rglob('*'),key=lambda p:len(p.parts),reverse=True):
        if p.is_file():p.unlink()
        elif p.is_dir():p.rmdir()
    directory.rmdir();meta=inside(target,HELPER_ROOT+'.meta')
    if meta.exists():meta.unlink()


def job_spec(stage,plan,journal,mode,budget):
    operations=[]
    for op in plan['operations']:
        before=op.get('before') or {};after=op['after']
        operations.append(dict(action=op['action'],source=op['source'],target=op['target'],guid=op['guid'],
            beforeFileHash=(before.get('file') or {}).get('sha256'),beforeMetaHash=(before.get('meta') or {}).get('sha256'),
            afterFileHash=after['file']['sha256'],afterMetaHash=(after.get('meta') or {}).get('sha256'),
            preserveLocalIds=op['action']=='move' and before['file']['sha256']==after['file']['sha256']))
    changed={p for op in plan['operations'] for p in (op['source'],op['target'])}
    watch=sorted(p for p in plan['targetFiles'] if p.endswith(('.prefab','.unity')) and p not in changed)[:128]
    return dict(targetRoot=plan['targetRoot'],stage=str(stage),mode=mode,planSha256=journal['planSha256'],operations=operations,watch=watch,
                budgetSeconds=budget,deadlineUtc=(datetime.now(timezone.utc)+timedelta(seconds=budget)).isoformat())


def run_child(stage,plan,journal,editor,mode,budget):
    if threading.current_thread() is not threading.main_thread():raise ValueError('Adoption child requires main-thread signal ownership')
    target=safe(plan['targetRoot']);native_lock(target)
    spec=job_spec(stage,plan,journal,mode,budget);path=stage/('job-'+mode+'.json');write_json(path,spec)
    from .native_environment import editor_environment
    env=editor_environment('BEAVER_ADOPT_JOB',path);args=[str(editor),'-batchmode','-nographics','-projectPath',str(target),'-executeMethod','BeaverAdoption.Run','-logFile',str(stage/(mode+'.log'))]
    interrupted=[];handlers={};process=None;end=time.monotonic()+budget+90
    try:
        with (stage/(mode+'-console.log')).open('w') as stream:
            process=subprocess.Popen(args,env=env,cwd=target,stdout=stream,stderr=subprocess.STDOUT)
            journal.update(state='native-'+mode,pid=process.pid,processExited=False);write_json(stage/'journal.json',journal)
            for number in (signal.SIGINT,signal.SIGTERM):handlers[number]=signal.signal(number,lambda n,f:interrupted.append(n))
            while process.poll() is None and time.monotonic()<end and not interrupted:time.sleep(.25)
            if process.poll() is None:
                process.terminate()
                try:process.wait(timeout=30)
                except subprocess.TimeoutExpired:pass
            journal.update(processExited=process.poll() is not None,exitCode=process.poll());write_json(stage/'journal.json',journal)
    finally:
        for number,handler in handlers.items():signal.signal(number,handler)
    if not journal['processExited']:fail('ADOPT_EXIT_PENDING',str(stage),'Owned Editor has not exited; inspect before recovery')
    output=stage/('native-'+mode+'.json')
    result=load(read(output)) if output.exists() else {}
    if journal.get('exitCode')!=0 or result.get('status') not in ('verified','rolled-back') or result.get('planSha256')!=journal['planSha256']:
        journal['state']='recovery-required';write_json(stage/'journal.json',journal)
        fail('ADOPT_NATIVE_FAILED',str(stage),'Native step failed; inspect native logs and use beaver adopt rollback for recorded writes')
    return result


def execute(plan_path,output,editor,budget=1200,expected=None):
    from .native_environment import editor_environment
    editor_environment('BEAVER_ADOPT_JOB', output)
    if type(budget)is not int or not 1<=budget<=1200:fail('ADOPT_BUDGET',str(budget),'Expected 1..1200 seconds per Editor step')
    plan,_=checked_plan(plan_path,expected)
    if not plan['targetProfile'].startswith('unity-'):fail('ADOPT_NATIVE_UNSUPPORTED',plan['targetProfile'],'Creator execution requires its asset-db adapter; preview is available')
    editor=safe(editor)
    if editor.name not in ('Unity','Unity.exe') or not editor.is_file():fail('ADOPT_EDITOR',str(editor),'Expected existing Unity Editor executable')
    if os.name=='posix' and editor.parent.name=='MacOS':
        metadata=editor.parent.parent/'Info.plist'
        if not metadata.is_file() or plistlib.loads(metadata.read_bytes()).get('CFBundleVersion')!='6000.6.2f1':
            fail('ADOPT_EDITOR_VERSION',str(editor),'Expected installed Unity 6000.6.2f1; no project upgrade is performed')
    stage,plan,journal=prepare(plan_path,output,expected);target=safe(plan['targetRoot'])
    from .transaction_fs import lock
    with lock(target):
        fingerprint=helper(target);journal['helperSha256']=fingerprint;write_json(stage/'journal.json',journal)
        try:
            applied=run_child(stage,plan,journal,editor,'apply',budget)
            reopened=run_child(stage,plan,journal,editor,'verify',budget)
            cleanup(target,fingerprint)
            return commit_state(stage,plan,journal,dict(status='verified',saveReopen=True,assets=len(reopened['assets']),referencesPreserved=reopened['referencesPreserved'],scope='changed assets and up to 128 untouched prefab/scene direct dependencies'))
        except BaseException:
            journal['state']='recovery-required';write_json(stage/'journal.json',journal)
            if journal.get('processExited'):
                try:cleanup(target,fingerprint)
                except Exception as error:
                    journal['cleanupError']=str(error);write_json(stage/'journal.json',journal)
            raise


def recover(transaction,editor,budget=1200):
    from .native_environment import editor_environment
    editor_environment('BEAVER_ADOPT_JOB', transaction)
    stage=safe(transaction);plan,digest=checked_plan(stage/'adoption-plan.json');journal=load(read(stage/'journal.json',max_bytes=128*1024**2),max_nodes=8_000_000)
    if journal.get('planSha256')!=digest or not journal.get('processExited') or journal.get('state') not in ('complete','prepared','recovery-required'):
        fail('ADOPT_RECOVERY_STATE',str(stage),'Expected an exited complete or recoverable transaction')
    if type(budget)is not int or not 1<=budget<=1200:fail('ADOPT_BUDGET',str(budget),'Expected 1..1200 seconds')
    target=safe(plan['targetRoot']);native_lock(target)
    if journal['state'] in ('prepared','recovery-required') and not (stage/'native-apply.json').exists():
        if inside(target,HELPER_ROOT).exists():fail('ADOPT_RECOVERY_HELPER_UNKNOWN',str(target),'Inspect retained helper ownership before recovery')
        # Compilation failed before our method entered. Confirm every affected
        # path is still at its exact before-image before declaring no writes.
        current=project_files(target,plan['targetProfile'])
        for op in plan['operations']:
            for kind,suffix in (('file',''),('meta','.meta')):
                expected=(op.get('before') or {}).get(kind)
                path=(op['source'] or op['target'])+suffix
                observed=current.get(path)
                if (observed or {}).get('sha256')!=(expected or {}).get('sha256'):
                    fail('ADOPT_RECOVERY_CONFLICT',path,'No native journal and target differs from before-image')
                if op['source'] and op['target']!=op['source'] and op['target']+suffix in current:
                    fail('ADOPT_RECOVERY_CONFLICT',op['target'],'Move destination exists without native journal')
        journal.update(state='rolled-back',recovery='no-native-method-entry; affected paths unchanged')
        write_json(stage/'journal.json',journal)
        return dict(status='rolled-back',restored=0,report=str(stage/'journal.json'))
    from .transaction_fs import lock,atomic
    with lock(target):
        # Post-completion rollback must not overwrite any later development.
        if journal['state']=='complete' and project_files(target,plan['targetProfile'])!=journal.get('targetFilesAfter'):
            fail('ADOPT_ROLLBACK_CONFLICT',str(target),'Target changed after adoption')
        state=inside(target,STATE)
        if journal.get('afterStateSha256') and (not state.exists() or digest_file(state)!=journal['afterStateSha256']):
            fail('ADOPT_ROLLBACK_CONFLICT',str(state),'Later adoption state exists')
        fingerprint=helper(target)
        try:
            result=run_child(stage,plan,journal,safe(editor),'rollback',budget);cleanup(target,fingerprint)
            before=stage/'before-state.json'
            if before.exists():atomic(state,read(before,max_bytes=128*1024**2))
            elif state.exists():state.unlink()
            journal['state']='rolled-back';write_json(stage/'journal.json',journal)
            return dict(status='rolled-back',restored=len(result['restored']),report=str(stage/'journal.json'))
        except BaseException:
            if journal.get('processExited'):cleanup(target,fingerprint)
            journal['state']='recovery-required';write_json(stage/'journal.json',journal);raise

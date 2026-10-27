"""Launch one hash-bound local macOS Unity artifact; never attach to other processes."""
from pathlib import Path
import os
import plistlib
import subprocess
from datetime import datetime,timezone
from .io import safe, inside, read, load, new_output, write_json, sha
from .output_limits import digest_file
from .errors import fail,BeaverError
from .payload import verify_manifest


def run(manifest_path,output,duration=10,expected=None):
    path=safe(manifest_path);doc=load(read(path,max_bytes=128*1024**2),max_nodes=8_000_000)
    if expected!=digest_file(path):
        fail('PLAYER_MANIFEST_DRIFT',str(path),'Explicit --expected-manifest must equal the reviewed artifact manifest SHA256')
    if doc.get('schemaVersion')!=1 or not doc.get('sourceSha256') or not isinstance(doc.get('files'),list):
        fail('PLAYER_MANIFEST',str(path),'Expected beaver build artifact manifest')
    root=path.parent;files={row['path']:row for row in doc['files']}
    try:verification=verify_manifest(root,path,scope='player',allow_links=True,artifact=True)
    except BeaverError as error:
        mapping={'PAYLOAD_DRIFT':'PLAYER_ARTIFACT_DRIFT','PAYLOAD_UNLISTED':'PLAYER_UNKNOWN_FILE','PAYLOAD_SYMLINK':'PLAYER_ARTIFACT_DRIFT','PAYLOAD_COLLISION':'PLAYER_MANIFEST_PATH'}
        fail(mapping.get(error.code,error.code),error.location,error.message)
    apps=list((root/'player').glob('*.app'))
    if len(apps)!=1:
        fail('PLAYER_APP',str(root),'Expected one app in build output')
    app=apps[0]
    info=plistlib.loads(read(app/'Contents/Info.plist'))
    name=info.get('CFBundleExecutable')
    if not isinstance(name,str) or Path(name).name!=name:
        fail('PLAYER_EXECUTABLE',str(app),'Invalid app executable')
    executable=safe(app/'Contents/MacOS'/name)
    if not os.access(executable,os.X_OK) or executable.relative_to(root).as_posix() not in files:
        fail('PLAYER_EXECUTABLE',str(executable),'Executable not owned by artifact manifest')
    if type(duration)is not int or not 1<=duration<=60:
        fail('PLAYER_DURATION',str(duration),'Explicit 1..60 second process budget')
    stage=new_output(output,[root]);stopped=False
    args=[str(executable),'-screen-fullscreen','0','-screen-width','960','-screen-height','640','-logFile',str(stage/'player.log')]
    environment=dict(os.environ)
    removed=[name for name in ('BEAVER_NATIVE_JOB','BEAVER_ADOPT_JOB','MONKEY_ADOPT_JOB') if name in environment]
    for name in removed:environment.pop(name)
    started=datetime.now(timezone.utc).isoformat()
    launch=dict(executable=str(executable),executableSha256=files[executable.relative_to(root).as_posix()]['sha256'],
                appRoot=str(app),artifactManifest=str(path),artifactManifestSha256=expected,sourceSha256=doc['sourceSha256'],
                startedUtc=started,budgetSeconds=duration,runDirectory=str(stage),removedStaleControlVariables=removed,
                saveIsolation='not_claimed; project-specific save adapter required',isolation='fresh evidence directory and own bounded process')
    with (stage/'console.log').open('w') as log:
        process=subprocess.Popen(args,cwd=app,env=environment,stdout=log,stderr=subprocess.STDOUT)
        write_json(stage/'process.json',dict(launch,pid=process.pid,owned=True,exited=False))
        try:code=process.wait(timeout=duration)
        except subprocess.TimeoutExpired:
            stopped=True;process.terminate()
            try:code=process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill();code=process.wait(timeout=10)
        except BaseException:
            process.terminate()
            try:process.wait(timeout=10)
            except subprocess.TimeoutExpired:process.kill();process.wait(timeout=10)
            raise
    result=dict(status='ok' if stopped or code==0 else 'failed',pid=process.pid,exitCode=code,
                stoppedByBudget=stopped,sourceSha256=doc['sourceSha256'],artifactManifestSha256=sha(read(path)),
                player='launched and bounded; gameplay/physical input/visual verdicts require separate evidence',
                report=str(stage/'player-result.json'))
    write_json(stage/'process.json',dict(launch,pid=process.pid,owned=True,exited=True,exitCode=code))
    try:
        post=verify_manifest(root,path,scope='player',allow_links=True,artifact=True)
        result.update(artifactVerificationBefore=verification,artifactVerificationAfter=post,launch=launch)
    except ValueError as error:
        result.update(status='failed',artifactVerificationAfter='failed',diagnostic=str(error),launch=launch)
    write_json(stage/'player-result.json',result)
    return result

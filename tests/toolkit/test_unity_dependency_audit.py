import tempfile,subprocess,json,sys
from pathlib import Path
script=str((Path(__file__).resolve().parents[2] / 'src/beaver/toolkit/unity_dependency_audit.py'))
import unittest

class DependencyAuditTests(unittest.TestCase):
 def test_dependency_closure_and_failure_boundaries(self):
  with tempfile.TemporaryDirectory() as d:
   t=Path(d);a=t/'Assets';a.mkdir()
   def put(name,guid,body):
    (a/name).write_text(body);(a/(name+'.meta')).write_text('fileFormatVersion: 2\nguid: '+guid+'\n')
   g1='1'*32;g2='2'*32;g3='3'*32
   put('Root.prefab',g1,'%YAML 1.1\nm_Material: {fileID: 1, guid: '+g2+'}\n')
   put('Mat.mat',g2,'%YAML 1.1\nm_Script: {fileID: 1, guid: '+g3+'}\n')
   put('Host.cs',g3,'class Host {}')
   def run(out,root='Root.prefab'):
    return subprocess.run([sys.executable,script,'--assets',str(a),'--root',root,'--output',str(out)],cwd=str(t),capture_output=True,text=True)
   out=t/'one.json';r=run(out);assert r.returncode==0,r.stderr
   j=json.loads(out.read_text());assert len(j['assets'])==2 and len(j['excluded_code_review_required'])==1
   assert run(out).returncode==2
   put('Second.prefab','4'*32,'%YAML 1.1\nm_Texture: {fileID: 1, guid: '+('9'*32)+'}\n')
   r=run(t/'two.json','Second.prefab');assert r.returncode==1
   put('Duplicate.mat',g2,'%YAML 1.1\n')
   r=run(t/'three.json');assert r.returncode==1
   j=json.loads((t/'three.json').read_text());assert any(i['kind']=='duplicate_guid' for i in j['issues'])
   assert run(t/'escape.json','../elsewhere').returncode==2
   assert run(a/'inside.json').returncode==2
   (a/'Link.prefab').symlink_to(a/'Root.prefab');assert run(t/'link.json','Link.prefab').returncode==2

if __name__ == '__main__':
 unittest.main()

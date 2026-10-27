import tarfile,json,subprocess,sys,tempfile,unittest
from pathlib import Path
SCRIPT=Path(__file__).resolve().parents[2]/'src/beaver/toolkit/package_backup.py'
class BackupTests(unittest.TestCase):
 def test_two_projects_and_restore_plan(self):
  with tempfile.TemporaryDirectory() as tmp:
   t=Path(tmp);projects=[]
   for i in [1,2]:
    p=t/str(i);(p/'Packages').mkdir(parents=True);c=p/'Library/PackageCache/com.example.tool@abc';c.mkdir(parents=True)
    (c/'package.json').write_text(json.dumps({'name':'com.example.tool','version':'1.0.0','dependencies':{}}));(c/'README.md').write_text('same source')
    (p/'Packages/packages-lock.json').write_text(json.dumps({'dependencies':{'com.example.tool':{'version':'1.0.0','source':'registry'}}}));projects.append(p)
   cmd=[sys.executable,str(SCRIPT),'snapshot','--project',str(projects[0]),'--project',str(projects[1]),'--output',str(t/'backup')]
   self.assertEqual(subprocess.run(cmd,capture_output=True).returncode,0)
   d=json.loads((t/'backup/catalog.json').read_text());self.assertEqual(len(d['packages']),1);self.assertEqual(len(d['projects']),2)
   self.assertNotIn(str(t),(t/'backup/catalog.json').read_text())
   self.assertEqual(d['packages'][0]['source_project'],'source-1')
   with tarfile.open(t/'backup'/d['packages'][0]['archive']) as archive:
    for member in archive:
     self.assertEqual((member.uid,member.gid,member.uname,member.gname),(0,0,'',''))
   def plan(out):return subprocess.run([sys.executable,str(SCRIPT),'plan','--backup',str(t/'backup'),'--package','com.example.tool@1.0.0','--output',str(out)],capture_output=True).returncode
   self.assertEqual(plan(t/'plan.json'),0);self.assertEqual(plan(t/'plan.json'),2)
   (t/'backup'/d['packages'][0]['archive']).write_bytes(b'corrupt');self.assertEqual(plan(t/'bad.json'),2)
   (projects[0]/'Library/PackageCache/com.example.tool@abc/README.md').write_text(str(projects[0].resolve()))
   dirty=[sys.executable,str(SCRIPT),'snapshot','--project',str(projects[0]),'--output',str(t/'private-backup')]
   self.assertEqual(subprocess.run(dirty,capture_output=True).returncode,2)
   self.assertFalse((t/'private-backup/catalog.json').exists())
if __name__=='__main__':unittest.main()

import importlib.util,json,struct,subprocess,sys,tempfile,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
def run(name,*args):return subprocess.run([sys.executable,str(ROOT/'src/beaver/toolkit'/name),*map(str,args)],cwd=tempfile.gettempdir(),capture_output=True,text=True)
class P0(unittest.TestCase):
 def test_numeric(self):
  with tempfile.TemporaryDirectory() as tmp:
   t=Path(tmp);a=t/'a.raw';b=t/'b.raw';a.write_bytes(struct.pack('<4e',-2,1.5,0,1));b.write_bytes(struct.pack('<4e',-2,1,0,1))
   args=[a,'--width',1,'--height',1,'--channels',4,'--dtype','f16'];r=run('audit_numeric_texture.py',*args);self.assertEqual(r.returncode,0,r.stderr);d=json.loads(r.stdout);self.assertEqual((d['negative'],d['above_one']),(1,1))
   self.assertEqual(run('audit_numeric_texture.py',*args,'--candidate',b).returncode,1)
   a.write_bytes(struct.pack('>2f',0.25,2));r=run('audit_numeric_texture.py',a,'--width',1,'--height',1,'--channels',2,'--dtype','f32','--endian','big');self.assertEqual(r.returncode,0);self.assertEqual(json.loads(r.stdout)['max'],2)
   self.assertEqual(run('audit_numeric_texture.py',*args).returncode,0) # same byte count, format is caller's contract
   a.write_bytes(b'PNG');self.assertEqual(run('audit_numeric_texture.py',*args).returncode,2)
 def test_trace(self):
  with tempfile.TemporaryDirectory() as tmp:
   t=Path(tmp);a=t/'a';b=t/'b';row={'seq':1,'event':'hit','state':{'count':2}};a.write_text(json.dumps(row)+'\n');b.write_text(a.read_text())
   self.assertEqual(run('compare_events.py',a,b).returncode,0)
   row['state']['count']=1;b.write_text(json.dumps(row)+'\n');r=run('compare_events.py',a,b);self.assertEqual(r.returncode,1);self.assertEqual(json.loads(r.stdout)['field'],'/state')
   b.write_text('');self.assertEqual(run('compare_events.py',a,b).returncode,2)
   b.write_text(a.read_text()*2);self.assertEqual(run('compare_events.py',a,b).returncode,2)
 def test_output_conflict(self):
  with tempfile.TemporaryDirectory() as tmp:
   t=Path(tmp);p=t/'a';p.write_bytes(struct.pack('<e',1));o=t/'o';o.write_text('keep');r=run('audit_numeric_texture.py',p,'--width',1,'--height',1,'--channels',1,'--dtype','f16','--output',o);self.assertEqual(r.returncode,2);self.assertEqual(o.read_text(),'keep')
if __name__=='__main__':unittest.main()

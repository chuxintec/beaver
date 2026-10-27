import copy
import json
from pathlib import Path
import tempfile
import unittest
from beaver.toolkit.config_preflight import inspect as config
from beaver.toolkit.performance_compare import compare
from beaver.toolkit.presentation_audit import inspect as presentation

class ObservationTests(unittest.TestCase):
    def test_config_presence_numbers_arrays_and_references(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder).resolve(); rows=[{'id':1,'rate':0.123456789012345,'targets':[0,1],'expr':'random()','empty':''},{'id':2,'rate':0.0,'targets':[],'expr':None}]
            fields=[{'name':'id','type':'int'},{'name':'rate','type':'double'},{'name':'targets','type':'int','array':True},{'name':'expr','type':'string','nullable':True},{'name':'empty','type':'string','optional':True}]
            c={'schema_version':1,'codec':'typed-json-v1','tables':[{'name':'A','path':'rows.json','authoring':'json','key':'id','fields':fields}], 'references':[{'from_table':'A','field':'targets','to_table':'A','key':'id','sentinels':[0]}]}
            (root/'rows.json').write_text(json.dumps(rows));(root/'contract.json').write_text(json.dumps(c))
            r=config(root/'contract.json');self.assertEqual(r['tables'][0]['states'][1]['empty'],'missing');self.assertEqual(r['tables'][0]['states'][1]['expr'],'null');self.assertEqual(r['tables'][0]['rows'][0]['rate'],rows[0]['rate'])
            rows[0]['targets']=[9];(root/'rows.json').write_text(json.dumps(rows))
            with self.assertRaisesRegex(ValueError,'reference'):config(root/'contract.json')
            c['references']=[];(root/'contract.json').write_text(json.dumps(c));config(root/'contract.json')

    def test_perf_refuses_mixed_conditions_and_unknown_zero(self):
        run={'schema_version':1,'candidate_sha256':'a'*64,'identity':dict(workload_sha256='1'*64,device='fixture',platform='mac',viewport=[540,900],quality='fixed',seed=1,input_sha256='2'*64,measurement='frame-time-v1'),'samples':[dict(frame=i,phase='steady',focused=True,valid=True,frame_ms=16+i) for i in range(4)],'counters':{'gpu':dict(supported=False,value=None,unit='ms')}}
        other=copy.deepcopy(run);other['candidate_sha256']='b'*64
        self.assertEqual(compare(run,other)['steady_delta_percent']['p95'],0)
        other['identity']['quality']='low'
        with self.assertRaisesRegex(ValueError,'Incomparable'):compare(run,other)
        other=copy.deepcopy(run);other['samples'][0]['focused']=False
        with self.assertRaisesRegex(ValueError,'background'):compare(run,other)
        other=copy.deepcopy(run);other['counters']['gpu']['value']=0
        with self.assertRaisesRegex(ValueError,'null'):compare(run,other)

    def test_additive_pool_and_stale_callback(self):
        nodes=[dict(id='additive',active=True,blend='additive_pma',effective_alpha=0,mesh_vertices=4),dict(id='label',active=True,text_length=4,mesh_vertices=0),dict(id='effect',active=True,track_time=1.0)]
        r={'schema_version':1,'candidate_sha256':'a'*64,'run_instance':'fixture','frames':[dict(frame=1,phase='cold_open',nodes=nodes),dict(frame=2,phase='pool_reuse',nodes=[dict(id='effect',active=True,expected_restart=True,track_time=1.5,generation=2,callback_generation=1)])]}
        findings=presentation(r)['findings'];self.assertEqual({x['code'] for x in findings},{'TEXT_WITHOUT_MESH','POOL_RESTART_NOT_OBSERVED','STALE_LIFETIME_CALLBACK'})
        self.assertFalse(presentation(r)['lifecycle_coverage'])

class CsvAndBinaryTests(unittest.TestCase):
    def test_normalized_csv_requires_explicit_metadata(self):
        from beaver.toolkit.performance_compare import read_run,sample_summary
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder).resolve();csv=root/'frames.csv';meta=root/'meta.json'
            csv.write_text('frame,phase,focused,valid,frame_ms\n1,steady,true,true,16.0\n2,steady,true,true,17.0\n')
            context={'schema_version':1,'candidate_sha256':'a'*64,'identity':dict(workload_sha256='b'*64,device='fixture',platform='mac',viewport=[540,900],quality='fixed',seed=0,input_sha256='c'*64,measurement='wall-ms-v1'),'counters':{}}
            meta.write_text(json.dumps(context));r,digest=read_run(csv,meta);self.assertEqual(sample_summary(r)['steady']['count'],2)
            csv.write_text('frame,phase,focused,valid,frame_ms\n1,steady,True,true,16\n')
            with self.assertRaises(ValueError):read_run(csv,meta)

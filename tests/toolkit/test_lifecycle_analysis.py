import copy,json,hashlib,tempfile,unittest
from pathlib import Path
from beaver.toolkit.compatibility_plan import analyze
from beaver.toolkit.operations_preview import analyze_events,config_preview
class LifecycleAnalysis(unittest.TestCase):
 def test_config_identity_is_not_save_schema_and_pending_is_preserved(self):
  with tempfile.TemporaryDirectory() as td:
   r=Path(td).resolve();a=r/'a.json';b=r/'b.json';a.write_text('{"reward":1}');b.write_text('{"reward":2}')
   fields={'balance':dict(type='integer',required=True),'pending':dict(type='string',required=True)}
   base=dict(schema_version=1,config=dict(path='a.json',sha256=hashlib.sha256(a.read_bytes()).hexdigest()),save=dict(version=1,fields=fields),protocol_version='v1',pending_fields=['pending'])
   target=copy.deepcopy(base);target['config']=dict(path='b.json',sha256=hashlib.sha256(b.read_bytes()).hexdigest())
   before=r/'before.json';after=r/'after.json';sample=r/'save.json';before.write_text(json.dumps(base));after.write_text(json.dumps(target));sample.write_text(json.dumps(dict(_schema_version=1,balance=7,pending='same-command')));old=sample.read_bytes()
   result=analyze(before,after,[sample],r/'plan');self.assertTrue(result['config_changed']);self.assertEqual(result['compatibility'],'declared_top_level_compatible');self.assertEqual(sample.read_bytes(),old)
   target['save']['version']=2;target['save']['fields']['label']=dict(type='string',required=True,default='owned');after.write_text(json.dumps(target));result=analyze(before,after,[sample],r/'added');self.assertEqual(result['samples'][0]['candidate']['label'],'owned');self.assertEqual(result['samples'][0]['candidate']['pending'],'same-command')
   target['pending_fields']=[];after.write_text(json.dumps(target));self.assertEqual(analyze(before,after,[sample],r/'bad')['compatibility'],'needs_action')
   sample.write_text('{"_schema_version":99}')
   with self.assertRaises(ValueError):analyze(before,after,[sample],r/'future')
   self.assertFalse((r/'future').exists())
 def test_funnel_time_order_closed_retention_and_economy_receipts(self):
  with tempfile.TemporaryDirectory() as td:
   r=Path(td).resolve();p=r/'events.jsonl'
   def event(id,player,session,time,type,**kw):return dict(schema_version=1,event_id=id,event_type=type,event_time=time,player_id=player,session_id=session,game_version='own-v1',config_version='own-c1',environment='synthetic',**kw)
   rows=[event('1','a','s1','2026-10-20T08:00:00+08:00','start'),event('2','a','s1','2026-10-20T08:01:00+08:00','finish'),event('3','a','s1','2026-10-20T08:02:00+08:00','claim',economy=dict(kind='grant',asset_id='coin',quantity='7',receipt_id='once')),event('4','a','s2','2026-10-21T08:00:00+08:00','start'),event('5','b','s3','2026-10-20T09:00:00+08:00','start'),event('6','b','s3','2026-10-20T09:00:00+08:00','finish')]
   rows.append(copy.deepcopy(rows[2]));rows.append(dict(rows[2],event_id='7',event_time='2026-10-20T08:03:00+08:00'))
   p.write_text(''.join(json.dumps(v)+'\n' for v in rows));kwargs=dict(start='2026-10-20T00:00:00+08:00',end='2026-10-22T00:00:00+08:00',as_of='2026-10-22T00:00:00+08:00',timezone='Asia/Shanghai',steps=['start','finish','claim'],active_event='start',data_kind='simulation')
   result=analyze_events(p,r/'report',**kwargs);group=result['groups'][0]
   self.assertEqual(group['funnel']['counts'],[3,1,1]);self.assertEqual(group['retention']['d1'][0]['rate'],0.5);self.assertEqual(group['economy'][0]['grants'],7);self.assertFalse(group['economy'][0]['stock_reconciled']);self.assertEqual(result['duplicate_events_ignored'],1);self.assertEqual(group['economy'][0]['duplicate_receipts'],1)
   rows.append(dict(rows[0],event_type='different'));p.write_text(''.join(json.dumps(v)+'\n' for v in rows))
   with self.assertRaises(ValueError):analyze_events(p,r/'conflict',**kwargs)
 def test_missing_denominator_is_null_and_open_cohort_is_not_zero_retention(self):
  with tempfile.TemporaryDirectory() as td:
   r=Path(td).resolve();p=r/'events';p.write_text(json.dumps(dict(schema_version=1,event_id='1',event_type='start',event_time='2026-10-20T08:00:00+08:00',player_id='p',session_id='s',game_version='v',config_version='c',environment='synthetic'))+'\n')
   result=analyze_events(p,r/'out','2026-10-20T00:00:00+08:00','2026-10-21T00:00:00+08:00','2026-10-21T00:00:00+08:00','Asia/Shanghai',['unseen'],active_event='start');g=result['groups'][0];self.assertEqual(g['funnel']['conversion'],[None]);self.assertEqual(g['retention']['d1'],[]);self.assertEqual(g['retention']['open_cohorts_excluded'],1)
 def test_config_preview_is_self_contained_and_never_applies(self):
  with tempfile.TemporaryDirectory() as td:
   r=Path(td).resolve();a=r/'a';b=r/'b';schema=r/'schema';target=r/'target';a.write_text('{"reward":1}');b.write_text('{"reward":2}');schema.write_text(json.dumps(dict(type='object',required=['reward'],properties=dict(reward=dict(type='integer',minimum=0)),additionalProperties=False)));target.write_text(json.dumps(dict(schema_version=1,environment='synthetic',audience='local-fixture',effective_at='2026-10-23T00:00:00+08:00',client_versions=['own-v1'],approval_reference='caller-declared',rollback_limits='config only; does not reverse rewards',platform='none')))
   before=a.read_bytes();result=config_preview(a,b,schema,target,r/'preview');self.assertEqual(result['changes'][0]['path'],'/reward');self.assertEqual(result['status'],'candidate_not_applied');self.assertEqual(result['production_write'],'Unavailable');self.assertEqual(a.read_bytes(),before)
   schema.write_text(json.dumps({'$dynamicRef':'https://invalid.example/schema'}))
   with self.assertRaises(ValueError):config_preview(a,b,schema,target,r/'network')
   self.assertFalse((r/'network').exists())

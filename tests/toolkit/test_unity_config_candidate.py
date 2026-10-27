import copy
import json
from pathlib import Path
import tempfile
import unittest
from beaver.toolkit.unity_config_candidate import generate


class ConfigurationCandidateTests(unittest.TestCase):
    def test_two_shapes_readback_and_manual_conflict(self):
        for name, fields, rows in [
            ('Enemy', [{'name':'Id','type':'int','min':1}, {'name':'Health','type':'int','min':1}], [{'Id':1,'Health':25}]),
            ('Hint', [{'name':'Id','type':'string'}, {'name':'Enabled','type':'bool'}, {'name':'Text','type':'string','nullable':True}], [{'Id':'intro','Enabled':False,'Text':None}, {'Id':'empty','Enabled':True,'Text':''}])]:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as t:
                root=Path(t).resolve(); schema=root/'schema.json'; source=root/'rows.json'; out=root/'candidate'
                schema.write_text(json.dumps(dict(schema_version=1,namespace='Own.Configuration',name=name,key='Id',fields=fields)))
                source.write_text(json.dumps(rows))
                report=generate(schema,source,out)
                self.assertEqual(json.loads((out/'source-rows.json').read_text()),rows)
                self.assertEqual(generate(schema,source,out)['status'],'unchanged')
                self.assertIn(name+'Table', (out/(name+'Table.cs')).read_text())
                target=out/(name+'Table.cs'); target.write_text(target.read_text()+'// manual\n')
                with self.assertRaisesRegex(ValueError,'conflict'): generate(schema,source,out)
                self.assertTrue(target.read_text().endswith('// manual\n'))
                self.assertEqual(report['status'],'candidate_created')

    def test_preflight_duplicate_missing_null_range_and_identifier(self):
        good=dict(schema_version=1,namespace='Own.Config',name='Enemy',key='Id',fields=[dict(name='Id',type='int',min=1)])
        for schema, rows in [(good,[{'Id':1},{'Id':1}]), (good,[{}]), (good,[{'Id':None}]), (good,[{'Id':False}]), (good,[{'Id':0}]), (good,[{'Id':1,'Extra':2}]), (dict(good,name='class'),[{'Id':1}])]:
            with tempfile.TemporaryDirectory() as t:
                root=Path(t).resolve(); s=root/'schema.json'; data=root/'input.json'; out=root/'output'
                s.write_text(json.dumps(schema));data.write_text(json.dumps(rows))
                with self.assertRaises(ValueError): generate(s,data,out)
                self.assertFalse(out.exists())

import json
from pathlib import Path
import tempfile
import unittest
from decimal import Decimal

from beaver.configuration_v3 import compare, generate, validate
from beaver.io import sha
from beaver.errors import BeaverError
from beaver.io import load, read, write_json


def schema():
    return {
        'schemaVersion': 2, 'id': 'fixture-v2', 'rootName': 'Root', 'namespace': 'Example.Config',
        'root': {'type': 'object', 'additionalProperties': False, 'required': ['version','rows','lookup','mode'],
                 'properties': {
                     'version': {'type':'string'},
                     'rows': {'type':'array','items': {'type':'object','additionalProperties':False,'required':['id','count','ratio','mode','child'],
                                 'properties': {'id':{'type':'integer','bits':64},'count':{'type':'integer','minimum':1,'maximum':5},
                                                'ratio':{'type':'decimal','maxScale':3},'mode':{'type':'enum','base':'string','values':['a','b']},
                                                'child':{'type':'object','additionalProperties':False,'required':['name','tag'],
                                                         'properties':{'name':{'type':'string'},'tag':{'type':'string','nullable':True}}}}}},
                     'lookup': {'type':'dictionary','values':{'type':'array','items':{'type':'integer','bits':64}}},
                     'mode': {'type':'enum','base':'integer','values':[0,2]},
                     'optional': {'type':'integer','bits':64,'nullable':True,'default':{'policy':'preserve'}}}},
        'tables':[{'name':'rows','path':'rows','id':'id'}],
        'references':[{'path':'lookup.*[]','table':'rows','field':'id'}],
    }


def valid_doc():
    return {'version':'v1','rows':[{'id':2**45,'count':3,'ratio':Decimal('1.125'),'mode':'a','child':{'name':'n','tag':None}}],
            'lookup':{'slot':[2**45]},'mode':2}


class ConfigurationV3Tests(unittest.TestCase):
    def test_nested_types_nullable_dictionary_enums_and_big_integer(self):
        result=validate(schema(),valid_doc())
        self.assertEqual(result['status'],'ok')
        self.assertEqual(result['counts']['tables']['rows'],1)
        self.assertEqual(result['defaultsApplied'],[])

    def test_decimal_is_bounded_to_shared_exact_csharp_go_range(self):
        document=valid_doc();document['rows'][0]['ratio']=Decimal('1e-29')
        self.assertIn('DECIMAL_PRECISION',{x['code'] for x in validate(schema(),document)['diagnostics']})

    def test_null_default_requires_explicit_nullable_and_value_default_is_validated(self):
        invalid=schema();invalid['root']['properties']['optional'].pop('nullable');invalid['root']['properties']['optional']['default']={'policy':'null'}
        with self.assertRaisesRegex(BeaverError,'CONFIG_SCHEMA'):
            validate(invalid,valid_doc())
        invalid=schema();invalid['root']['properties']['optional']['default']={'policy':'value','value':'wrong'}
        with self.assertRaisesRegex(BeaverError,'CONFIG_SCHEMA_DEFAULT'):
            validate(invalid,valid_doc())
        document=valid_doc();document['rows'][0]['ratio']=Decimal('79228162514264337593543950336')
        self.assertIn('DECIMAL_PRECISION',{x['code'] for x in validate(schema(),document)['diagnostics']})

    def test_missing_unknown_null_numeric_precision_and_foreign_key_are_explicit(self):
        document=valid_doc();del document['rows'][0]['count'];document['unexpected']=True
        document['rows'][0]['child']['tag']=''
        document['lookup']['slot']=[999]
        result=validate(schema(),document)
        codes={item['code'] for item in result['diagnostics']}
        self.assertIn('REQUIRED_MISSING',codes)
        self.assertIn('UNKNOWN_FIELD',codes)
        self.assertIn('FOREIGN_KEY_MISSING',codes)
        document=valid_doc();document['rows'][0]['ratio']=Decimal('1.12345')
        self.assertIn('NUMBER_SCALE',{x['code'] for x in validate(schema(),document)['diagnostics']})
        document=valid_doc();document['rows'][0]['child']['tag']=None
        self.assertEqual(validate(schema(),document)['status'],'ok')
        document=valid_doc();document['rows'][0]['child']['name']=None
        self.assertIn('NULL_NOT_ALLOWED',{x['code'] for x in validate(schema(),document)['diagnostics']})

    def test_duplicate_json_members_and_duplicate_ids_reject(self):
        raw=b'{"version":"x","version":"y"}'
        with self.assertRaisesRegex(BeaverError,'CONFIG_JSON'):
            validate(schema(),raw)
        document=valid_doc();document['rows'].append(dict(document['rows'][0]))
        self.assertIn('DUPLICATE_ID',{x['code'] for x in validate(schema(),document)['diagnostics']})

    def test_external_table_and_resource_path_guid_checks(self):
        external=schema();external['references']=[{'path':'lookup.slot[]','table':'externalItems','field':'id'}]
        ok=validate(external,valid_doc(),tables={'externalItems':[2**45]})
        self.assertEqual(ok['status'],'ok')
        with tempfile.TemporaryDirectory(dir='/private/tmp') as temp:
            root=Path(temp);(root/'Assets').mkdir();(root/'Assets'/'item.json').write_text('{}')
            doc={'resource':'Assets/item.json'}
            s={'schemaVersion':2,'root':{'type':'object','required':['resource'],'properties':{'resource':{'type':'string'}}},
               'resources':[{'path':'resource','kind':'assetPath'}]}
            self.assertEqual(validate(s,doc,project=root)['status'],'ok')
            doc['resource']='Assets/missing.json'
            self.assertIn('RESOURCE_PATH_MISSING',{x['code'] for x in validate(s,doc,project=root)['diagnostics']})

    def test_compare_reports_breaking_and_additive_shape_changes(self):
        old=schema();new=schema();new['root']['required'].remove('version');new['root']['properties']['version']['default']={'policy':'preserve'}
        new['root']['properties']['extra']={'type':'boolean','required':False,'default':{'policy':'value','value':False}}
        report=compare(old,new,valid_doc(),valid_doc())
        self.assertEqual(report['compatibility'],'backward_compatible')
        self.assertIn('optional_property_added',[x['change'] for x in report['changes']])
        breaking=schema();breaking['root']['required'].append('extra')
        breaking['root']['properties']['extra']={'type':'string'}
        self.assertEqual(compare(old,breaking)['compatibility'],'breaking')

    def test_generation_is_owned_diffs_and_refuses_hand_edit(self):
        with tempfile.TemporaryDirectory(dir='/private/tmp') as temp:
            root=Path(temp);spec=root/'schema.json';write_json(spec,schema());out=root/'generated'
            first=generate(spec,out)
            self.assertEqual(first['status'],'ok')
            self.assertTrue((out/'ConfigurationDtos.g.cs').is_file())
            self.assertTrue((out/'ConfigurationJsonRuntime.g.cs').is_file())
            self.assertTrue((out/'ConfigurationTypes.go').is_file())
            dto=(out/'ConfigurationDtos.g.cs').read_text()
            runtime=(out/'ConfigurationJsonRuntime.g.cs').read_text()
            self.assertIn('long Id',dto)
            self.assertIn('JsonTextReader',runtime)
            self.assertIn('MissingMemberHandling.Error',runtime)
            self.assertIn('type RootType = Root',(out/'ConfigurationTypes.go').read_text())
            self.assertIn('ReadConfiguration[T any]',(out/'BeaverJsonRuntime.go').read_text())
            self.assertIn('ConfigSnapshot',(out/'ConfigurationJsonRuntime.g.cs').read_text())
            self.assertIn('package configv3',(out/'ConfigurationTypes.go').read_text())
            self.assertIn('CheckDecimalTokens',(out/'ConfigurationJsonRuntime.g.cs').read_text())
            generated_schema=(out/'configuration.schema.json').read_bytes()
            self.assertEqual(first['schemaSha256'],sha(generated_schema))
            self.assertEqual(generate(spec,out)['status'],'ok')
            target=out/'ConfigurationDtos.g.cs';target.write_text(target.read_text()+'// manual edit\n')
            blocked=generate(spec,out)
            self.assertEqual(blocked['status'],'blocked')
            self.assertIn('ConfigurationDtos.g.cs',blocked['modifiedOwnedFiles'])


if __name__=='__main__':unittest.main()

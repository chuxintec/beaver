"""Read declared save/config/protocol contracts; create candidates, never migrate player data."""
import argparse,copy,json,math,hashlib
from beaver.toolkit.support.host.paths import Path
from beaver.toolkit.support.common import hash_file,write_new,GreaError
from beaver.toolkit.support.safe_paths import canonical_path,disjoint,relative_name

def read(path):
    path=canonical_path(path)
    before=hash_file(path,4*1024**2);raw=path.read_bytes()
    if len(raw)>4*1024**2 or hashlib.sha256(raw).hexdigest()!=before['sha256'] or hash_file(path)!=before:raise ValueError('JSON changed while reading')
    return json.loads(raw.decode('utf-8'),parse_constant=lambda _:(_ for _ in ()).throw(ValueError('Nonfinite JSON')))

def matches(value,kind):
    return {'string':isinstance(value,str),'integer':type(value) is int,'number':type(value) in (int,float) and math.isfinite(value),'boolean':type(value) is bool,'object':type(value) is dict,'array':type(value) is list,'null':value is None}.get(kind,False)

def contract(path):
    value=read(path)
    if set(value)!={'schema_version','config','save','protocol_version','pending_fields'} or (type(value['schema_version']) is not int or value['schema_version']!=1):raise ValueError('Expected compatibility contract v1')
    config=value['config'];save=value['save']
    if set(config)!={'path','sha256'} or hash_file(Path(path).resolve().parent/relative_name(config['path']),16*1024**2)['sha256']!=config['sha256']:raise ValueError('Config identity mismatch')
    read(Path(path).resolve().parent/relative_name(config['path']))
    if set(save)!={'version','fields'} or type(save['version']) is not int or save['version']<1 or not isinstance(save['fields'],dict) or len(save['fields'])>256:raise ValueError('Invalid save schema')
    for name,field in save['fields'].items():
        if not name or name=='_schema_version' or not isinstance(field,dict) or set(field)-{'type','required','default'} or not {'type','required'}<=set(field) or type(field['required']) is not bool or field['type'] not in ('string','integer','number','boolean','object','array','null'):raise ValueError('Invalid top-level save field')
        if 'default' in field and not matches(field['default'],field['type']):raise ValueError('Default has wrong type')
    if not isinstance(value['pending_fields'],list) or len(set(value['pending_fields']))!=len(value['pending_fields']) or not set(value['pending_fields'])<=save['fields'].keys():raise ValueError('Pending fields must name declared save fields')
    if not isinstance(value['protocol_version'],str) or not value['protocol_version']:raise ValueError('Explicit protocol version required')
    return value

def analyze(before,after,samples,output):
    old=contract(before);new=contract(after)
    if len(samples)>100:raise ValueError('At most 100 explicit save samples')
    if new['save']['version']<old['save']['version']:raise ValueError('Save schema downgrade requires explicit separate design')
    a=old['save']['fields'];b=new['save']['fields'];issues=[]
    for field in a.keys()-b.keys():issues.append(dict(field=field,kind='removed_field_needs_business_migration'))
    for field in a.keys()&b.keys():
        if a[field]['type']!=b[field]['type']:issues.append(dict(field=field,kind='type_change_needs_business_migration'))
    for field in old['pending_fields']:
        if field not in new['pending_fields'] or b.get(field)!=a[field]:issues.append(dict(field=field,kind='pending_identity_must_be_preserved'))
    for field in b:
        if b[field]['required'] and not a.get(field,{}).get('required',False) and 'default' not in b[field]:issues.append(dict(field=field,kind='new_required_field_without_default'))
    if old['protocol_version']!=new['protocol_version']:issues.append(dict(kind='protocol_change_requires_backend_contract_review'))
    if a!=b and new['save']['version']==old['save']['version']:issues.append(dict(kind='save_shape_changed_without_schema_version'))
    if sum(hash_file(p,4*1024**2)['bytes'] for p in samples)>16*1024**2:raise ValueError('Total save sample budget exceeds 16 MiB')
    rows=[];config_paths=[Path(p).resolve().parent/v['config']['path'] for p,v in [(before,old),(after,new)]]
    out=disjoint(output,before,after,*samples,*config_paths)
    if out.exists() or not out.parent.is_dir():raise ValueError('Use a new output directory')
    for path in samples:
        source=read(path)
        if not isinstance(source,dict) or type(source.get('_schema_version')) is not int or source['_schema_version'] not in (old['save']['version'],new['save']['version']):raise ValueError('Unknown save schema version; do not overwrite or guess downgrade')
        fields=a if source['_schema_version']==old['save']['version'] else b
        for key,field in fields.items():
            if key in source and not matches(source[key],field['type']) or field['required'] and key not in source:raise ValueError('Sample does not match declared source schema')
        candidate=copy.deepcopy(source);local=[]
        for key in source.keys()-fields.keys()-{'_schema_version'}:local.append(dict(field=key,kind='undeclared_field_preserved_needs_review'))
        for key,field in b.items():
            if key not in candidate and 'default' in field:candidate[key]=copy.deepcopy(field['default'])
        candidate['_schema_version']=new['save']['version']
        rows.append(dict(source=hash_file(path),candidate=candidate if not issues and not local else None,issues=local))
    result=dict(schema_version=1,status='preview_not_applied',compatibility='needs_action' if issues or any(x['issues'] for x in rows) else 'declared_top_level_compatible',config_changed=old['config']['sha256']!=new['config']['sha256'],save_versions=[old['save']['version'],new['save']['version']],issues=issues,samples=rows,inputs=[hash_file(p) for p in (before,after,*config_paths)],limitations=['Only declared top-level types/default additions are analyzed; units, enum/ID meanings and nested schemas need business evidence.','Config content hash alone does not invalidate an old save. No arbitrary migration code, ordinary save overwrite or database access.','Candidates are local examples only; confirmed server assets and pending/receipt identities must not be rolled back from client snapshots.'])
    out.mkdir();write_new(out/'compatibility.json',result);return result

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--before',required=True);p.add_argument('--after',required=True);p.add_argument('--sample',action='append',default=[]);p.add_argument('--output',required=True);a=p.parse_args()
    try:r=analyze(a.before,a.after,a.sample,a.output);print(json.dumps(dict(status=r['status'],compatibility=r['compatibility'])));return 1 if r['compatibility']=='needs_action' else 0
    except (OSError,ValueError,TypeError,KeyError,GreaError,RecursionError) as e:print(str(e));return 2
if __name__=='__main__':raise SystemExit(main())

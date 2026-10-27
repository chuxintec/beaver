"""Explicit UI field corrections lowered to existing batch actions."""
from .io import safe, inside, load, read, write_json, new_output
from .output_limits import digest_file
from .errors import fail


def actions(project, snapshot, repairs):
    if not isinstance(repairs,list) or len(repairs)>1000:
        fail('UI_REPAIRS','repairs','Expected at most 1000 explicit field repairs')
    rows=[];seen=set()
    for repair in repairs:
        if not isinstance(repair,dict) or set(repair)!={'asset','nodePath','componentType','field','mode','value','source'}:
            fail('UI_REPAIR_SPEC','repair','Explicit asset/node/component/field/mode/value/source required')
        if not isinstance(repair['source'],str) or not repair['source'].strip():fail('UI_REPAIR_SOURCE','source','Expected provenance for the approved correction')
        assets=[a for a in snapshot['assets'] if a['path']==repair['asset']]
        if len(assets)!=1 or not repair['asset'].endswith('.prefab'):fail('UI_REPAIR_ASSET',repair['asset'],'Exactly one inspected Prefab required')
        asset=assets[0]
        for key,suffix in (('sourceSha256',''),('metaSha256','.meta')):
            if digest_file(inside(project,repair['asset']+suffix))!=asset.get(key):fail('UI_REPAIR_DRIFT',repair['asset'],'Inspected source/meta changed')
        fields=[f for c in asset['components'] if c['nodePath']==repair['nodePath'] and c['type']==repair['componentType'] for f in c.get('fields',[]) if f.get('propertyPath',f.get('name'))==repair['field']]
        if len(fields)!=1:fail('UI_REPAIR_FIELD',repair['field'],'Exactly one inspected serialized field required')
        if fields[0].get('referenceState') in ('null','bound','missing') or repair['field'].split('.')[0] in ('m_Script','m_GameObject','m_PrefabAsset','m_PrefabInstance','m_CorrespondingSourceObject','m_Father','m_Children'):
            fail('UI_REPAIR_REFERENCE',repair['field'],'Identity and ObjectReference changes require the guarded wiring contract')
        key=(repair['asset'],repair['nodePath'],repair['componentType'],repair['field'])
        if key in seen:fail('UI_REPAIR_DUPLICATE',repair['field'],'Duplicate correction destination')
        seen.add(key)
        mode,value=repair['mode'],repair['value']
        if mode not in ('float','int','bool','string','enum','vector2','color'):fail('UI_REPAIR_MODE',mode,'Explicit scalar/vector2/color modes only')
        action=dict(kind='property.set',path=repair['nodePath'],component=repair['componentType'],property=repair['field'],mode=mode)
        if mode in ('vector2','color'):
            if not isinstance(value,list) or len(value)!=(2 if mode=='vector2' else 4) or any(type(x) not in (int,float) for x in value):fail('UI_REPAIR_VALUE',mode,'Exact finite numeric vector required')
            action['numbers']=value
        else:
            if mode=='bool' and type(value)is not bool or mode in ('int','enum') and type(value)is not int or mode=='float' and type(value)not in (int,float) or mode=='string' and not isinstance(value,str):fail('UI_REPAIR_VALUE',mode,'Value must match the explicit mode')
            action['value']=str(value).lower() if mode=='bool' else str(value)
        rows.append((repair['asset'],action))
    return rows


def plan(project,snapshot_path,spec_path,output):
    root=safe(project);snapshot=load(read(snapshot_path));spec=load(read(spec_path))
    if spec.get('schemaVersion')!=1 or set(spec)!={'schemaVersion','repairs'}:fail('UI_REPAIR_SPEC','spec','Expected schemaVersion 1 and explicit repairs')
    if snapshot.get('schemaVersion')!=1 or snapshot.get('mode')!='static-prefab':fail('UI_REPAIR_SNAPSHOT','snapshot','Inspected static Prefab snapshot required')
    grouped={}
    for path,action in actions(root,snapshot,spec['repairs']):grouped.setdefault(path,[]).append(action)
    if not grouped:fail('UI_REPAIR_EMPTY','repairs','At least one approved correction required')
    stage=new_output(output,[root,snapshot_path,spec_path])
    batch=dict(schemaVersion=2,operation='batch',steps=[dict(kind='prefab.patch',assetPath=p,actions=a) for p,a in grouped.items()])
    write_json(stage/'batch-spec.json',batch)
    from .batch import plan as batch_plan
    result=batch_plan(root,stage/'batch-spec.json',stage/'batch-plan')
    write_json(stage/'repair-sources.json',spec)
    return result

"""Schema-driven validation and C# Json.NET DTO/runtime generation."""
from copy import deepcopy
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import uuid

from .errors import BeaverError, budget, fail
from .io import encoded, load, new_output, read, safe, sha, write_json
from .output_limits import digest_file

SCHEMA_VERSION = 2
GENERATOR = 'beaver-config-v3/1'
MAX_INPUT_BYTES = 64 * 1024 * 1024
MAX_DIAGNOSTICS = 5000
MAX_RESOURCE_FILES = 250_000
_GUID = re.compile(r'^[0-9a-fA-F]{32}$')


def _duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate object key: '+key)
        result[key] = value
    return result


def _loads(raw, location):
    if len(raw) > MAX_INPUT_BYTES:
        fail('CONFIG_INPUT_BUDGET', location, 'JSON exceeds the 64 MiB input bound')
    try:
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=_duplicates,
                           parse_int=int, parse_float=Decimal,
                           parse_constant=lambda token: (_ for _ in ()).throw(ValueError('non-finite number')))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        fail('CONFIG_JSON', location, 'Invalid strict UTF-8 JSON: '+str(error)[:240])
    _budget(value, location)
    return value


def _budget(value, location):
    stack=[(value,0)];count=0
    while stack:
        item,depth=stack.pop();count+=1
        if count>2_000_000 or depth>128:
            fail('CONFIG_BUDGET', location, 'JSON exceeds the node/depth bound')
        if isinstance(item,Decimal) and not item.is_finite():
            fail('CONFIG_NUMBER', location, 'Non-finite decimal is not accepted')
        if isinstance(item,str):
            try:item.encode('utf-8','strict')
            except UnicodeEncodeError:fail('CONFIG_UNICODE',location,'Unpaired Unicode surrogate')
        if isinstance(item,float) and (item!=item or abs(item)==float('inf')):
            fail('CONFIG_NUMBER',location,'Non-finite number is not allowed')
        if isinstance(item,dict):stack.extend((v,depth+1) for v in item.values())
        elif isinstance(item,list):stack.extend((v,depth+1) for v in item)


def _document(value, label):
    if isinstance(value, dict):
        _budget(value,label);return value,None
    if isinstance(value, bytes):
        return _loads(value,label),hashlib.sha256(value).hexdigest()
    if isinstance(value, (str,Path)):
        path=safe(value)
        raw=read(path,MAX_INPUT_BYTES)
        return _loads(raw,str(path)),hashlib.sha256(raw).hexdigest()
    fail('CONFIG_INPUT',label,'Expected an object, UTF-8 JSON bytes, or JSON file path')


def _resolve(schema, node):
    if not isinstance(node,dict):return node
    if '$ref' not in node:return node
    ref=node['$ref']
    if not isinstance(ref,str) or not ref.startswith('#/definitions/'):
        fail('CONFIG_SCHEMA_REF',str(ref),'Only local #/definitions references are supported')
    name=ref.split('/',2)[2].replace('~1','/').replace('~0','~')
    target=schema.get('definitions',{}).get(name)
    if not isinstance(target,dict):fail('CONFIG_SCHEMA_REF',ref,'Definition is missing')
    if len(node)>1:
        merged=deepcopy(target);merged.update({k:v for k,v in node.items() if k!='$ref'});return merged
    return target


def _check_schema(schema):
    if not isinstance(schema,dict) or schema.get('schemaVersion')!=SCHEMA_VERSION or not isinstance(schema.get('root'),dict):
        fail('CONFIG_SCHEMA','schema','Expected beaver configuration schemaVersion 2')
    for key in ('rootName','namespace','goPackage'):
        if key in schema and (not isinstance(schema[key],str) or not schema[key].strip()):
            fail('CONFIG_SCHEMA',key,'Expected a non-empty generated name')
    allowed={'object','array','dictionary','string','integer','decimal','boolean','enum'}
    stack=[schema['root']]
    for name,definition in schema.get('definitions',{}).items():
        if not isinstance(name,str) or not isinstance(definition,dict):fail('CONFIG_SCHEMA',name,'Invalid definition')
        stack.append(definition)
    while stack:
        node=_resolve(schema,stack.pop())
        if not isinstance(node,dict):fail('CONFIG_SCHEMA','node','Schema node must be an object')
        kind=node.get('type')
        if kind not in allowed:fail('CONFIG_SCHEMA','node','Unsupported type: '+str(kind))
        if kind=='object':
            props=node.get('properties',{})
            required=node.get('required',[])
            if not isinstance(props,dict) or not isinstance(required,list) or any(k not in props for k in required):
                fail('CONFIG_SCHEMA','object','Object requires properties and a valid required list')
            for key,child in props.items():
                if not isinstance(key,str) or not isinstance(child,dict):fail('CONFIG_SCHEMA','object','Invalid property entry')
                if key not in required:
                    default=child.get('default')
                    if not isinstance(default,dict) or default.get('policy') not in ('preserve','value','null'):
                        fail('CONFIG_SCHEMA','object.'+key,'Optional properties require explicit default policy preserve/value/null')
                    if default.get('policy')=='value' and 'value' not in default:
                        fail('CONFIG_SCHEMA','object.'+key,'Value default policy requires an explicit value')
                    if default.get('policy')=='null' and not child.get('nullable',False):
                        fail('CONFIG_SCHEMA','object.'+key,'Null default policy requires nullable: true')
                stack.append(child)
            if node.get('additionalProperties',False) not in (False,True) and not isinstance(node.get('additionalProperties'),dict):
                fail('CONFIG_SCHEMA','object','additionalProperties must be boolean or a value schema')
            if isinstance(node.get('additionalProperties'),dict):stack.append(node['additionalProperties'])
        elif kind=='array':
            if not isinstance(node.get('items'),dict):fail('CONFIG_SCHEMA','array','Array items schema is required')
            stack.append(node['items'])
        elif kind=='dictionary':
            if node.get('keyType','string')!='string' or not isinstance(node.get('values'),dict):
                fail('CONFIG_SCHEMA','dictionary','Only string dictionary keys with an explicit values schema are supported')
            stack.append(node['values'])
        elif kind=='enum':
            if node.get('base') not in ('string','integer') or not isinstance(node.get('values'),list) or not node['values']:
                fail('CONFIG_SCHEMA','enum','Enums require a string/integer base and non-empty values')
            expected=str if node.get('base')=='string' else int
            if any(type(value) is not expected for value in node['values']):
                fail('CONFIG_SCHEMA','enum','Enum values must match the declared base exactly')
            if expected is int and any(value<-(2**63) or value>2**63-1 for value in node['values']):
                fail('CONFIG_SCHEMA','enum','Generated C#/Go integer enums are limited to signed 64-bit values')
            if len({(type(x).__name__,str(x)) for x in node['values']})!=len(node['values']):
                fail('CONFIG_SCHEMA','enum','Enum values must be unique')
        if node.get('nullable',False) not in (True,False):fail('CONFIG_SCHEMA','nullable','Expected boolean')
        if kind in ('integer','decimal'):
            for limit in ('minimum','maximum','exclusiveMinimum','exclusiveMaximum'):
                if limit in node and (type(node[limit]) not in (int,float,Decimal) or isinstance(node[limit],bool)):
                    fail('CONFIG_SCHEMA','number.'+limit,'Numeric bound required')
            if 'minimum' in node and 'maximum' in node and node['minimum']>node['maximum']:
                fail('CONFIG_SCHEMA','number','minimum exceeds maximum')
            if kind=='integer' and node.get('bits',32) not in (32,64,128):
                fail('CONFIG_SCHEMA','integer.bits','Integer width must be 32, 64, or 128')
            if kind=='decimal' and ('maxScale' in node and (type(node['maxScale']) is not int or not 0 <= node['maxScale'] <= 28)):
                fail('CONFIG_SCHEMA','decimal.maxScale','Decimal scale must be between 0 and 28 for cross-runtime parity')
        if 'minItems' in node and (type(node['minItems']) is not int or node['minItems']<0):fail('CONFIG_SCHEMA','minItems','Non-negative integer required')
        if 'maxItems' in node and (type(node['maxItems']) is not int or node['maxItems']<0):fail('CONFIG_SCHEMA','maxItems','Non-negative integer required')
    if not isinstance(schema.get('tables',[]),list) or not isinstance(schema.get('references',[]),list) or not isinstance(schema.get('resources',[]),list):
        fail('CONFIG_SCHEMA','constraints','tables/references/resources must be arrays')
    for raw in stack_defaults(schema):
        child,path,default_value=raw
        probe=dict(schema);probe['root']=child
        invalid,_=_schema_errors(probe,default_value)
        if invalid:fail('CONFIG_SCHEMA_DEFAULT',path,'Declared default does not satisfy its property schema')


def stack_defaults(schema):
    pending=[schema['root'],*schema.get('definitions',{}).values()]
    while pending:
        node=_resolve(schema,pending.pop())
        if not isinstance(node,dict):continue
        if node.get('type')=='object':
            for key,child in node.get('properties',{}).items():
                policy=child.get('default',{}).get('policy') if isinstance(child,dict) else None
                if policy=='value':yield child,'/properties/'+key,child['default']['value']
                pending.append(child)
            additional=node.get('additionalProperties')
            if isinstance(additional,dict):pending.append(additional)
        elif node.get('type')=='array' and isinstance(node.get('items'),dict):pending.append(node['items'])
        elif node.get('type')=='dictionary' and isinstance(node.get('values'),dict):pending.append(node['values'])


def _type_name(value):
    if value is None:return 'null'
    if isinstance(value,bool):return 'boolean'
    if isinstance(value,int):return 'integer'
    if isinstance(value,(float,Decimal)):return 'decimal'
    if isinstance(value,str):return 'string'
    if isinstance(value,dict):return 'object'
    if isinstance(value,list):return 'array'
    return type(value).__name__


def _pointer(parent, segment):
    return parent+'/'+str(segment).replace('~','~0').replace('/','~1')


def _walk_path(root,path):
    """Yield (value, JSON pointer) for dotted paths with [] array expansion."""
    current=[(root,'')]
    for part in path.split('.') if path else []:
        expand=part.endswith('[]');key=part[:-2] if expand else part
        next_items=[]
        for value,pointer in current:
            if key=='*' and isinstance(value,dict):
                for k,child in value.items():
                    child_ptr=_pointer(pointer,k)
                    if expand and isinstance(child,list):next_items.extend((v,_pointer(child_ptr,i)) for i,v in enumerate(child))
                    elif not expand:next_items.append((child,child_ptr))
                continue
            if isinstance(value,dict) and key in value:
                child=value[key];child_ptr=_pointer(pointer,key)
                if expand:
                    if isinstance(child,list):next_items.extend((v,_pointer(child_ptr,i)) for i,v in enumerate(child))
                else:next_items.append((child,child_ptr))
        current=next_items
    yield from current


def _schema_errors(schema, document):
    errors=[];defaults=[]
    def add(code,path,message,expected=None,actual=None):
        if len(errors)<MAX_DIAGNOSTICS:
            row={'code':code,'path':path or '/','message':message}
            if expected is not None:row['expected']=expected
            if actual is not None:row['actualType']=actual
            errors.append(row)
    def visit(value, raw_node, path):
        node=_resolve(schema,raw_node)
        if value is None:
            if node.get('nullable',False):return
            add('NULL_NOT_ALLOWED',path,'Explicit null is not allowed',node.get('type'),'null');return
        kind=node['type']
        checks={'object':lambda x:isinstance(x,dict),'array':lambda x:isinstance(x,list),
                'dictionary':lambda x:isinstance(x,dict),'string':lambda x:isinstance(x,str),
                'integer':lambda x:type(x)is int,'decimal':lambda x:(type(x)is int or isinstance(x,Decimal)) and not isinstance(x,bool),
                'boolean':lambda x:type(x)is bool,'enum':lambda x:True}
        if not checks[kind](value):
            add('TYPE_MISMATCH',path,'Value has the wrong JSON type',kind,_type_name(value));return
        if kind=='enum':
            base=node['base'];valid=(type(value)is str if base=='string' else type(value)is int)
            if not valid or value not in node['values']:
                add('ENUM_VALUE',path,'Value is outside the declared enum',node['values'],_type_name(value))
            return
        if kind in ('integer','decimal'):
            number=Decimal(value) if type(value)is int else value
            if not number.is_finite():add('NUMBER_NOT_FINITE',path,'Non-finite number is not allowed')
            else:
                for key,op in (('minimum',lambda a,b:a<b),('maximum',lambda a,b:a>b),
                               ('exclusiveMinimum',lambda a,b:a<=b),('exclusiveMaximum',lambda a,b:a>=b)):
                    if key in node and op(number,Decimal(node[key])):add('NUMBER_RANGE',path,'Number violates '+key,node[key],kind)
                if kind=='decimal' and 'maxScale' in node and max(0,-number.as_tuple().exponent)>node['maxScale']:
                    add('NUMBER_SCALE',path,'Decimal scale exceeds maxScale',node['maxScale'],kind)
                if kind=='integer' and 'bits' in node:
                    bits=node['bits'];lo=-(2**(bits-1));hi=2**(bits-1)-1
                    if bits not in (32,64,128) or value<lo or value>hi:add('INTEGER_WIDTH',path,'Integer does not fit declared width',bits,kind)
                if kind=='decimal':
                    scale=max(0,-number.as_tuple().exponent)
                    if scale>28 or abs(number)>Decimal('79228162514264337593543950335'):
                        add('DECIMAL_PRECISION',path,'Decimal cannot be represented by the shared C#/Go contract',{'maxScale':28,'maxAbs':'79228162514264337593543950335'},kind)
            return
        if kind in ('object','dictionary'):
            props=node.get('properties',{}) if kind=='object' else value
            required=set(node.get('required',[])) if kind=='object' else set()
            if kind=='object':
                for key in required:
                    if key not in value:add('REQUIRED_MISSING',_pointer(path,key),'Required property is missing',key)
                for key,child in node.get('properties',{}).items():
                    if key in value:visit(value[key],child,_pointer(path,key))
                    elif key not in required:
                        policy=child.get('default',{}).get('policy')
                        if policy=='value':
                            defaults.append(_pointer(path,key));visit(child['default']['value'],child,_pointer(path,key))
                        elif policy=='null':
                            defaults.append(_pointer(path,key));visit(None,dict(child,nullable=True),_pointer(path,key))
                extras=set(value)-set(node.get('properties',{}))
                ap=node.get('additionalProperties',False)
                if kind=='object' and ap is False:
                    for key in sorted(extras):add('UNKNOWN_FIELD',_pointer(path,key),'Unknown property is not allowed')
                elif kind=='object' and isinstance(ap,dict):
                    for key in extras:visit(value[key],ap,_pointer(path,key))
            else:
                value_schema=node['values']
                for key,child in value.items():visit(child,value_schema,_pointer(path,key))
            return
        if kind=='array':
            if 'minItems'in node and len(value)<node['minItems']:add('ARRAY_MIN_ITEMS',path,'Array has too few items',node['minItems'],'array')
            if 'maxItems'in node and len(value)>node['maxItems']:add('ARRAY_MAX_ITEMS',path,'Array has too many items',node['maxItems'],'array')
            for index,item in enumerate(value):visit(item,node['items'],_pointer(path,index))
    visit(document,schema['root'],'')
    return errors,defaults


def _index_tables(schema, document, external, errors):
    indexes={}
    for spec in schema.get('tables',[]):
        if not isinstance(spec,dict) or not all(isinstance(spec.get(k),str) for k in ('name','path','id')):
            errors.append({'code':'SCHEMA_TABLE','path':'/','message':'Malformed table declaration'});continue
        values=[]
        for rows,pointer in _walk_path(document,spec['path']):
            if not isinstance(rows,list):continue
            seen={}
            for i,row in enumerate(rows):
                p=_pointer(pointer,i);identity=row.get(spec['id']) if isinstance(row,dict) else None
                marker=(type(identity).__name__,str(identity))
                if identity is None:continue
                if marker in seen:errors.append({'code':'DUPLICATE_ID','path':_pointer(p,spec['id']),'message':'Duplicate table identity','table':spec['name']})
                else:seen[marker]=p
                values.append(identity)
        indexes[spec['name']]=set((type(v).__name__,str(v)) for v in values)
    for name,rows in (external or {}).items():
        values=[]
        if isinstance(rows,dict):values=list(rows.keys())
        elif isinstance(rows,(list,tuple,set,frozenset)):
            for item in rows:values.append(item.get('id') if isinstance(item,dict) else item)
        else:continue
        indexes[name]=set((type(v).__name__,str(v)) for v in values)
    return indexes


def _asset_guid_index(project):
    if project is None:return None
    root=safe(project);assets=root/'Assets'
    if not assets.is_dir():return {}
    index={};count=0
    for meta in assets.rglob('*.meta'):
        meta=safe(meta);count+=1
        if count>MAX_RESOURCE_FILES:fail('CONFIG_RESOURCE_BUDGET',str(assets),'Asset metadata scan exceeds file budget')
        try:raw=read(meta,2*1024*1024).decode('utf-8-sig')
        except (UnicodeDecodeError,BeaverError):continue
        match=re.search(r'^guid:\s*([0-9a-fA-F]{32})\s*$',raw,re.M)
        if match:index.setdefault(match.group(1).lower(),[]).append(str(meta)[:-5])
    return index


def validate(schema, input, tables=None, project=None):
    schema_doc,_=_document(schema,'schema')
    _check_schema(schema_doc)
    document,input_hash=_document(input,'input')
    errors,defaults=_schema_errors(schema_doc,document)
    indexes=_index_tables(schema_doc,document,tables,errors)
    for ref in schema_doc.get('references',[]):
        if not isinstance(ref,dict) or not isinstance(ref.get('table'),str) or not isinstance(ref.get('path'),str):
            errors.append({'code':'SCHEMA_REFERENCE','path':'/','message':'Malformed foreign-key declaration'});continue
        known=indexes.get(ref['table'],set());skip=ref.get('skip',[])
        for value,pointer in _walk_path(document,ref['path']):
            if any(value==item and type(value)is type(item) for item in skip):continue
            if (type(value).__name__,str(value)) not in known:
                errors.append({'code':'FOREIGN_KEY_MISSING','path':pointer or '/','message':'Reference does not resolve','table':ref['table']})
    if schema_doc.get('resources') and project is not None:
        guid_index=None
        for ref in schema_doc['resources']:
            if not isinstance(ref,dict) or not isinstance(ref.get('path'),str):
                errors.append({'code':'SCHEMA_RESOURCE','path':'/','message':'Malformed resource declaration'});continue
            for value,pointer in _walk_path(document,ref['path']):
                if value is None and ref.get('nullable',False):continue
                if not isinstance(value,str):continue
                kind=ref.get('kind')
                if kind=='assetPath':
                    rel=Path(value)
                    if rel.is_absolute() or '..' in rel.parts or not value.startswith('Assets/'):
                        errors.append({'code':'RESOURCE_PATH_UNSAFE','path':pointer,'message':'Expected a safe project-relative Assets path'});continue
                    candidate=safe(Path(project)/rel)
                    if not candidate.exists():errors.append({'code':'RESOURCE_PATH_MISSING','path':pointer,'message':'Referenced project asset is missing'})
                elif kind=='assetGuid':
                    if guid_index is None:guid_index=_asset_guid_index(project)
                    matches=guid_index.get(value.lower(),[]) if _GUID.fullmatch(value) else []
                    if len(matches)!=1:errors.append({'code':'RESOURCE_GUID','path':pointer,'message':'GUID must resolve to exactly one project asset'})
                else:errors.append({'code':'SCHEMA_RESOURCE','path':pointer,'message':'Unsupported resource kind'})
    errors=errors[:MAX_DIAGNOSTICS]
    counts={'diagnostics':len(errors),'defaultsApplied':len(defaults),'tables':{name:len(values) for name,values in indexes.items()},
            'rootProperties':len(document) if isinstance(document,dict) else None}
    result={'schema':'beaver-config-report/2','status':'ok' if not errors else 'failed',
            'schemaId':schema_doc.get('id'),'inputSha256':input_hash,'counts':counts,
            'defaultsApplied':defaults,'diagnostics':errors,
            'limitations':['Structural and declared relational/resource rules only; gameplay economics and save migration are not inferred']}
    return result


def compare(before_schema, after_schema, before_input=None, after_input=None):
    before,_=_document(before_schema,'before_schema');after,_=_document(after_schema,'after_schema')
    _check_schema(before);_check_schema(after)
    changes=[]
    def walk(old_raw,new_raw,path):
        old=_resolve(before,old_raw);new=_resolve(after,new_raw)
        if old.get('type')!=new.get('type') or old.get('base')!=new.get('base'):
            changes.append({'path':path,'change':'type_changed','compatibility':'breaking'});return
        if old.get('nullable',False)!=new.get('nullable',False):
            changes.append({'path':path,'change':'nullability_changed','compatibility':'breaking' if old.get('nullable') and not new.get('nullable') else 'additive'})
        if old['type']=='object':
            op,np=old.get('properties',{}),new.get('properties',{})
            old_required=set(old.get('required',[]));new_required=set(new.get('required',[]))
            for key in sorted(set(op)-set(np)):changes.append({'path':path+'/'+key,'change':'property_removed','compatibility':'breaking'})
            for key in sorted(set(np)-set(op)):
                breaking=key in new_required
                changes.append({'path':path+'/'+key,'change':'required_property_added' if breaking else 'optional_property_added','compatibility':'breaking' if breaking else 'backward_compatible'})
            for key in sorted(set(op)&set(np)):
                if key not in old_required and key in new_required:changes.append({'path':path+'/'+key,'change':'property_now_required','compatibility':'breaking'})
                walk(op[key],np[key],path+'/'+key)
        elif old['type']=='array':walk(old['items'],new['items'],path+'[]')
        elif old['type']=='dictionary':walk(old['values'],new['values'],path+'{}')
        elif old['type']=='enum':
            removed=[v for v in old['values'] if v not in new['values']]
            added=[v for v in new['values'] if v not in old['values']]
            if removed:changes.append({'path':path,'change':'enum_values_removed','compatibility':'breaking','count':len(removed)})
            if added:changes.append({'path':path,'change':'enum_values_added','compatibility':'backward_compatible','count':len(added)})
        for key in ('minimum','exclusiveMinimum','maximum','exclusiveMaximum','maxScale','bits'):
            if old.get(key)!=new.get(key):changes.append({'path':path,'change':'constraint_'+key+'_changed','compatibility':'review_required'})
    walk(before['root'],after['root'],'')
    for field in ('tables','references','resources'):
        if before.get(field,[])!=after.get(field,[]):changes.append({'path':'/'+field,'change':'relational_contract_changed','compatibility':'review_required'})
    outcomes={}
    if before_input is not None and after_input is not None:
        old_under_new=validate(after,before_input)
        new_under_old=validate(before,after_input)
        outcomes={'beforeInputAgainstAfterSchema':old_under_new['status'],
                  'afterInputAgainstBeforeSchema':new_under_old['status'],
                  'beforeInputDiagnostics':old_under_new['counts']['diagnostics'],
                  'afterInputDiagnostics':new_under_old['counts']['diagnostics']}
    compatibility='breaking' if any(c['compatibility']=='breaking' for c in changes) else ('review_required' if any(c['compatibility']=='review_required' for c in changes) else 'backward_compatible')
    return {'schema':'beaver-config-compatibility/2','status':'ok' if compatibility=='backward_compatible' else 'partial',
            'compatibility':compatibility,'changes':changes,'inputCompatibility':outcomes,
            'limitations':['Player saves and persisted runtime state are outside configuration-schema compatibility']}


def _safe_name(value):
    out=re.sub(r'[^A-Za-z0-9_]', '_', value)
    if not out or out[0].isdigit():out='T_'+out
    return out


def _namespace(value):
    return '.'.join(_safe_name(part) for part in str(value).split('.'))


def _resolve_defs(schema,node):
    while '$ref' in node:
        name=node['$ref'].split('/',2)[-1].replace('~1','/').replace('~0','~')
        node=schema.get('definitions',{}).get(name,{})
    return node


def _generate_sources(schema):
    classes={};enums={}
    def visit(raw,path,name_hint):
        node=_resolve_defs(schema,raw)
        kind=node['type']
        if kind=='enum':
            name=_safe_name(name_hint)
            if name not in enums:
                values=node['values'];members=[]
                for i,value in enumerate(values):
                    member=_safe_name(str(value)) if node['base']=='string' else 'Value_'+str(value).replace('-','Neg').replace('.','_')
                    base=member;n=2
                    while member in {m[0] for m in members}:member=base+'_'+str(n);n+=1
                    members.append((member,value))
                enums[name]=(node,members)
            return name
        if kind in ('object',):
            name=_safe_name(name_hint)
            if name in classes:return name
            classes[name]=None
            fields=[]
            required=set(node.get('required',[]))
            property_names=set()
            for key,child_raw in node.get('properties',{}).items():
                child=_resolve_defs(schema,child_raw)
                child_name=name+'_'+_safe_name(key)
                ctype=visit(child,path+'/'+key,child_name)
                optional=key not in required and child.get('default',{}).get('policy')=='preserve'
                if optional:ctype='OptionalValue<'+ctype+'>'
                prop=_safe_name(key);prop=prop[:1].upper()+prop[1:]
                base=prop;suffix=2
                while prop in property_names:prop=base+'_'+str(suffix);suffix+=1
                property_names.add(prop)
                fields.append((key,prop,ctype,optional,child, key in required))
            classes[name]=(node,fields)
            return name
        if kind=='array':return 'List<'+visit(node['items'],path+'[]',name_hint+'Item')+'>'
        if kind=='dictionary':return 'Dictionary<string,'+visit(node['values'],path+'{}',name_hint+'Value')+'>'
        if kind=='string':return 'string'
        if kind=='boolean':return 'bool'
        if kind=='integer':
            bits=node.get('bits',32)
            return 'int' if bits==32 else 'long' if bits==64 else 'System.Numerics.BigInteger'
        if kind=='decimal':return 'decimal'
        return 'object'
    root_name=visit(schema['root'],'$',schema.get('rootName','ConfigurationRoot'))
    def cstype(node):
        n=_resolve_defs(schema,node);kind=n['type']
        if kind=='object':return _safe_name(n.get('name','Object'))
        return None
    lines=['// <auto-generated by Beaver configuration v3. Do not edit.>','using System;','using System.Collections.Generic;','using System.Numerics;','using System.Runtime.Serialization;','using Newtonsoft.Json;','using Newtonsoft.Json.Converters;','namespace '+_namespace(schema.get('namespace','Beaver.GeneratedConfig')),'{']
    for name,(node,members) in enums.items():
        if node['base']=='string':lines.append('    [JsonConverter(typeof(StringEnumConverter))]')
        else:lines.append('    [JsonConverter(typeof(NumericEnumConverter<'+name+'>))]')
        bits=max((abs(int(v)).bit_length()+1 for _,v in members),default=32) if node['base']=='integer' else 32
        underlying=' : long' if node['base']=='integer' and bits>31 else ''
        lines.append('    public enum '+name+underlying+' {')
        for member,value in members:
            if node['base']=='string':lines.append('        [EnumMember(Value = '+json.dumps(value,ensure_ascii=True)+')]')
            lines.append('        '+member+(' = '+str(value) if node['base']=='integer' else '')+',')
        lines.append('    }')
    for name,entry in classes.items():
        if entry is None:continue
        node,fields=entry;lines.append('    [JsonObject(MemberSerialization.OptIn)]');lines.append('    public sealed class '+name+' {')
        for key,prop,ctype,optional,child,required in fields:
            req='Always' if required and not child.get('nullable',False) else 'AllowNull' if required else 'Default'
            lines.append('        [JsonProperty('+json.dumps(key,ensure_ascii=True)+', Required = Required.'+req+')]')
            if child.get('nullable',False) and child['type'] in ('integer','decimal','boolean','enum'):
                base=ctype[len('OptionalValue<'):-1] if optional else ctype
                if not base.endswith('?'):base += '?'
                ctype='OptionalValue<'+base+'>' if optional else base
            if optional:
                base=ctype[len('OptionalValue<'):-1]
                lines.append('        [JsonConverter(typeof(OptionalValueConverter<'+base+'>))]')
            lines.append('        public '+ctype+' '+prop+' { get; set; }')
        lines.append('    }')
    lines.append('}')
    dto='\n'.join(lines)+'\n'
    schema_json=json.dumps(schema,ensure_ascii=False,separators=(',',':'))
    runtime=Path(__file__).parent/'targets'/'BeaverJsonRuntime.cs'
    runtime=runtime.read_text(encoding='utf-8').replace('namespace Beaver.GeneratedConfig {','namespace '+_namespace(schema.get('namespace','Beaver.GeneratedConfig'))+' {')
    go_dto=_generate_go_types(schema)
    template=Path(__file__).parent/'targets'/'BeaverJsonRuntime.go.template'
    go_runtime=template.read_text(encoding='utf-8').replace('package configv3','package '+_safe_name(schema.get('goPackage','configv3')).lower())
    return {'ConfigurationDtos.g.cs':dto,'ConfigurationJsonRuntime.g.cs':runtime,
            'ConfigurationTypes.go':go_dto,'BeaverJsonRuntime.go':go_runtime,
            'configuration.schema.json':json.dumps(schema,ensure_ascii=False,indent=2)+'\n'}


def _generate_go_types(schema):
    classes={};enums={}
    def visit(raw,path,name_hint):
        node=_resolve_defs(schema,raw);kind=node['type']
        if kind=='object':
            name=_safe_name(name_hint)
            if name in classes:return name
            classes[name]=None;fields=[];required=set(node.get('required',[]))
            for key,child_raw in node.get('properties',{}).items():
                child=_resolve_defs(schema,child_raw);ctype=visit(child,path+'/'+key,name+'_'+_safe_name(key))
                optional=key not in required and child.get('default',{}).get('policy')=='preserve'
                if child.get('nullable',False):ctype='Nullable['+ctype+']'
                if optional:ctype='Optional['+ctype+']'
                prop=_safe_name(key);prop=prop[:1].upper()+prop[1:]
                fields.append((key,prop,ctype,child))
            classes[name]=fields;return name
        if kind=='enum':
            name=_safe_name(name_hint);enums.setdefault(name,(node,node['values']));return name
        if kind=='array':return '[]'+visit(node['items'],path+'[]',name_hint+'Item')
        if kind=='dictionary':return 'map[string]'+visit(node['values'],path+'{}',name_hint+'Value')
        if kind=='string':return 'string'
        if kind=='boolean':return 'bool'
        if kind=='integer':return 'int32' if node.get('bits',32)==32 else 'int64' if node.get('bits',32)==64 else 'BigInteger'
        if kind=='decimal':return 'Decimal'
        return 'any'
    root=visit(schema['root'],'$',schema.get('rootName','ConfigurationRoot'))
    lines=['// Code generated by Beaver configuration v3. DO NOT EDIT.','package '+_safe_name(schema.get('goPackage','configv3')).lower(),'']
    for name,(node,values) in enums.items():
        base='string' if node['base']=='string' else 'int32' if max((abs(int(x)).bit_length()+1 for x in values),default=32)<=31 else 'int64'
        lines.append('type '+name+' '+base);lines.append('const (')
        for value in values:
            member=_safe_name(str(value));member=member[:1].upper()+member[1:]
            rendered=json.dumps(value,ensure_ascii=True) if node['base']=='string' else str(value)
            lines.append('\t'+name+'_'+member+' '+name+' = '+rendered)
        lines.append(')')
    for name,fields in classes.items():
        lines.append('type '+name+' struct {')
        for key,prop,ctype,child in fields:lines.append('\t'+prop+' '+ctype+' `json:'+json.dumps(key)+'`')
        lines.append('}')
    lines.append('type RootType = '+root)
    return '\n'.join(lines)+'\n'


def generate(schema, output):
    schema_doc,_=_document(schema,'schema');_check_schema(schema_doc)
    target=Path(output).absolute()
    if any(p.is_symlink() for p in (target,*target.parents)):fail('CONFIG_OUTPUT_SYMLINK',str(target),'Output path cannot contain symlinks')
    target=target.resolve()
    generated=_generate_sources(schema_doc)
    content={name:text.encode('utf-8') for name,text in generated.items()}
    files={name:{'sha256':sha(raw),'bytes':len(raw)} for name,raw in sorted(content.items())}
    schema_raw=content['configuration.schema.json'];schema_hash=sha(schema_raw)
    manifest={'generator':GENERATOR,'schemaVersion':SCHEMA_VERSION,'schemaId':schema_doc.get('id'),
              'schemaSha256':schema_hash,'ownedFiles':files}
    manifest_raw=encoded(manifest)
    if target.exists():
        if not target.is_dir():fail('CONFIG_OUTPUT','output','Existing output must be a directory')
        old_path=target/'generation-manifest.json'
        if not old_path.is_file():return {'status':'blocked','output':str(target),'reason':'existing output has no Beaver ownership manifest'}
        try:old=load(read(old_path,2*1024*1024))
        except BeaverError:return {'status':'blocked','output':str(target),'reason':'existing ownership manifest is unreadable'}
        if old.get('generator')!=GENERATOR or not isinstance(old.get('ownedFiles'),dict):
            return {'status':'blocked','output':str(target),'reason':'ownership manifest belongs to another generator'}
        drift=[]
        for name,entry in old['ownedFiles'].items():
            p=target/name
            if not p.is_file() or digest_file(p)!=entry.get('sha256'):drift.append(name)
        if drift:return {'status':'blocked','output':str(target),'reason':'generated owned files were edited; no overwrite performed','modifiedOwnedFiles':drift}
        collisions=[name for name in content if (target/name).exists() and name not in old['ownedFiles']]
        if collisions:return {'status':'blocked','output':str(target),'reason':'generated path collides with unowned file','collisions':collisions}
        previous=old.get('ownedFiles',{})
        diff={'added':[x for x in content if x not in previous],
              'updated':[x for x in content if x in previous and files[x]['sha256']!=previous[x].get('sha256')],
              'removed':[x for x in previous if x not in content],
              'unownedFiles':sorted(str(p.relative_to(target)) for p in target.rglob('*') if p.is_file() and p.name!='generation-manifest.json' and str(p.relative_to(target)) not in previous)}
        # Never delete obsolete generated files automatically; retain as unowned until an owning transaction reviews them.
        for name,raw in content.items():_atomic_write(target/name,raw)
        _atomic_write(old_path,manifest_raw)
        return {'status':'ok','output':str(target),'manifest':str(old_path),'schemaSha256':schema_hash,'diff':diff}
    target.mkdir(parents=True,exist_ok=False)
    for name,raw in content.items():_atomic_write(target/name,raw)
    _atomic_write(target/'generation-manifest.json',manifest_raw)
    return {'status':'ok','output':str(target),'manifest':str(target/'generation-manifest.json'),
            'schemaSha256':schema_hash,'diff':{'added':sorted(content),'updated':[],'removed':[],'unownedFiles':[]}}


def _atomic_write(path, raw):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    try:
        fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'wb') as stream:stream.write(raw);stream.flush();os.fsync(stream.fileno())
        os.replace(temp,path)
    finally:
        try:temp.unlink()
        except FileNotFoundError:pass

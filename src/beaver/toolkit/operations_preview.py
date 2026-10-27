"""Bounded local event analysis and configuration previews; no network or player writes."""
import argparse,json,hashlib
from datetime import datetime,timedelta
from zoneinfo import ZoneInfo
from collections import defaultdict,Counter
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError,SchemaError
from beaver.toolkit.support.host.paths import Path
from beaver.toolkit.support.common import hash_file,write_new,GreaError,canonical
from beaver.toolkit.support.safe_paths import canonical_path,disjoint
from .compatibility_plan import read
from beaver.toolkit.support.lifecycle_data_contracts import documents
_EVENT=Draft202012Validator(documents()["operations-event.schema.json"])

def instant(value):
    if not isinstance(value,str):raise ValueError('Timestamp must be ISO8601 with offset')
    result=datetime.fromisoformat(value.replace('Z','+00:00'))
    if result.tzinfo is None:raise ValueError('Timestamp requires timezone offset')
    return result

def events(path,as_of):
    path=canonical_path(path);before=hash_file(path,16*1024**2);rows=[];seen={};duplicates=0
    with path.open(encoding='utf-8') as f:
        for line in f:
            if len(line.encode('utf-8'))>65536:raise ValueError('Event line exceeds 64 KiB')
            if not line.strip():continue
            row=json.loads(line,parse_constant=lambda _:(_ for _ in ()).throw(ValueError('Nonfinite JSON')))
            if not _EVENT.is_valid(row):raise ValueError('Event does not match operations-event-v1 schema')
            required=('event_id','event_type','event_time','player_id','session_id','game_version','config_version','environment')
            if not isinstance(row,dict) or (type(row.get('schema_version')) is not int or row.get('schema_version')!=1) or any(not isinstance(row.get(k),str) or not 1<=len(row[k])<=256 for k in required):raise ValueError('Event v1 requires bounded identity, versions and explicit pseudonymous player/session')
            time=instant(row['event_time'])
            if time>as_of:raise ValueError('Event later than declared data watermark')
            payload=hashlib.sha256(canonical(row)).hexdigest();key=row['event_id']
            if key in seen:
                if seen[key]!=payload:raise ValueError('Conflicting payload for duplicate event ID')
                duplicates+=1;continue
            seen[key]=payload;rows.append((time,row))
            if len(rows)>100000:raise ValueError('Event count exceeds 100000')
    if hash_file(path)!=before:raise ValueError('Events changed while reading')
    return rows,before,duplicates

def economy_identity(row):
    item=row['economy']
    if not isinstance(item,dict) or set(item)!={'kind','asset_id','quantity','receipt_id'} or item['kind'] not in ('grant','spend','snapshot') or not isinstance(item['asset_id'],str) or not item['asset_id'] or not isinstance(item['receipt_id'],str) or not item['receipt_id'] or not isinstance(item['quantity'],str) or not item['quantity'].isascii() or not item['quantity'].isdigit() or len(item['quantity'])>19 or int(item['quantity'])>9223372036854775807:raise ValueError('Economy requires bounded integer string and stable receipt ID')
    return (row['environment'],row['player_id'],item['receipt_id'],item['asset_id'],item['kind'])

def analyze_events(source,output,start,end,as_of,timezone,steps,active_event='session_start',window_seconds=3600,data_kind='observed_events'):
    start,end,watermark=map(instant,(start,end,as_of));tz=ZoneInfo(timezone)
    if not start<end<=watermark or not 1<=window_seconds<=604800 or not 1<=len(steps)<=12 or len(set(steps))!=len(steps):raise ValueError('Invalid window or ordered unique funnel steps')
    if data_kind not in ('observed_events','ledger','simulation'):raise ValueError('Explicit supported data kind required')
    rows,identity,dupes=events(source,watermark);groups=defaultdict(list);receipt_owners={}
    for time,row in sorted(rows,key=lambda x:(x[0],x[1]['event_id'])):
        if 'economy' not in row:continue
        key=economy_identity(row);quantity=row['economy']['quantity']
        if key in receipt_owners and receipt_owners[key][1]!=quantity:raise ValueError('Conflicting economy receipt')
        receipt_owners.setdefault(key,(row['event_id'],quantity))
    for time,row in rows:
        if start<=time<end:groups[(row['environment'],row['game_version'],row['config_version'])].append((time,row))
    reports=[]
    for (env,version,config),group in sorted(groups.items()):
        sessions=defaultdict(list);active=defaultdict(set);economy=defaultdict(Counter);ties=0
        for time,row in group:
            sessions[(row['player_id'],row['session_id'])].append((time,row))
            if row['event_type']==active_event:active[row['player_id']].add(time.astimezone(tz).date())
            if 'economy' in row:
                item=row['economy']
                key=economy_identity(row)
                if receipt_owners[key][0]!=row['event_id']:
                    economy[item['asset_id']]['duplicate_receipts']+=1;continue
                counts=economy[item['asset_id']];counts[item['kind']+'_events']+=1
                if item['kind']!='snapshot':counts[item['kind']]+=int(item['quantity'])
        funnel=[0]*len(steps)
        for sequence in sessions.values():
            sequence.sort(key=lambda x:x[0]);index=0;first=None;last=None
            for time,row in sequence:
                if index==len(steps):break
                if row['event_type']!=steps[index]:continue
                if last is not None and time<=last:ties+=1;continue
                if first is not None and (time-first).total_seconds()>window_seconds:break
                first=time if first is None else first;last=time;funnel[index]+=1;index+=1
        cohorts=defaultdict(lambda:[0,0]);open_cohorts=0
        for dates in active.values():
            day=min(dates);closed=datetime.combine(day+timedelta(days=2),datetime.min.time(),tzinfo=tz)
            if closed>min(end,watermark):open_cohorts+=1;continue
            counts=cohorts[day.isoformat()];counts[0]+=1;counts[1]+=int(day+timedelta(days=1) in dates)
        reports.append(dict(environment=env,game_version=version,config_version=config,sample_events=len(group),sample_sessions=len(sessions),sample_players=len({r['player_id'] for _,r in group}),funnel=dict(steps=steps,counts=funnel,denominator='sessions reaching first step',conversion=[n/funnel[0] if funnel[0] else None for n in funnel],window_seconds=window_seconds,ambiguous_equal_times_not_advanced=ties),retention=dict(active_event=active_event,cohort_basis='first observed active date within this input group, not acquisition',d1=[dict(day=d,denominator=c[0],returned=c[1],rate=c[1]/c[0]) for d,c in sorted(cohorts.items())],open_cohorts_excluded=open_cohorts),economy=[dict(asset_id=k,grants=v['grant'],spends=v['spend'],net_flow=v['grant']-v['spend'],snapshots=v['snapshot_events'],duplicate_receipts=v['duplicate_receipts'],stock_reconciled=False) for k,v in sorted(economy.items())]))
    result=dict(schema_version=1,status='readonly_analysis',data_kind=data_kind,source=identity,window=dict(start=start.isoformat(),end=end.isoformat(),as_of=watermark.isoformat(),timezone=timezone),duplicate_events_ignored=dupes,events_outside_window=sum(not start<=time<end for time,_ in rows),groups=reports,limitations=['Pseudonymity, completeness and ledger authenticity are caller obligations; source IDs are not printed in aggregate reports.','Missing or delayed events are unknown; null denominators are not zero performance. No causal revenue or product claims.','Strictly increasing timestamps within a session; equal-time order is unresolved. One funnel attempt per session.','Retention is first-observed within each version/config group, left-censored and not acquisition retention. Open days excluded.','Economy is flow only, snapshots do not establish opening stock or conservation; simulation is not live facts.'])
    out=disjoint(output,source)
    if out.exists() or not out.parent.is_dir():raise ValueError('Use a new output directory')
    out.mkdir();write_new(out/'analysis.json',result);return result

def config_preview(before,after,schema,target,output):
    paths=[before,after,schema,target];identities=[hash_file(p,4*1024**2) for p in paths];a,b,spec,scope=[read(p) for p in paths]
    Draft202012Validator.check_schema(spec)
    # No remote schema resolution: previews must be self-contained.
    def refs(node):
        if isinstance(node,dict):
            if '$id' in node:raise ValueError('Schema resource IDs are unsupported; use local fragment refs')
            for key in ('$ref','$dynamicRef','$recursiveRef'):
                if key in node and not str(node[key]).startswith('#'):raise ValueError('External schema references are unsupported')
            for value in node.values():refs(value)
        elif isinstance(node,list):
            for value in node:refs(value)
    refs(spec)
    for value in (a,b):Draft202012Validator(spec).validate(value)
    fields={'schema_version','environment','audience','effective_at','client_versions','approval_reference','rollback_limits','platform'}
    if not isinstance(scope,dict) or set(scope)!=fields or (type(scope['schema_version']) is not int or scope['schema_version']!=1) or any(not isinstance(scope.get(k),str) or not scope[k] for k in fields-{'schema_version','client_versions'}) or not isinstance(scope['client_versions'],list) or not scope['client_versions'] or not all(isinstance(v,str) and v for v in scope['client_versions']):raise ValueError('Explicit target and rollback preview contract required')
    instant(scope['effective_at']);changes=[]
    def diff(old,new,pointer=''):
        if type(old) is dict and type(new) is dict:
            for key in sorted(old.keys()|new.keys()):
                path=pointer+'/'+key.replace('~','~0').replace('/','~1')
                if key not in old:changes.append(dict(path=path,operation='add',after=new[key]))
                elif key not in new:changes.append(dict(path=path,operation='remove',before=old[key]))
                else:diff(old[key],new[key],path)
        elif old!=new or type(old)!=type(new):changes.append(dict(path=pointer,operation='replace',before=old,after=new))
        if len(changes)>256:raise ValueError('Configuration diff exceeds 256 changes; narrow input')
    diff(a,b)
    if [hash_file(p) for p in paths]!=identities:raise ValueError('Preview input changed')
    result=dict(schema_version=1,status='candidate_not_applied',inputs=identities,target=scope,changes=changes,schema_validation='both_documents_pass',client_save_protocol_compatibility='not_established_by_json_schema',platform_preflight='not_selected' if scope['platform']=='none' else 'adapter_not_implemented',rollback=dict(config_file_only='baseline candidate retained by SHA; no action executed',business_side_effects='Grants, debits and migrations are not reversed by replacing a configuration file'),production_write='Unavailable',limitations=['Approval reference is a declaration, not permission. No upload, notification, experiment allocation, reward or network calls.','A schema-compatible config can still change business behavior or break old clients; separate compatibility evidence required.'])
    out=disjoint(output,*paths)
    if out.exists() or not out.parent.is_dir():raise ValueError('Use a new output directory')
    out.mkdir();write_new(out/'preview.json',result);return result

def main():
    p=argparse.ArgumentParser(description=__doc__);sub=p.add_subparsers(dest='command',required=True)
    e=sub.add_parser('events');e.add_argument('--input',required=True);e.add_argument('--output',required=True)
    for name in ('start','end','as-of','timezone','steps'):e.add_argument('--'+name,required=True)
    e.add_argument('--active-event',default='session_start');e.add_argument('--window-seconds',type=int,default=3600);e.add_argument('--data-kind',choices=['observed_events','ledger','simulation'],required=True)
    c=sub.add_parser('config');
    for name in ('before','after','schema','target','output'):c.add_argument('--'+name,required=True)
    a=p.parse_args()
    try:
        r=analyze_events(a.input,a.output,a.start,a.end,a.as_of,a.timezone,a.steps.split(','),a.active_event,a.window_seconds,a.data_kind) if a.command=='events' else config_preview(a.before,a.after,a.schema,a.target,a.output)
        print(json.dumps(dict(status=r['status'])));return 0
    except (OSError,ValueError,TypeError,KeyError,GreaError,RecursionError,ValidationError,SchemaError) as e:print(str(e));return 2
if __name__=='__main__':raise SystemExit(main())

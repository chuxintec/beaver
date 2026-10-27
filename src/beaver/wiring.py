"""Plans typed native bindings; execution is the existing batch transaction."""
import re
from pathlib import Path
from .errors import fail
from .io import safe, inside, read, load, new_output, write_json, sha
from .output_limits import digest_file


def _one(rows, location):
    if len(rows) != 1:
        raise ValueError(location + ': expected exactly one match, observed ' + str(len(rows)))
    return rows[0]


def _ref_key(value):
    if not value:
        return 'null'
    if value.get('scope') == 'node' or (not value.get('scope') and value.get('nodePath') is not None and not value.get('guid')):
        return 'node:' + value['nodePath'] + ':' + (value.get('componentType') or 'GameObject')
    return 'asset:' + value['guid'] + ':' + str(value['localId'])


def _target(asset, row, expected, roles):
    target = row.get('target')
    if row.get('targetRole'):
        target = _one([r['target'] for r in roles if r.get('role') == row['targetRole']], 'role')
    components = asset.get('components', [])
    if target and target.get('guid'):
        if not re.fullmatch('[a-f0-9]{32}', target['guid']) or type(target.get('localId')) not in (str, int):
            raise ValueError('explicit GUID and integer localId required')
        if not re.fullmatch('-?[0-9]+', str(target['localId'])) or int(target['localId']) == 0:
            raise ValueError('nonzero exact localId required')
        # Actual subasset type is checked natively, never inferred from a guessed fileID.
        return dict(assetGuid=target['guid'], localId=int(target['localId'])), target
    if target:
        path = target.get('nodePath')
        if not isinstance(path, str):
            raise ValueError('target nodePath required')
        if target.get('componentType') == 'GameObject':
            _one([n for n in asset['nodes'] if n['path'] == path], 'target node')
            if expected not in ('UnityEngine.GameObject', 'UnityEngine.Object'):
                raise ValueError('GameObject not assignable to ' + expected)
            return dict(targetNode=path), target
        candidates = [c for c in components if c['nodePath'] == path and c['type'] == target.get('componentType')]
    else:
        candidates = [c for c in components if expected in c.get('assignableTypes', [c['type']])]
    candidate = _one(candidates, 'typed target')
    if expected not in candidate.get('assignableTypes', [candidate['type']]):
        raise ValueError('target type is not assignable to ' + expected)
    value = dict(nodePath=candidate['nodePath'], componentType=candidate['type'])
    return dict(targetNode=value['nodePath'], targetComponent=value['componentType']), value


def plan(project, snapshot_path, spec_path, output):
    root = safe(project);snapshot_path = safe(snapshot_path);spec_path = safe(spec_path)
    snapshot = load(read(snapshot_path));spec = load(read(spec_path))
    if snapshot.get('schemaVersion') != 1 or not isinstance(snapshot.get('assets'), list):
        fail('WIRING_SNAPSHOT', str(snapshot_path), 'Expected native UI snapshot v1')
    if snapshot.get('mode') not in ('static-prefab', 'static-scene'):
        fail('WIRING_MODE', str(snapshot_path), 'Runtime observations may diagnose; persistent Prefab binding needs static snapshot')
    if spec.get('schemaVersion') != 1 or set(spec) - {'schemaVersion', 'bindings', 'roles', 'repairs'}:
        fail('WIRING_SPEC', str(spec_path), 'Expected schemaVersion, bindings and optional explicit roles')
    bindings = spec.get('bindings')
    if not isinstance(bindings, list) or not 1 <= len(bindings) <= 1000:
        fail('WIRING_BINDINGS', str(spec_path), 'Expected 1..1000 bindings')
    roles = spec.get('roles', [])
    if not isinstance(roles, list):fail('WIRING_ROLES', str(spec_path), 'Expected explicit role list')
    stage = new_output(output, [root, snapshot_path, spec_path]);steps = {};results = [];seen = set()
    for row in bindings:
        result = dict(status='blocked', request=row)
        try:
            if not isinstance(row, dict) or set(row) - {'asset','nodePath','componentType','field','kind','target','targetRole','method','source','replace'}:
                raise ValueError('unsupported binding property')
            if not isinstance(row.get('source'), str) or not row['source'].strip():raise ValueError('explicit expectation source required')
            if 'replace' in row and type(row['replace']) is not bool:raise ValueError('replace must be boolean')
            asset = _one([a for a in snapshot['assets'] if a['path'] == row['asset']], 'selected asset')
            if not row['asset'].endswith('.prefab'):
                raise ValueError('Scene diagnosis supported; persistent wiring currently accepts Prefab only')
            for key, suffix in (('sourceSha256', ''), ('metaSha256', '.meta')):
                if not asset.get(key) or digest_file(inside(root, row['asset'] + suffix)) != asset[key]:
                    raise ValueError('snapshot source/meta drift: ' + row['asset'])
            component = _one([c for c in asset['components'] if c['nodePath'] == row['nodePath'] and c['type'] == row['componentType']], 'owner component')
            key = (row['asset'], row['nodePath'], row['componentType'], row['field'])
            if key in seen:raise ValueError('duplicate destination')
            seen.add(key)
            action = dict(path=row['nodePath'], component=row['componentType'], property=row['field'])
            if row.get('kind', 'reference') == 'event':
                if row['componentType'] != 'UnityEngine.UI.Button' or row['field'] != 'onClick':raise ValueError('only Button.onClick event supported')
                event = _one([e for e in asset.get('events', []) if e['nodePath'] == row['nodePath'] and e['field'] == 'onClick'], 'Button event')
                if event.get('persistentCalls'):raise ValueError('nonempty event protected; replacement not supported')
                target_args, target = _target(asset, row, 'UnityEngine.MonoBehaviour', roles)
                candidate = _one([c for c in asset['components'] if c['nodePath'] == target.get('nodePath') and c['type'] == target.get('componentType')], 'event target')
                _one([m for m in candidate.get('methods', []) if m['name'] == row.get('method') and m.get('public') is True and not m.get('static') and m.get('returnType') == 'System.Void' and m.get('parameterTypes') == []], 'public void() event method')
                action.update(kind='binding.event', value='empty', method=row['method'], **target_args)
            elif row.get('kind', 'reference') == 'reference':
                field = _one([f for f in component.get('fields', []) if f.get('propertyPath', f.get('name')) == row['field']], 'serialized field')
                if field.get('referenceState') == 'missing':raise ValueError('broken identity requires explicit repair, not automatic replacement')
                if field.get('referenceState') not in ('null', 'bound') or not field.get('acceptedType') or '.' in row['field'] or row['field'] == 'm_Script':raise ValueError('only direct compiled serialized ObjectReference fields supported')
                target_args, target = _target(asset, row, field['acceptedType'], roles)
                before = 'null' if field.get('referenceState') == 'null' else _ref_key(field.get('objectReference'))
                desired = ('asset:' + target['guid'] + ':' + str(target['localId'])) if target.get('guid') else _ref_key(target)
                if before == desired:
                    result.update(status='already_bound', expectedSource=row['source']);results.append(result);continue
                if before != 'null' and not row.get('replace', False):raise ValueError('nonempty manual binding protected')
                action.update(kind='binding.reference', value=before, flag=row.get('replace', False), **target_args)
            else:raise ValueError('unsupported binding kind')
            steps.setdefault(row['asset'], []).append(action)
            result.update(status='planned', expectedSource=row['source'], action=action, nativeTypeCheck='required_before_write')
        except (KeyError, ValueError, TypeError) as error:
            result['reason'] = str(error)
        results.append(result)
    report = dict(schemaVersion=1, contract='beaver-wiring/1', status='blocked' if any(r['status']=='blocked' for r in results) else 'planned',
                  snapshotSha256=digest_file(snapshot_path), specSha256=digest_file(spec_path), projectRoot=str(root), bindings=results,
                  limitations=['Unique compiled type or explicit role/identity only; names never select the first match', 'Prefab direct ObjectReference and empty Button.onClick public void() subset', 'Native batch preconditions, type checks and saved/reopened readback remain mandatory'])
    if report['status'] != 'blocked' and spec.get('repairs'):
        from .ui_repairs import actions as repair_actions
        for asset_path, action in repair_actions(root, snapshot, spec['repairs']):
            key=(asset_path,action['path'],action['component'],action['property'])
            if key in seen:fail('WIRING_REPAIR_COLLISION',action['property'],'Binding and repair destinations must be distinct')
            seen.add(key);steps.setdefault(asset_path,[]).append(action)
    if report['status'] != 'blocked' and steps:
        from .batch import plan as batch_plan
        batch_spec = dict(schemaVersion=2, operation='batch', steps=[dict(kind='prefab.patch', assetPath=p, actions=a) for p,a in steps.items()])
        write_json(stage/'batch-spec.json', batch_spec)
        report['batch'] = batch_plan(root, stage/'batch-spec.json', stage/'batch-plan')
    elif report['status'] != 'blocked':report['status'] = 'already_bound'
    write_json(stage/'wiring-plan.json', report)
    return dict(report, report=str(stage/'wiring-plan.json'))

"""Explicit project-local bindings. Presence is never provider readiness."""
from pathlib import Path, PurePosixPath
import re
import uuid
from . import __version__
from .io import safe, inside, read, load, encoded, sha, write_json, new_output
from .errors import fail
from .output_limits import digest_file
from .transaction_fs import lock
from .unity import identity, VERSION

PROFILE = '.beaver/project.json'
CONTRACT = 'beaver-project/1'


def relative(value):
    if not isinstance(value, str) or not value or '\\' in value or value.startswith('/') or any(p in ('', '.', '..') for p in value.split('/')):
        fail('PROJECT_PATH', str(value), 'Canonical project-relative path required')
    if any(p in ('.git', '.beaver', 'Library', 'Temp', 'obj', 'Logs', 'UserSettings') for p in PurePosixPath(value).parts):
        fail('PROJECT_RESERVED', value, 'Project state, engine cache and repository metadata are never writable product paths')
    return value


def under(path, roots):
    relative(path)
    target=path[:-5] if path.endswith('.meta') else path
    return any(target == root or target.startswith(root + '/') for root in roots)


def observed(root, engine):
    root = safe(root)
    if engine == 'unity':
        from .context import inspect
        context = inspect(root)
        if context['engine'] != 'unity' or context['editorVersion'] != VERSION:
            fail('PROJECT_UNITY_VERSION', str(root), 'Existing Unity ' + VERSION + ' project required')
        return dict(engine='unity', version=context['editorVersion'], authorityFiles=['ProjectSettings/ProjectVersion.txt', 'Packages/manifest.json']+(['Packages/packages-lock.json'] if (root/'Packages/packages-lock.json').is_file() else []))
    if engine == 'go':
        raw = read(root/'go.mod').decode('utf-8')
        module = re.search(r'^module\s+(\S+)', raw, re.M)
        version = re.search(r'^go\s+(\d+\.\d+(?:\.\d+)?)\s*$', raw, re.M)
        if not module or not version:
            fail('PROJECT_GO_MODULE', str(root), 'Explicit go.mod module and go version required')
        return dict(engine='go', version=version.group(1), module=module.group(1), authorityFiles=['go.mod'] + (['go.sum'] if (root/'go.sum').is_file() else []))
    fail('PROJECT_ENGINE', str(engine), 'Supported production bindings: unity or go')


def validate_spec(spec):
    if not isinstance(spec, dict) or set(spec) - {'schemaVersion', 'engine', 'name', 'writableRoots', 'generatedRoots', 'namespace', 'assemblyPrefixes'} or spec.get('schemaVersion') != 1:
        fail('PROJECT_SPEC', 'spec', 'Closed project setup schemaVersion 1 required')
    if not isinstance(spec.get('name'), str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_.-]{0,79}', spec['name']):
        fail('PROJECT_NAME', 'name', 'Portable explicit project name required')
    roots = spec.get('writableRoots')
    if not isinstance(roots, list) or not 1 <= len(roots) <= 64 or any(not isinstance(p, str) for p in roots) or len(set(roots)) != len(roots):
        fail('PROJECT_WRITE_ROOTS', 'writableRoots', 'Unique bounded explicit writable roots required')
    for root in roots:
        relative(root)
        if spec.get('engine') == 'unity' and root.split('/')[0] not in ('Assets', 'Packages', 'ProjectSettings'):
            fail('PROJECT_UNITY_ROOT', root, 'Unity source roots only; use a separate Go binding for Server')
    generated = spec.get('generatedRoots', [])
    if not isinstance(generated, list) or any(not under(p, roots) for p in generated):
        fail('PROJECT_GENERATED_ROOT', 'generatedRoots', 'Generated roots must be explicitly within writable roots')
    for key in ('namespace',):
        if key in spec and (not isinstance(spec[key], str) or not re.fullmatch(r'[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*', spec[key])):
            fail('PROJECT_NAMESPACE', key, 'Explicit C#/Go-safe namespace required')
    if 'assemblyPrefixes' in spec and (not isinstance(spec['assemblyPrefixes'], list) or any(not isinstance(x, str) or not x for x in spec['assemblyPrefixes'])):
        fail('PROJECT_ASSEMBLIES', 'assemblyPrefixes', 'Explicit assembly name prefixes required')


def setup_plan(project, spec_path, output):
    project = safe(project); spec_path = safe(spec_path); spec = load(read(spec_path)); validate_spec(spec)
    context = observed(project, spec.get('engine'))
    destination = project/PROFILE
    if destination.exists():
        fail('PROJECT_ALREADY_BOUND', str(destination), 'Existing binding is preserved; use explicit rebind instead')
    authority = {p: digest_file(inside(project, p)) for p in context['authorityFiles']}
    profile = dict(contract=CONTRACT, schemaVersion=1, projectId=str(uuid.uuid4()), projectRoot=str(project),
                   name=spec['name'], engine=context['engine'], engineVersion=context['version'],
                   tool=dict(name='beaver', version=__version__, implementationSha256=identity()),
                   writableRoots=spec['writableRoots'], generatedRoots=spec.get('generatedRoots', []),
                   namespace=spec.get('namespace'), assemblyPrefixes=spec.get('assemblyPrefixes', []),
                   authorityFiles=authority, transactionPolicy='outside_project_create_only',
                   cachePolicy='outside_project_non_authoritative',
                   isolation='Caller must supply account/save/backend isolation; binding does not create it')
    out = new_output(output, [project, spec_path])
    plan = dict(contract='beaver-project-setup/1', operation='setup', projectRoot=str(project),
                implementationSha256=identity(), expectedProfileSha256=None, profile=profile)
    write_json(out/'project-plan.json', plan); digest = sha(encoded(plan)); write_json(out/'plan-identity.json', dict(sha256=digest))
    return dict(status='planned', report=str(out/'project-plan.json'), planSha256=digest, writes=[PROFILE], providerReadiness='not_run')


def validate_profile(profile):
    if not isinstance(profile, dict) or profile.get('contract') != CONTRACT or profile.get('schemaVersion') != 1:
        fail('PROJECT_PROFILE', 'profile', 'Known project profile required')
    validate_spec(dict(schemaVersion=1, **{k: profile[k] for k in ('engine','name','writableRoots','generatedRoots')},
                       **({k:profile[k] for k in ('namespace','assemblyPrefixes') if profile.get(k) is not None})))
    for path, fingerprint in profile.get('authorityFiles', {}).items():
        relative(path)
        if not isinstance(fingerprint, str) or not re.fullmatch('[a-f0-9]{64}', fingerprint):
            fail('PROJECT_AUTHORITY', path, 'Exact authority fingerprint required')


def check(project, profile_path=None, writes=(), verify_tool=True):
    project = safe(project); path = safe(profile_path or project/PROFILE); profile = load(read(path))
    validate_profile(profile)
    if profile.get('contract') != CONTRACT or profile.get('schemaVersion') != 1 or profile.get('projectRoot') != str(project):
        fail('PROJECT_BINDING_DRIFT', str(path), 'Exact project root binding required; moved projects need explicit rebind')
    if verify_tool and profile.get('tool') != dict(name='beaver', version=__version__, implementationSha256=identity()):
        fail('PROJECT_TOOL_DRIFT', str(path), 'Pinned beaver bytes/version differ; explicitly rebind after review')
    context = observed(project, profile['engine'])
    if context['version'] != profile['engineVersion']:
        fail('PROJECT_ENGINE_DRIFT', str(project), 'Engine version changed since binding')
    if set(context['authorityFiles'])!=set(profile['authorityFiles']):fail('PROJECT_AUTHORITY_DRIFT',str(path),'Package/module authority file set changed; explicit rebind required')
    if any(digest_file(inside(project, p)) != h for p, h in profile['authorityFiles'].items()):
        fail('PROJECT_AUTHORITY_DRIFT', str(path), 'Engine/package/module pins changed; explicitly rebind with reviewed authority changes')
    for rel in writes:
        if not under(rel, profile['writableRoots']):
            fail('PROJECT_WRITE_DENIED', rel, 'Outside explicitly configured writable roots')
        inside(project, rel)
    return dict(profile=profile, path=str(path), sha256=digest_file(path), context=context)


def rebind_plan(project, profile_path, expected_profile, output, accept_authority_change=False, spec_path=None):
    project = safe(project); path = safe(profile_path)
    if digest_file(path) != expected_profile:
        fail('PROJECT_PROFILE_DRIFT', str(path), 'Explicit old profile hash required')
    profile = load(read(path))
    validate_profile(profile)
    if profile.get('contract') != CONTRACT:
        fail('PROJECT_BINDING', str(path), 'Known project binding required')
    context = observed(project, profile['engine'])
    if context['version'] != profile['engineVersion']:
        fail('PROJECT_REBIND_VERSION', str(project), 'Rebinding cannot silently upgrade engine version')
    authority = {p: digest_file(inside(project, p)) for p in context['authorityFiles']}
    if authority != profile['authorityFiles'] and not accept_authority_change:
        fail('PROJECT_REBIND_SOURCE', str(project), 'Root relocation requires identical engine/package authority files')
    destination = project/PROFILE
    if path != destination and destination.exists():
        fail('PROJECT_REBIND_OCCUPIED', str(destination), 'Destination already has an independent binding')
    updated = dict(profile, projectRoot=str(project), authorityFiles=authority, tool=dict(name='beaver', version=__version__, implementationSha256=identity()))
    if spec_path is not None:
        policy = load(read(spec_path)); validate_spec(policy)
        if policy['engine'] != profile['engine']:
            fail('PROJECT_REBIND_ENGINE', str(spec_path), 'A policy review cannot change the bound engine')
        for key in ('name', 'writableRoots', 'generatedRoots', 'namespace', 'assemblyPrefixes'):
            updated[key] = policy.get(key, [] if key in ('generatedRoots', 'assemblyPrefixes') else None)
    out = new_output(output, [project, path])
    plan = dict(contract='beaver-project-setup/1', operation='rebind', projectRoot=str(project), oldProfile=str(path),
                oldProfileSha256=expected_profile, expectedProfileSha256=expected_profile if path == destination else None,
                reviewedAuthorityChanges=authority if authority != profile['authorityFiles'] else {},
                reviewedPolicyChanges={k:updated[k] for k in ('name','writableRoots','generatedRoots','namespace','assemblyPrefixes') if updated.get(k)!=profile.get(k)},
                implementationSha256=identity(), profile=updated)
    write_json(out/'project-plan.json', plan); digest = sha(encoded(plan)); write_json(out/'plan-identity.json', dict(sha256=digest))
    return dict(status='planned', report=str(out/'project-plan.json'), planSha256=digest, writes=[PROFILE], providerReadiness='not_run')


def setup_apply(plan_path, expected, output):
    path = safe(plan_path); raw = read(path); plan = load(raw)
    if sha(raw) != expected or plan.get('contract') != 'beaver-project-setup/1' or plan.get('implementationSha256') != identity():
        fail('PROJECT_PLAN_DRIFT', str(path), 'Exact plan and tool identity required')
    project = safe(plan['projectRoot']); profile = plan['profile']; context = observed(project, profile['engine'])
    validate_profile(profile)
    if profile['projectRoot'] != str(project):
        fail('PROJECT_ROOT', str(project), 'Profile and plan roots differ')
    if context['version'] != profile['engineVersion'] or any(digest_file(inside(project, p)) != h for p, h in profile['authorityFiles'].items()):
        fail('PROJECT_AUTHORITY_DRIFT', str(project), 'Engine/package authority changed after plan')
    out = new_output(output, [project, path.parent]); target = project/PROFILE
    with lock(project):
        actual = digest_file(target) if target.is_file() else None
        if actual != plan['expectedProfileSha256']:
            fail('PROJECT_PROFILE_DRIFT', str(target), 'Existing profile differs; no overwrite')
        if plan.get('oldProfile') and digest_file(plan['oldProfile']) != plan['oldProfileSha256']:
            fail('PROJECT_PROFILE_DRIFT', plan['oldProfile'], 'Original rebind source changed')
        before = read(target) if target.exists() else None
        write_json(out/'before-profile.json', load(before) if before else None)
        target.parent.mkdir(parents=True, exist_ok=True)
        from .transaction_fs import atomic
        atomic(target, encoded(profile))
        if read(target) != encoded(profile):
            fail('PROJECT_PROFILE_READBACK', str(target), 'Profile bytes differ after save')
        result = dict(status='verified', profile=str(target), profileSha256=digest_file(target), projectId=profile['projectId'],
                      operation=plan['operation'], planSha256=expected, providerReadiness='not_run', productSourceChanges=[])
        write_json(out/'receipt.json', result)
    return result


def inspect(project, profile_path=None):
    result = check(project, profile_path)
    return dict(status='ok', profile=result['path'], profileSha256=result['sha256'], **result['profile'], providerReadiness='not_run')

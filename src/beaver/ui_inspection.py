"""Read-only, spec-bound Unity UI inspection."""
from __future__ import annotations
import hashlib, json, os, shutil, subprocess, tempfile
from pathlib import Path
from .adoption import native_lock
from .transaction_fs import lock
from .native_environment import editor_environment
from .errors import fail, BeaverError
from .io import safe, inside, load, write_json, sha
from .unity import editor_path


def _validate_spec(spec):
    allowed={'schemaVersion','mode','assets','resources','expectations'}
    if not isinstance(spec,dict) or set(spec)-allowed or spec.get('schemaVersion')!=1:
        fail('UI_SPEC','spec','Expected schemaVersion 1 and explicit mode/assets/expectations')
    if spec.get('mode') not in ('static-prefab','static-scene','runtime'):
        fail('UI_MODE','mode','Choose static-prefab, static-scene, or runtime explicitly')
    assets=spec.get('assets')
    if not isinstance(assets,list) or not assets or len(assets)>256: fail('UI_ASSETS','assets','Expected 1..256 explicitly selected assets')
    suffix='.prefab' if spec['mode']=='static-prefab' else '.unity' if spec['mode']=='static-scene' else None
    for p in assets:
        if not isinstance(p,str) or not p.startswith('Assets/') or '..' in p.split('/') or '\\' in p or (suffix and not p.endswith(suffix)):
            fail('UI_ASSET_PATH',str(p),'Explicit canonical Assets path required')
    resources=spec.get('resources',[])
    if not isinstance(resources,list) or len(resources)>512 or any(not isinstance(p,str) or not p.startswith('Assets/') or '..' in p.split('/') or '\\' in p for p in resources): fail('UI_RESOURCE_PATHS','resources','Expected bounded explicit project resource paths')
    if len(set(resources))!=len(resources): fail('UI_RESOURCE_PATHS','resources','Duplicate selected resource path')
    ex=spec.get('expectations',[])
    if not isinstance(ex,list) or len(ex)>10000: fail('UI_EXPECTATIONS','expectations','Expected bounded array')
    for row in ex:
        if not isinstance(row,dict) or not {'asset','nodePath','componentType','field','equals','source'}<=set(row) or set(row)-{'asset','nodePath','componentType','field','equals','source'}:
            fail('UI_EXPECTATION','expectations','Each expectation requires asset, nodePath, componentType, field and equals')
        if row['asset'] not in assets or not all(isinstance(row[k],str) for k in ('asset','nodePath','componentType','field','source')) or not row['source'].strip():
            fail('UI_EXPECTATION','expectations','Expectation must bind to a selected asset and string field identity')
    return spec


def _issue(code, asset, node, component, field, observed, expected, source, certainty, next_step):
    stable=json.dumps([code,asset,node,component,field],ensure_ascii=False,separators=(',',':'))
    classification='failure' if code in ('MISSING_SCRIPT','MISSING_REFERENCE','MISSING_GLYPH','BUTTON_INVALID_PERSISTENT_CALL') else 'rule_difference' if code=='EXPECTATION_MISMATCH' else 'observation'
    return {'classification':classification,'severity':'error' if classification=='failure' else 'warning','id':'ui-'+hashlib.sha256(stable.encode()).hexdigest()[:16], 'code':code,'asset':asset,'nodePath':node,
            'componentType':component,'field':field,'observed':observed,'expected':expected,
            'expectationSource':source,'certainty':certainty,'nextStep':next_step}


def _typed_value(field):
    value=field.get('value'); kind=field.get('valueType')
    try:
        if kind=='Boolean': return value=='true' if isinstance(value,str) else value
        if kind=='Integer': return int(value)
        if kind=='Float': return float(value)
        if kind in ('Vector2','Vector3','Color'):
            return field.get('numbers')
    except (TypeError,ValueError): return {'invalidTypedValue':value,'valueType':kind}
    return value


def analyze(snapshot, spec):
    spec=_validate_spec(spec)
    if not isinstance(snapshot,dict) or snapshot.get('schemaVersion')!=1 or snapshot.get('mode')!=spec['mode'] or not isinstance(snapshot.get('assets'),list):
        fail('UI_SNAPSHOT','snapshot','Snapshot contract mismatch')
    if snapshot.get('errors'): fail('UI_COLLECTION_ERRORS','snapshot.errors','Native collector reported errors: '+str(snapshot['errors']))
    if spec['mode']=='runtime' and snapshot.get('runtimeObserved') is not True: fail('UI_RUNTIME_UNOBSERVED','snapshot.runtimeObserved','Runtime spec requires an explicitly live collection')
    selected=set(spec['assets']); by_path={a.get('path'):a for a in snapshot['assets'] if isinstance(a,dict)}
    if set(by_path)!=selected: fail('UI_SELECTION_DRIFT','snapshot.assets','Snapshot must contain exactly the explicitly selected assets')
    resources=snapshot.get('resources',[])
    if {r.get('path') for r in resources}!=set(spec.get('resources',[])): fail('UI_RESOURCE_SELECTION_DRIFT','snapshot.resources','Resource identity inventory must cover exactly the selected resource paths')
    issues=[]
    for asset in sorted(by_path):
        row=by_path[asset]; components=row.get('components',[]); nodes=row.get('nodes',[])
        if row.get('missingScripts',0): issues.append(_issue('MISSING_SCRIPT',asset,'','','m_Script',row['missingScripts'],'0','native collector','high','恢复缺失脚本并重新采集'))
        if row.get('overflow',[]):
            for item in row['overflow']:
                if not next((n.get('activeInHierarchy',True) for n in nodes if n.get('path')==item.get('nodePath')),True): continue
                issues.append(_issue('TEXT_OVERFLOW',asset,item.get('nodePath',''),item.get('componentType',''),'overflow',item.get('observed'),'no overflow','native text bounds','medium','核对字体、容器尺寸和布局'))
        if row.get('missingGlyphs',[]):
            for item in row['missingGlyphs']:
                if not next((n.get('activeInHierarchy',True) for n in nodes if n.get('path')==item.get('nodePath')),True): continue
                if item.get('glyphLookupStatus')=='unknown':
                    issues.append(_issue('GLYPH_LOOKUP_UNKNOWN',asset,item.get('nodePath',''),item.get('componentType',''),'font',item.get('glyphLookupError'),'successful fallback-aware glyph query','native TMP font API','low','检查TMP Font Asset与当前Unity版本的HasCharacter接口'))
                elif item.get('glyphLookupStatus')=='missing' or item.get('characters'):
                    issues.append(_issue('MISSING_GLYPH',asset,item.get('nodePath',''),item.get('componentType',''),'text',item.get('characters'),'all glyphs available','native font asset lookup','high','补齐字体字形并在目标运行时复核'))
        for exp in spec.get('expectations',[]):
            if exp['asset']!=asset: continue
            hit=[c for c in components if c.get('nodePath')==exp['nodePath'] and c.get('type')==exp['componentType']]
            observed=[next((_typed_value(f) for f in c.get('fields',[]) if f.get('propertyPath')==exp['field'] or f.get('name')==exp['field']),None) for c in hit]
            if len(observed)!=1 or observed[0]!=exp['equals']:
                issues.append(_issue('EXPECTATION_MISMATCH',asset,exp['nodePath'],exp['componentType'],exp['field'],observed,exp['equals'],exp['source'],'high','检查序列化字段或修正明确的预期'))
        for item in row.get('maskCandidates',[]):
            if next((n.get('activeInHierarchy',True) for n in nodes if n.get('path')==item.get('nodePath')),True):
                issues.append(_issue('RECT_MASK_CLIP_CANDIDATE',asset,item.get('nodePath',''),item.get('componentType',''),'RectMask2D',item.get('observed'),'within mask bounds','native RectMask2D geometry','medium','核对滚动视口裁切范围及内容尺寸'))
        for item in row.get('occlusionCandidates',[]):
            if next((n.get('activeInHierarchy',True) for n in nodes if n.get('path')==item.get('nodePath')),True):
                issues.append(_issue('GRAPHIC_OVERLAP_CANDIDATE',asset,item.get('nodePath',''),item.get('componentType',''),'geometry',item.get('observed'),item.get('relatedNodePath'),'native same-parent RectTransform bounds','low','在目标分辨率检查层级绘制顺序和Graphic射线阻挡'))
        types={c.get('type','') for c in components}
        if spec['mode']=='static-scene' and any(t.endswith('.Canvas') for t in types):
            if not any(t.endswith('.GraphicRaycaster') for t in types):
                issues.append(_issue('CANVAS_NO_RAYCASTER',asset,'','UnityEngine.Canvas','GraphicRaycaster',False,True,'native component inventory','high','确认该Canvas是否需要接收uGUI输入'))
        by_node={}
        for c in components: by_node.setdefault(c.get('nodePath',''),[]).append(c)
        for node_path,node_components in by_node.items():
            c_types={c.get('type','') for c in node_components}
            if any(t.endswith('LayoutGroup') for t in c_types) and any(t.endswith('ContentSizeFitter') for t in c_types):
                issues.append(_issue('LAYOUT_CONFLICT_CANDIDATE',asset,node_path,'UnityEngine.UI.LayoutGroup+ContentSizeFitter','layout',sorted(c_types),'no same-object layout sizing conflict','native component inventory','medium','检查LayoutGroup控制尺寸与ContentSizeFitter是否争用同一轴'))
            if any(t.endswith('.Button') for t in c_types):
                graphics=[c for c in node_components if c.get('type','').endswith(('.Image','.RawImage','.Text')) or c.get('type','').startswith('TMPro.')]
                raycast=any(next((f.get('value') for f in g.get('fields',[]) if f.get('name')=='m_RaycastTarget'),'true')!='false' for g in graphics)
                if not raycast and next((n.get('activeInHierarchy',True) for n in nodes if n.get('path')==node_path),True):
                    issues.append(_issue('BUTTON_RAYCAST_CANDIDATE',asset,node_path,'UnityEngine.UI.Button','raycastTarget',False,True,'native Graphic component inventory','medium','确认按钮节点或其子节点是否有可射线检测的Graphic'))
        for n in nodes:
            if not n.get('activeInHierarchy',True): continue
            amin=n.get('anchorMin',[]);amax=n.get('anchorMax',[])
            if len(amin)==2 and len(amax)==2 and (any(x<0 or x>1 for x in amin+amax) or any(amin[i]>amax[i] for i in range(2))):
                issues.append(_issue('INVALID_ANCHOR_CANDIDATE',asset,n.get('path',''),'RectTransform','anchors',{'min':amin,'max':amax},'ordered normalized anchors in [0,1]','native RectTransform','medium','核对是否为有意使用的越界锚点'))
        for c in components:
            for frow in c.get('fields',[]):
                f=frow.get('propertyPath',''); v=frow.get('objectReference')
                if frow.get('referenceState')=='missing':
                    issues.append(_issue('MISSING_REFERENCE',asset,c.get('nodePath',''),c.get('type',''),f,frow.get('value'),'valid object reference','native SerializedProperty','high','在 Editor 中确认该引用并修复失效对象'))
                elif frow.get('referenceState')=='bound' and isinstance(v,dict) and v.get('scope')=='external':
                    issues.append(_issue('UNRESOLVED_REFERENCE',asset,c.get('nodePath',''),c.get('type',''),f,v,'persistent asset reference','native serialized field','high','在 Inspector 中确认并重新绑定引用'))
        for e in row.get('events',[]):
            for bad in e.get('invalidCalls',[]): issues.append(_issue('BUTTON_INVALID_PERSISTENT_CALL',asset,e.get('nodePath',''),e.get('componentType',''),'onClick',bad,'valid persistent target and method','native UnityEvent metadata','high','检查事件目标组件和公共方法是否仍存在'))
        if spec['mode']=='runtime' and snapshot.get('runtimeObserved') is True:
            for hit in row.get('hitTests',[]):
                if hit.get('raycastObserved') is True and hit.get('state')=='blocked_hit':
                    observed={k:hit.get(k) for k in ('stage','state','buttonPath','screenPosition','topHitPath','topHitType','topHitCanvasPath','topHitCamera','raycastCamera','canvasRenderMode','cameraState','buttonIsAncestor','source')}
                    issues.append(_issue('RUNTIME_BUTTON_BLOCKED_HIT_CANDIDATE',asset,hit.get('buttonPath',''),'UnityEngine.UI.Button','hitTests',observed,'Button or descendant is top EventSystem raycast result','live EventSystem.RaycastAll sample','medium','核对overlay是否为预期遮挡，并在业务现场判断按钮是否需接收指针'))
        for n in nodes:
            rect=n.get('rect') or {}; size=rect.get('rect') if isinstance(rect,dict) else rect
            if not (isinstance(size,list) and len(size)==4): size=rect.get('sizeDelta') if isinstance(rect,dict) else n.get('sizeDelta')
            if isinstance(size,list) and len(size)==4: size=size[2:]
            if n.get('activeInHierarchy',True) and isinstance(size,list) and len(size)==2 and (size[0]<=0 or size[1]<=0):
                issues.append(_issue('ZERO_RECT_SIZE',asset,n.get('path',''),'RectTransform','sizeDelta',size,'positive rendered size','native RectTransform','medium','检查锚点、布局组与尺寸控制者'))

    seen=set(); unique=[]
    for issue in issues:
        if issue['id'] not in seen: seen.add(issue['id']); unique.append(issue)
    for issue in unique:
        row=by_path[issue['asset']]
        issue.update(assetGuid=row.get('guid',''),assetLocalId=row.get('localId',0),assetIdentityState=row.get('identityState','unavailable'),projectIdentity=snapshot.get('projectIdentity'),
                     componentIdentity=next(({'localId':c.get('localId',0),'state':c.get('identityState','unavailable')} for c in row.get('components',[]) if c.get('nodePath')==issue.get('nodePath') and c.get('type')==issue.get('componentType')),None),
                     sourceIdentity={'path':issue['asset'],'sourceSha256':row.get('sourceSha256'),'metaSha256':row.get('metaSha256')},
                     evidence={'mode':snapshot.get('mode'),'runtimeObserved':snapshot.get('runtimeObserved',False),'observation':issue.get('observed'),'expected':issue.get('expected')})
    return {'schemaVersion':1,'editorVersion':snapshot.get('editorVersion'),'projectIdentity':snapshot.get('projectIdentity'),'status':'issues' if unique else 'clear','mode':spec['mode'],'runtimeObserved':snapshot.get('runtimeObserved',False),
            'visualAcceptance':'not-established','interactionAcceptance':'not-established','issues':unique}


def inspect(project, spec_path, output, editor, timeout=600):
    """Invoke the read-only companion against exact selected assets."""
    from .batch import inventory, state, cleanup as batch_cleanup
    project=safe(project); spec=_validate_spec(load(safe(spec_path).read_bytes()))
    if spec['mode']=='runtime': fail('UI_RUNTIME_CALLER_REQUIRED','mode','Runtime snapshots must come from BeaverUIInspection.Collect in a playing Editor; inspect launches batchmode only')
    output=safe(output); editor=editor_path(editor)
    for rel in spec['assets']+spec.get('resources',[]):
        source=safe(project/rel)
        if not source.is_relative_to(project) or not source.is_file(): fail('UI_ASSET_MISSING',rel,'Selected asset must be an existing regular file inside the project')
    if output.exists() or output.is_relative_to(project) or project.is_relative_to(output): fail('UI_OUTPUT_EXISTS',str(output),'Use a fresh output directory outside the project')
    output.mkdir(parents=True); stage=output/'native';stage.mkdir()
    helper=inside(project,'Assets/BeaverUIInspection');owned={};report=None;primary_error=None;cleanup_ok=False
    with lock(project):
        native_lock(project)
        if helper.exists() or Path(str(helper)+'.meta').exists(): fail('UI_HELPER_CONFLICT',str(helper),'Temporary helper location occupied')
        before_inventory=inventory(project)
        tracked=sorted(set((spec['assets']+spec.get('resources',[]))+[x+'.meta' for x in (spec['assets']+spec.get('resources',[]))]))
        before_state=state(project,tracked)
        try:
            helper.mkdir(parents=True);(helper/'Editor').mkdir()
            dest=helper/'Editor'/'BeaverUIInspection.cs';shutil.copyfile(Path(__file__).parent/'targets'/'BeaverUIInspection.cs',dest);owned[dest]=sha(dest.read_bytes())
            job={'schemaVersion':1,'projectRoot':str(project),'stage':str(stage),'mode':spec['mode'],'assets':spec['assets'],'resources':spec.get('resources',[])}
            job_path=stage/'ui-job.json';write_json(job_path,job)
            env=editor_environment('BEAVER_UI_JOB',job_path)
            result=subprocess.run([str(editor),'-batchmode','-nographics','-projectPath',str(project),'-executeMethod','BeaverUIInspection.Run','-logFile',str(stage/'editor.log')],env=env,capture_output=True,text=True,timeout=timeout)
            if result.returncode!=0: fail('UI_EDITOR_FAILED',str(stage/'editor.log'),'Unity Editor batch collector failed with exit code '+str(result.returncode))
            snap=load((stage/'ui-snapshot.json').read_bytes())
            for asset in snap.get('assets',[])+snap.get('resources',[]):
                src=safe(project/asset['path']);meta=safe(Path(str(src)+'.meta'))
                asset['sourceSha256']=sha(src.read_bytes());asset['metaSha256']=sha(meta.read_bytes()) if meta.is_file() else None
            write_json(output/'ui-snapshot.json',snap)
            report=analyze(snap,spec);report['snapshotPath']=str(output/'ui-snapshot.json');report['editorExitCode']=result.returncode
        except BaseException as error:
            primary_error=BeaverError("UI_EDITOR_TIMEOUT",str(stage),"Owned collector exceeded the explicit budget; inspect the retained provider log") if isinstance(error,subprocess.TimeoutExpired) else error
        finally:
            cleanup_ok=batch_cleanup(project,helper,owned) if helper.exists() else False
            after_state=state(project,tracked)
            after_inventory=inventory(project)
        if primary_error: raise primary_error
        if not cleanup_ok: fail('UI_HELPER_CLEANUP','Assets/BeaverUIInspection','Temporary helper changed or contains unexpected files; manual inspection required')
        changed=[p for p in tracked if before_state.get(p)!=after_state.get(p)]
        unexpected=sorted(p for p in set(before_inventory)|set(after_inventory) if before_inventory.get(p)!=after_inventory.get(p) and p not in set(tracked))
        report['projectModifiedPaths']=changed
        report['unplannedProjectChanges']=unexpected
        report['sourceUnchanged']=not changed
        report_path=output/'ui-report.json'
        if changed or unexpected:
            report['status']='failed';write_json(report_path,report)
            fail('UI_PROJECT_MODIFIED',str(report_path),'Read-only inspection changed selected inputs or other project paths: '+str(changed+unexpected))
        write_json(report_path,report)
        return dict(report,report=str(report_path),nativeReadback="read_only_native_snapshot")

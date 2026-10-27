#if UNITY_EDITOR
using System;
using System.IO;
using System.Linq;
using System.Collections.Generic;
using System.Reflection;
using UnityEngine;
using UnityEditor;
using UnityEditor.SceneManagement;
using UnityEngine.SceneManagement;
using UnityEngine.UI;
using UnityEngine.EventSystems;

public static class BeaverUIInspection {
    [Serializable] public class Job { public int schemaVersion; public string projectRoot,stage,mode; public string[] assets,resources; }
    [Serializable] public class Ref { public string guid,type,nodePath,componentType,scope,identityState; public long localId; }
    [Serializable] public class FieldRow { public string name,propertyPath,value,valueType,referenceState,acceptedType; public string[] assignableTypes; public float[] numbers; public Ref objectReference; }
    [Serializable] public class MethodRow { public string name,returnType; public string[] parameterTypes; public bool @public,@static; }
    [Serializable] public class ComponentRow { public string nodePath,type,identityState; public long localId; public string[] assignableTypes; public FieldRow[] fields; public MethodRow[] methods; }
    [Serializable] public class NodeRow { public string path,parentPath; public bool activeSelf,activeInHierarchy; public int siblingIndex; public float[] anchorMin,anchorMax,pivot,anchoredPosition,sizeDelta,rect; }
    [Serializable] public class CallRow { public Ref target; public string method,mode; public string[] arguments; }
    [Serializable] public class EventRow { public string nodePath,componentType,field; public CallRow[] persistentCalls; public string[] invalidCalls; }
    [Serializable] public class DiagnosticRow { public string nodePath,componentType,observed,characters,relatedNodePath,glyphLookupStatus,glyphLookupError; }
    [Serializable] public class HitTestRow { public string stage,state,buttonPath,topHitPath,topHitType,topHitCanvasPath,topHitCamera,raycastCamera,canvasRenderMode,cameraState,reason,source; public float[] screenPosition; public bool raycastObserved,buttonIsAncestor; }
    [Serializable] public class AssetRow { public string path,guid,type,identityState; public long localId; public int missingScripts; public NodeRow[] nodes; public ComponentRow[] components; public EventRow[] events; public DiagnosticRow[] overflow,missingGlyphs,occlusionCandidates,maskCandidates; public HitTestRow[] hitTests; }
    [Serializable] public class ResourceRow { public string path,guid,type,name,identityState; public long localId; public string[] assignableTypes; }
    [Serializable] public class Snapshot { public int schemaVersion=1; public string mode,editorVersion; public bool runtimeObserved; public string projectIdentity; public AssetRow[] assets; public ResourceRow[] resources; public string[] errors; }
    static Job job; static bool includeRootIdentity; static readonly List<ResourceRow> resources=new List<ResourceRow>(); static Scene sceneContext; static readonly List<AssetRow> assets=new List<AssetRow>(); static readonly List<string> errors=new List<string>();
    static bool Identity(UnityEngine.Object o,out string guid,out long id) { guid="";id=0;if(o==null)return false;if(AssetDatabase.TryGetGUIDAndLocalFileIdentifier(o,out guid,out id))return true;var source=PrefabUtility.GetCorrespondingObjectFromSource(o);if(source!=null&&AssetDatabase.TryGetGUIDAndLocalFileIdentifier(source,out guid,out id))return true;guid="";id=0;return false; }
    static string PathOf(Transform root,Transform t) { return t==root?"":AnimationUtility.CalculateTransformPath(t,root); }
    static string[] Types(Type t) { var rows=new List<string>(); for(var x=t;x!=null;x=x.BaseType) rows.Add(x.FullName); foreach(var i in t.GetInterfaces())rows.Add(i.FullName); return rows.Distinct().OrderBy(x=>x,StringComparer.Ordinal).ToArray(); }
    static Ref RefFor(UnityEngine.Object obj,Transform root) {
        if(obj==null)return null; string guid;long id;bool identified=Identity(obj,out guid,out id);
        var c=obj as Component; var g=obj as GameObject; var t=c?c.transform:g?g.transform:null;
        bool local=t&&root&&(t==root||t.IsChildOf(root));bool sceneNode=t&&sceneContext.IsValid()&&t.gameObject.scene==sceneContext;bool node=local||sceneNode;bool asset=!String.IsNullOrEmpty(guid);string nodePath="";
        if(local)nodePath=includeRootIdentity?(root.name+"/"+PathOf(root,t)).TrimEnd('/'):PathOf(root,t);
        else if(sceneNode){var sceneRoot=t.root;nodePath=(sceneRoot.name+"/"+AnimationUtility.CalculateTransformPath(sceneRoot,t)).TrimEnd('/');}
        return new Ref{guid=node?"":asset?guid:"",localId=id,type=obj.GetType().FullName,nodePath=node?nodePath:"",componentType=c?c.GetType().FullName:"",scope=node?"node":asset?"asset":"external",identityState=identified?"available":"unavailable"};
    }
    static string PropertyValue(SerializedProperty p) {
        switch(p.propertyType) {
            case SerializedPropertyType.String:return p.stringValue;
            case SerializedPropertyType.Boolean:return p.boolValue.ToString().ToLowerInvariant();
            case SerializedPropertyType.Integer:return p.longValue.ToString(System.Globalization.CultureInfo.InvariantCulture);
            case SerializedPropertyType.Float:return p.doubleValue.ToString("R",System.Globalization.CultureInfo.InvariantCulture);
            case SerializedPropertyType.Enum:return p.enumNames!=null&&p.enumValueIndex>=0&&p.enumValueIndex<p.enumNames.Length?p.enumNames[p.enumValueIndex]:"<unknown>";
            case SerializedPropertyType.ObjectReference:return p.objectReferenceValue?p.objectReferenceValue.name:"null";
            case SerializedPropertyType.Vector2:return p.vector2Value.ToString("R");
            case SerializedPropertyType.Vector3:return p.vector3Value.ToString("R");
            case SerializedPropertyType.Color:return p.colorValue.ToString("R");
            default:return "<unsupported:"+p.propertyType+">";
        }
    }
    static bool HasMissingObjectReference(SerializedProperty p) {
        var current=typeof(SerializedProperty).GetProperty("objectReferenceEntityIdValue");
        if(current!=null){var value=current.GetValue(p,null);return value!=null&&!value.Equals(Activator.CreateInstance(current.PropertyType));}
        var legacy=typeof(SerializedProperty).GetProperty("objectReferenceInstanceIDValue");return legacy!=null&&Convert.ToInt32(legacy.GetValue(p,null))!=0;
    }
    static FieldRow[] Fields(Component c,Transform root) {
        var rows=new List<FieldRow>(); var so=new SerializedObject(c); var p=so.GetIterator(); bool enter=true;
        while(p.Next(enter)) { enter=false; if(p.propertyPath=="m_Script"||p.propertyPath=="m_GameObject"||p.propertyPath.StartsWith("m_Prefab"))continue;
            var f=new FieldRow{name=p.name,propertyPath=p.propertyPath,value=PropertyValue(p),valueType=p.propertyType.ToString(),referenceState="",acceptedType="",assignableTypes=new string[0]};
            if(p.propertyType==SerializedPropertyType.Vector2){var v=p.vector2Value;f.numbers=new[]{v.x,v.y};}
            else if(p.propertyType==SerializedPropertyType.Vector3){var v=p.vector3Value;f.numbers=new[]{v.x,v.y,v.z};}
            else if(p.propertyType==SerializedPropertyType.Color){var v=p.colorValue;f.numbers=new[]{v.r,v.g,v.b,v.a};}
            if(p.propertyType==SerializedPropertyType.ObjectReference) {
                var obj=p.objectReferenceValue; f.objectReference=RefFor(obj,root); f.referenceState=obj?"bound":HasMissingObjectReference(p)?"missing":"null";
                var field=SerializedField(c.GetType(),p.name); var ft=field==null?null:field.FieldType;if(ft!=null&&(ft.IsArray||ft.IsGenericType&&ft.GetGenericTypeDefinition()==typeof(List<>))&&p.propertyPath.Contains("Array.data")){ft=null;}f.acceptedType=ft==null?"<unknown-or-unsupported>":ft.FullName;if(ft!=null)f.assignableTypes=Types(ft);
            }
            rows.Add(f);
        }
        return rows.ToArray();
    }
    static FieldInfo SerializedField(Type t,string name) { for(var x=t;x!=null;x=x.BaseType){var f=x.GetField(name,BindingFlags.Instance|BindingFlags.Public|BindingFlags.NonPublic|BindingFlags.DeclaredOnly);if(f!=null)return f;}return null; }
    static MethodRow[] Methods(Component c) { return c.GetType().GetMethods(BindingFlags.Instance|BindingFlags.Static|BindingFlags.Public).Where(m=>!m.IsSpecialName).Select(m=>new MethodRow{name=m.Name,returnType=m.ReturnType.FullName,parameterTypes=m.GetParameters().Select(p=>p.ParameterType.FullName).ToArray(),@public=m.IsPublic,@static=m.IsStatic}).ToArray(); }
    static void InspectTMP(Component c,string path,List<DiagnosticRow> overs,List<DiagnosticRow> glyphs,RectTransform rt) {
        var behaviour=c as Behaviour;if(behaviour==null||!behaviour.isActiveAndEnabled)return;
        var t=c.GetType();var text=t.GetProperty("text")?.GetValue(c,null) as string??"";var overflow=t.GetProperty("isTextOverflowing")?.GetValue(c,null);
        if(overflow is bool&&((bool)overflow))overs.Add(new DiagnosticRow{nodePath=path,componentType=t.FullName,observed="TMP isTextOverflowing=true"});
        if(rt!=null){var preferred=t.GetMethod("GetPreferredValues",new[]{typeof(string)});var val=preferred==null?null:preferred.Invoke(c,new object[]{text});if(val is Vector2&&((Vector2)val).x>rt.rect.width+0.5f)overs.Add(new DiagnosticRow{nodePath=path,componentType=t.FullName,observed="preferredWidth="+((Vector2)val).x.ToString("R",System.Globalization.CultureInfo.InvariantCulture)+" rectWidth="+rt.rect.width.ToString("R",System.Globalization.CultureInfo.InvariantCulture)});}
        var font=t.GetProperty("font")?.GetValue(c,null);if(font==null){glyphs.Add(new DiagnosticRow{nodePath=path,componentType=t.FullName,glyphLookupStatus="unknown",glyphLookupError="TMP font asset unavailable"});return;}
        var has=font.GetType().GetMethod("HasCharacters",new[]{typeof(string),typeof(uint[]).MakeByRefType(),typeof(bool),typeof(bool)});
        if(has==null){glyphs.Add(new DiagnosticRow{nodePath=path,componentType=t.FullName,glyphLookupStatus="unknown",glyphLookupError="TMP fallback-aware HasCharacters(string,out uint[],bool,bool) API unavailable"});return;}
        try {
            var args=new object[]{new string(text.Where(ch=>!Char.IsWhiteSpace(ch)).ToArray()),null,true,false};
            var complete=(bool)has.Invoke(font,args);var missing=args[1] as uint[];
            if(!complete&&(missing==null||missing.Length==0)){glyphs.Add(new DiagnosticRow{nodePath=path,componentType=t.FullName,glyphLookupStatus="unknown",glyphLookupError="TMP font lookup not initialized or no missing-codepoint evidence returned"});return;}
            if(missing!=null&&missing.Length>0)glyphs.Add(new DiagnosticRow{nodePath=path,componentType=t.FullName,characters=String.Join(" ",missing.Distinct().Select(x=>x.ToString("X"))),glyphLookupStatus="missing"});
        } catch(Exception e){glyphs.Add(new DiagnosticRow{nodePath=path,componentType=t.FullName,glyphLookupStatus="unknown",glyphLookupError=e.GetBaseException().GetType().Name+": "+e.GetBaseException().Message});}
    }

    static void CollectAsset(string path,GameObject root,bool includeRoot=false) {
        includeRootIdentity=includeRoot;var ar=new AssetRow{path=path,missingScripts=0}; string guid;long local;bool assetId=Identity(AssetDatabase.LoadMainAssetAtPath(path),out guid,out local);ar.guid=guid;ar.localId=local;ar.identityState=assetId?"available":"unavailable";ar.type=Path.GetExtension(path)==".prefab"?"Prefab":"Scene";
        var ns=new List<NodeRow>();var cs=new List<ComponentRow>();var es=new List<EventRow>();var overs=new List<DiagnosticRow>();var glyphs=new List<DiagnosticRow>();var occlusions=new List<DiagnosticRow>();var masks=new List<DiagnosticRow>();var graphics=new List<Graphic>();
        foreach(var tr in root.GetComponentsInChildren<Transform>(true)) {
            var np=PathOf(root.transform,tr);if(includeRoot)np=String.IsNullOrEmpty(np)?root.name:root.name+"/"+np;var rt=tr as RectTransform;
            ns.Add(new NodeRow{path=np,parentPath=tr.parent==root.transform?(includeRoot?root.name:""):tr.parent?(includeRoot?root.name+"/":"")+PathOf(root.transform,tr.parent):"",activeSelf=tr.gameObject.activeSelf,activeInHierarchy=tr.gameObject.activeInHierarchy,siblingIndex=tr.GetSiblingIndex(),anchorMin=rt?new[]{rt.anchorMin.x,rt.anchorMin.y}:new float[0],anchorMax=rt?new[]{rt.anchorMax.x,rt.anchorMax.y}:new float[0],pivot=rt?new[]{rt.pivot.x,rt.pivot.y}:new float[0],anchoredPosition=rt?new[]{rt.anchoredPosition.x,rt.anchoredPosition.y}:new float[0],sizeDelta=rt?new[]{rt.sizeDelta.x,rt.sizeDelta.y}:new float[0],rect=rt?new[]{rt.rect.x,rt.rect.y,rt.rect.width,rt.rect.height}:new float[0]});
            foreach(var c in tr.GetComponents<Component>()) {if(c==null){ar.missingScripts++;continue;}var graphic=c as Graphic;if(graphic&&graphic.isActiveAndEnabled)graphics.Add(graphic);string cg;long cl;bool componentId=Identity(c,out cg,out cl);cs.Add(new ComponentRow{nodePath=np,type=c.GetType().FullName,identityState=componentId?"available":"unavailable",localId=cl,assignableTypes=Types(c.GetType()),fields=Fields(c,root.transform),methods=Methods(c)});if(c.GetType().FullName=="TMPro.TextMeshProUGUI"||c.GetType().FullName=="TMPro.TextMeshPro")InspectTMP(c,np,overs,glyphs,rt);
                var text=c as Text;if(text&&text.font&&tr is RectTransform){var rtText=(RectTransform)tr;var missing=new string(text.text.Where(ch=>!Char.IsWhiteSpace(ch)&&!text.font.HasCharacter(ch)).Distinct().ToArray());if(missing.Length>0)glyphs.Add(new DiagnosticRow{nodePath=np,componentType=c.GetType().FullName,characters=missing});if(text.horizontalOverflow==HorizontalWrapMode.Overflow&&text.preferredWidth>rtText.rect.width+0.5f)overs.Add(new DiagnosticRow{nodePath=np,componentType=c.GetType().FullName,observed=text.preferredWidth.ToString("R",System.Globalization.CultureInfo.InvariantCulture)+">"+rtText.rect.width.ToString("R",System.Globalization.CultureInfo.InvariantCulture)});}var b=c as Button;if(b){var calls=new List<CallRow>();var invalid=new List<string>();for(int i=0;i<b.onClick.GetPersistentEventCount();i++){var target=b.onClick.GetPersistentTarget(i);var method=b.onClick.GetPersistentMethodName(i);var component=target as Component;bool valid=target!=null&&!String.IsNullOrEmpty(method)&&target.GetType().GetMethods(BindingFlags.Instance|BindingFlags.Static|BindingFlags.Public).Any(m=>m.Name==method);if(!valid)invalid.Add(np+":target/method missing");calls.Add(new CallRow{target=RefFor(target,root.transform),method=method,mode=b.onClick.GetPersistentListenerState(i).ToString(),arguments=new string[0]});}es.Add(new EventRow{nodePath=np,componentType=c.GetType().FullName,field="onClick",persistentCalls=calls.ToArray(),invalidCalls=invalid.ToArray()});}
            }
        }
        for(int i=0;i<graphics.Count&&occlusions.Count<1000;i++)for(int j=i+1;j<graphics.Count&&occlusions.Count<1000;j++){var a=graphics[i];var b=graphics[j];if(a.transform.parent!=b.transform.parent||a.transform.GetSiblingIndex()==b.transform.GetSiblingIndex()||a.color.a<=0.01f)continue;var ra=(a.transform as RectTransform);var rb=(b.transform as RectTransform);if(!ra||!rb)continue;var aa=ra.rect;var bb=rb.rect;var ca=ra.TransformPoint(aa.center);var cb=rb.TransformPoint(bb.center);if(Math.Abs(ca.x-cb.x)<(aa.width+bb.width)/2&&Math.Abs(ca.y-cb.y)<(aa.height+bb.height)/2)occlusions.Add(new DiagnosticRow{nodePath=PathOf(root.transform,a.transform),relatedNodePath=PathOf(root.transform,b.transform),componentType=a.GetType().FullName,observed="same-parent bounds overlap; draw order and raycast require review"});}
        foreach(var g in graphics){var rt=g.transform as RectTransform;if(!rt)continue;var mask=g.transform.parent?g.transform.parent.GetComponentInParent<RectMask2D>():null;if(mask){var mr=mask.rectTransform;var a=rt.rect;var b=mr.rect;var ca=rt.TransformPoint(a.center);var cb=mr.TransformPoint(b.center);if(Math.Abs(ca.x-cb.x)<(a.width+b.width)/2&&Math.Abs(ca.y-cb.y)<(a.height+b.height)/2&&(a.width>b.width||a.height>b.height))masks.Add(new DiagnosticRow{nodePath=PathOf(root.transform,g.transform),componentType=g.GetType().FullName,observed="bounds intersect RectMask2D clipping region"});}}
        ar.nodes=ns.ToArray();ar.components=cs.ToArray();ar.events=es.ToArray();ar.overflow=overs.ToArray();ar.missingGlyphs=glyphs.ToArray();ar.occlusionCandidates=occlusions.ToArray();ar.maskCandidates=masks.ToArray();assets.Add(ar);includeRootIdentity=false;
    }
    static void ScanResources(string[] paths) {
        foreach(var path in paths??new string[0]) {
            if(!path.StartsWith("Assets/",StringComparison.Ordinal)||path.Contains("..")||path.Contains("\\"))throw new Exception("Unsafe selected resource path: "+path);
            var objects=AssetDatabase.LoadAllAssetsAtPath(path);if(objects==null||objects.Length==0)throw new Exception("No imported objects at selected resource path: "+path);
            foreach(var o in objects){if(o==null)continue;string guid;long id;bool identified=Identity(o,out guid,out id);resources.Add(new ResourceRow{path=path,guid=guid,localId=id,type=o.GetType().FullName,name=o.name,identityState=identified?"available":"unavailable",assignableTypes=Types(o.GetType())});}
        }
    }
    static void Scan(string path) {
        if(!path.StartsWith("Assets/",StringComparison.Ordinal)||path.Contains("..")||path.Contains("\\"))throw new Exception("Unsafe selected asset path: "+path);
        if(job.mode=="static-prefab") {var root=PrefabUtility.LoadPrefabContents(path);try{CollectAsset(path,root);}finally{PrefabUtility.UnloadPrefabContents(root);}}
        else if(job.mode=="static-scene") {var scene=EditorSceneManager.OpenScene(path,OpenSceneMode.Additive);sceneContext=scene;try{var roots=scene.GetRootGameObjects();int start=assets.Count;foreach(var root in roots)CollectAsset(path,root,true);if(roots.Length==0)assets.Add(new AssetRow{path=path,missingScripts=0,nodes=new NodeRow[0],components=new ComponentRow[0],events=new EventRow[0],overflow=new DiagnosticRow[0],missingGlyphs=new DiagnosticRow[0],occlusionCandidates=new DiagnosticRow[0],maskCandidates=new DiagnosticRow[0]});if(assets.Count>start){var rows=assets.Skip(start).ToArray();var merged=rows[0];merged.nodes=rows.SelectMany(x=>x.nodes).ToArray();merged.components=rows.SelectMany(x=>x.components).ToArray();merged.events=rows.SelectMany(x=>x.events).ToArray();merged.overflow=rows.SelectMany(x=>x.overflow).ToArray();merged.missingGlyphs=rows.SelectMany(x=>x.missingGlyphs).ToArray();merged.occlusionCandidates=rows.SelectMany(x=>x.occlusionCandidates).ToArray();merged.maskCandidates=rows.SelectMany(x=>x.maskCandidates).ToArray();merged.missingScripts=rows.Sum(x=>x.missingScripts);assets.RemoveRange(start,rows.Length);assets.Add(merged);}}finally{sceneContext=default(Scene);EditorSceneManager.CloseScene(scene,true);}}
        else throw new Exception("Runtime mode requires an explicit live Editor caller; batch collector will not claim runtime observation");
    }
    static HitTestRow SampleButtonCenter(GameObject root,Button button) {
        var row=new HitTestRow{stage="button-center",buttonPath=PathOf(root.transform,button.transform),source="live EventSystem.RaycastAll at active Button RectTransform center"};
        if(!button.isActiveAndEnabled||!button.interactable){row.state="skipped";row.reason="inactive-or-disabled";return row;}
        var eventSystem=EventSystem.current;if(eventSystem==null){row.state="unknown";row.reason="EventSystem.current unavailable";return row;}
        var rect=button.transform as RectTransform;if(rect==null){row.state="unknown";row.reason="Button has no RectTransform";return row;}
        var raycaster=button.GetComponentInParent<GraphicRaycaster>();if(raycaster==null){row.state="unknown";row.reason="parent GraphicRaycaster unavailable";return row;}
        var canvas=button.GetComponentInParent<Canvas>();if(canvas==null){row.state="unknown";row.reason="parent Canvas unavailable";return row;}
        var camera=raycaster.eventCamera;row.canvasRenderMode=canvas.renderMode.ToString();row.raycastCamera=camera?camera.name:"";
        if(canvas.renderMode!=RenderMode.ScreenSpaceOverlay&&camera==null){row.state="unknown";row.cameraState="required-camera-unavailable";row.reason="Canvas event camera unavailable";return row;}
        row.cameraState=canvas.renderMode==RenderMode.ScreenSpaceOverlay&&camera==null?"overlay-no-camera-required":"available";
        var world=rect.TransformPoint(rect.rect.center);var screen=RectTransformUtility.WorldToScreenPoint(camera,world);row.screenPosition=new[]{screen.x,screen.y};
        var pointer=new PointerEventData(eventSystem){position=screen};var hits=new List<RaycastResult>();eventSystem.RaycastAll(pointer,hits);row.raycastObserved=true;
        if(hits.Count==0){row.state="miss";return row;}
        var top=hits[0];var hitTransform=top.gameObject?top.gameObject.transform:null;
        row.topHitPath=hitTransform&&(hitTransform==root.transform||hitTransform.IsChildOf(root.transform))?PathOf(root.transform,hitTransform):top.gameObject?top.gameObject.name:"";
        var hitGraphic=top.gameObject?top.gameObject.GetComponent<Graphic>():null;
        var hitComponent=top.gameObject?top.gameObject.GetComponents<Component>().FirstOrDefault(x=>x!=null&&x.GetType()!=typeof(Transform)):null;
        row.topHitType=hitGraphic?hitGraphic.GetType().FullName:hitComponent?hitComponent.GetType().FullName:"unknown";
        var hitCanvas=top.gameObject?top.gameObject.GetComponentInParent<Canvas>():null;row.topHitCanvasPath=hitCanvas?PathOf(root.transform,hitCanvas.transform):"";
        row.topHitCamera=top.module&&top.module.eventCamera?top.module.eventCamera.name:"";
        row.buttonIsAncestor=hitTransform&&(hitTransform==button.transform||hitTransform.IsChildOf(button.transform));
        row.state=row.buttonIsAncestor?"hit":"blocked_hit";return row;
    }
    public static string Collect(GameObject liveRoot,string label) { if(!Application.isPlaying)throw new Exception("Runtime collection requires Application.isPlaying");if(liveRoot==null)throw new ArgumentNullException("liveRoot");assets.Clear();errors.Clear();CollectAsset(label,liveRoot);var row=assets.Last();row.hitTests=liveRoot.GetComponentsInChildren<Button>(true).Select(b=>SampleButtonCenter(liveRoot,b)).ToArray();return JsonUtility.ToJson(new Snapshot{mode="runtime",runtimeObserved=true,editorVersion=Application.unityVersion,projectIdentity=Application.dataPath,assets=assets.ToArray(),resources=resources.ToArray(),errors=errors.ToArray()},true); }
    public static void Run() { try { RunCore(); } catch(Exception e) { Debug.LogException(e); EditorApplication.Exit(1); } }
    static void RunCore() {
        var encoded=Environment.GetEnvironmentVariable("BEAVER_UI_JOB_BASE64");if(String.IsNullOrEmpty(encoded)||encoded.Length>65536)throw new Exception("Explicit BEAVER_UI_JOB_BASE64 required");var jobPath=new System.Text.UTF8Encoding(false,true).GetString(Convert.FromBase64String(encoded));var raw=File.ReadAllText(jobPath,new System.Text.UTF8Encoding(false,true));
        job=JsonUtility.FromJson<Job>(raw); if(job==null||job.schemaVersion!=1||job.assets==null||job.assets.Length==0)throw new Exception("Invalid UI job");
        var snap=new Snapshot{mode=job.mode,runtimeObserved=false,editorVersion=Application.unityVersion,projectIdentity=Application.dataPath,errors=new string[0]};
        foreach(var path in job.assets)try{Scan(path);}catch(Exception e){errors.Add(path+": "+e.Message);}try{ScanResources(job.resources);}catch(Exception e){errors.Add("resources: "+e.Message);}
        snap.assets=assets.ToArray();snap.resources=resources.ToArray();snap.errors=errors.ToArray();Directory.CreateDirectory(job.stage);File.WriteAllText(Path.Combine(job.stage,"ui-snapshot.json"),JsonUtility.ToJson(snap,true));
        EditorApplication.Exit(errors.Count>0?1:0);
    }
}
#endif

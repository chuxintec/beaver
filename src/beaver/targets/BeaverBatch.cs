// Native batch companion: no YAML authoring, no global SaveAssets, no Unpack.
#if UNITY_EDITOR
using System;
using System.IO;
using System.Linq;
using System.Collections.Generic;
using System.Security.Cryptography;
using UnityEngine;
using UnityEditor;
using UnityEditor.SceneManagement;
using UnityEngine.SceneManagement;

public static class BeaverBatch {
    static string JobPath() {
        var encoded=Environment.GetEnvironmentVariable("BEAVER_BATCH_JOB_BASE64");
        if(String.IsNullOrEmpty(encoded)||encoded.Length>65536)throw new Exception("Explicit ASCII-encoded native job required");
        return new System.Text.UTF8Encoding(false,true).GetString(Convert.FromBase64String(encoded));
    }

    [Serializable] public class Action {
        public string kind,path,parent,name,component,property,value,targetNode,targetComponent,assetGuid,method,eventName,mode;
        public long localId;public int index;public bool flag;public float[] numbers;public string[] items;
    }
    [Serializable] public class Step {
        public string kind,assetPath,source,target,oldGuid,newGuid,format,pointer,before,after,payload;
        public long oldLocalId,newLocalId;public string[] consumers;public Action[] actions;
    }
    [Serializable] public class FileRow {public string path,source,sha256;public bool exists;public long bytes;}
    [Serializable] public class Job {
        public string projectRoot,stage,mode,planSha256;public string[] writes;
        public bool requiresReopen;
        public Step[] steps;public FileRow[] patches,restores,installs,preconditions;
    }
    [Serializable] public class NodeRow {public string path;public long localId;public string[] components;}
    [Serializable] public class DependencyRow {public string path;public string[] consumers;}
    [Serializable] public class ResourceState {public string status;}
    [Serializable] public class PrefabRow {
        public string path,guid,kind,basePath,contentSha256,serializedTreeSha256;public int missingScripts;public NodeRow[] nodes;
    }
    [Serializable] public class Report {
        public string status,planSha256,error,mode,editorVersion;public int completedSteps,hashReadOperations;
        public List<FileRow> files=new List<FileRow>();public List<PrefabRow> prefabs=new List<PrefabRow>();
        public List<string> resourceReadbacks=new List<string>();
        public List<string> diagnostics=new List<string>();
        public List<DependencyRow> nativeConsumers=new List<DependencyRow>();
    }
    static Job job;static Report report;
    static string Hash(string path) {if(report!=null)report.hashReadOperations++;using(var hash=SHA256.Create())using(var stream=File.OpenRead(path))return BitConverter.ToString(hash.ComputeHash(stream)).Replace("-","").ToLowerInvariant();}
    static string Full(string relative) {
        if(String.IsNullOrEmpty(relative)||!(relative.StartsWith("Assets/",StringComparison.Ordinal)||relative=="ProjectSettings/EditorSettings.asset")||relative.Contains("\\")||relative.Split('/').Any(p=>p==""||p=="."||p==".."))throw new Exception("Unsafe asset path: "+relative);
        return Path.Combine(job.projectRoot,relative);
    }
    static void Save() {
        report.files.Clear();foreach(var rel in job.writes) {
            var full=Full(rel);var exists=File.Exists(full);
            report.files.Add(new FileRow{path=rel,exists=exists,sha256=exists?Hash(full):"",bytes=exists?new FileInfo(full).Length:0});
        }
        var path=Path.Combine(job.stage,"native-result.json");var temp=path+".tmp";
        File.WriteAllText(temp,JsonUtility.ToJson(report,true));if(File.Exists(path))File.Delete(path);File.Move(temp,path);
    }
    static Transform Find(GameObject root,string path) {
        var t=String.IsNullOrEmpty(path)?root.transform:root.transform.Find(path);
        if(t==null)throw new Exception("Unknown node: "+path);return t;
    }
    static Type TypeFor(string name) {
        var types=AppDomain.CurrentDomain.GetAssemblies().Select(a=>a.GetType(name,false)).Where(t=>t!=null).Distinct().ToArray();
        if(types.Length!=1||!typeof(Component).IsAssignableFrom(types[0])||types[0].IsAbstract||types[0].ContainsGenericParameters)throw new Exception("Expected one existing concrete Component type: "+name);
        return types[0];
    }
    static Component ComponentAt(GameObject root,string path,string typeName) {
        var type=TypeFor(typeName);var items=Find(root,path).GetComponents(type);
        if(items.Length!=1)throw new Exception("Component is missing or ambiguous: "+path+":"+typeName);return items[0];
    }
    static UnityEngine.Object Asset(string guid,long id) {
        var path=AssetDatabase.GUIDToAssetPath(guid);if(String.IsNullOrEmpty(path))throw new Exception("GUID missing: "+guid);
        foreach(var o in AssetDatabase.LoadAllAssetsAtPath(path)) {string g;long local;if(o!=null&&AssetDatabase.TryGetGUIDAndLocalFileIdentifier(o,out g,out local)&&g==guid&&local==id)return o;}
        throw new Exception("LocalID missing: "+guid+":"+id);
    }
    static string Relative(Transform root,Transform t) {return t==root?"":AnimationUtility.CalculateTransformPath(t,root);}
    static void Property(GameObject root,Action a) {
        var blocked=new[]{"m_Script","m_GameObject","m_Father","m_Children","m_CorrespondingSourceObject","m_PrefabInstance","m_PrefabAsset"};
        if(String.IsNullOrEmpty(a.property)||blocked.Contains(a.property.Split('.')[0]))throw new Exception("Identity property forbidden");
        var component=ComponentAt(root,a.path,a.component);var so=new SerializedObject(component);so.Update();var p=so.FindProperty(a.property);
        if(p==null)throw new Exception("Unknown serialized field: "+a.property);
        switch(a.mode) {
            case "string":if(p.propertyType!=SerializedPropertyType.String)throw new Exception("Expected string");p.stringValue=a.value;break;
            case "bool":if(p.propertyType!=SerializedPropertyType.Boolean)throw new Exception("Expected boolean");p.boolValue=Boolean.Parse(a.value);break;
            case "int":if(p.propertyType!=SerializedPropertyType.Integer)throw new Exception("Expected integer");p.longValue=Int64.Parse(a.value,System.Globalization.CultureInfo.InvariantCulture);break;
            case "enum":if(p.propertyType!=SerializedPropertyType.Enum)throw new Exception("Expected enum");p.intValue=Int32.Parse(a.value,System.Globalization.CultureInfo.InvariantCulture);break;
            case "color":if(p.propertyType!=SerializedPropertyType.Color||a.numbers==null||a.numbers.Length!=4)throw new Exception("Expected RGBA color");p.colorValue=new Color(a.numbers[0],a.numbers[1],a.numbers[2],a.numbers[3]);break;
            case "vector2":if(p.propertyType!=SerializedPropertyType.Vector2||a.numbers==null||a.numbers.Length!=2)throw new Exception("Expected Vector2");p.vector2Value=new Vector2(a.numbers[0],a.numbers[1]);break;
            case "float":if(p.propertyType!=SerializedPropertyType.Float)throw new Exception("Expected float");var n=Double.Parse(a.value,System.Globalization.CultureInfo.InvariantCulture);if(Double.IsNaN(n)||Double.IsInfinity(n))throw new Exception("Nonfinite value");p.doubleValue=n;break;
            case "object":
                if(p.propertyType!=SerializedPropertyType.ObjectReference)throw new Exception("Expected object reference");
                if(!String.IsNullOrEmpty(a.assetGuid))p.objectReferenceValue=Asset(a.assetGuid,a.localId);
                else if(a.mode=="null")p.objectReferenceValue=null;
                else p.objectReferenceValue=String.IsNullOrEmpty(a.targetComponent)?(UnityEngine.Object)Find(root,a.targetNode).gameObject:ComponentAt(root,a.targetNode,a.targetComponent);
                break;
            case "null":if(p.propertyType!=SerializedPropertyType.ObjectReference)throw new Exception("Expected object reference");p.objectReferenceValue=null;break;
            default:throw new Exception("Explicit property mode string/bool/int/float/object/null required");
        }
        so.ApplyModifiedPropertiesWithoutUndo();EditorUtility.SetDirty(component);
    }
    static string ObjectKey(UnityEngine.Object value,GameObject root) {
        if(value==null)return "null";
        var go=value as GameObject;var component=value as Component;
        if(component!=null)go=component.gameObject;
        if(go!=null&&(go==root||go.transform.IsChildOf(root.transform)))return "node:"+Relative(root.transform,go.transform)+(component==null?":GameObject":":"+component.GetType().FullName+":"+Array.IndexOf(go.GetComponents<Component>(),component));
        string guid;long id;
        if(AssetDatabase.TryGetGUIDAndLocalFileIdentifier(value,out guid,out id))return "asset:"+guid+":"+id;
        throw new Exception("Unresolved nonpersistent external object in prefab snapshot: "+value.name);
    }
    static string[] lastTreeRows;
    static string TreeHash(GameObject root) {
        var rows=new List<string>();var culture=System.Globalization.CultureInfo.InvariantCulture;
        foreach(var t in root.GetComponentsInChildren<Transform>(true)) {
            var path=Relative(root.transform,t);rows.Add(path+"|node|"+t.GetSiblingIndex()+"|"+t.gameObject.activeSelf);
            var components=t.GetComponents<Component>();
            for(int index=0;index<components.Length;index++) {
                var component=components[index];if(component==null)throw new Exception("Missing script in native snapshot");
                var so=new SerializedObject(component);var p=so.GetIterator();bool enter=true;
                while(p.Next(enter)) {
                    enter=p.propertyType!=SerializedPropertyType.ObjectReference;
                    if(new[]{"m_ObjectHideFlags","m_CorrespondingSourceObject","m_PrefabInstance","m_PrefabAsset","m_GameObject"}.Contains(p.propertyPath)){enter=false;continue;}
                    string value=null;
                    switch(p.propertyType) {
                        case SerializedPropertyType.ObjectReference:value=ObjectKey(p.objectReferenceValue,root);break;
                        case SerializedPropertyType.Integer:value=p.longValue.ToString(culture);break;
                        case SerializedPropertyType.Boolean:value=p.boolValue.ToString();break;
                        case SerializedPropertyType.Float:value=p.doubleValue.ToString("R",culture);break;
                        case SerializedPropertyType.String:value=p.stringValue;break;
                        case SerializedPropertyType.Enum:value=p.intValue.ToString(culture);break;
                        case SerializedPropertyType.ArraySize:value=p.intValue.ToString(culture);break;
                        case SerializedPropertyType.Color:value=p.colorValue.ToString("F8");break;
                        case SerializedPropertyType.Vector2:value=p.vector2Value.ToString("F8");break;
                        case SerializedPropertyType.Vector3:value=p.vector3Value.ToString("F8");break;
                        case SerializedPropertyType.Vector4:value=p.vector4Value.ToString("F8");break;
                        case SerializedPropertyType.Quaternion:value=p.quaternionValue.ToString("F8");break;
                        case SerializedPropertyType.Rect:value=p.rectValue.ToString("F8");break;
                        case SerializedPropertyType.Bounds:value=p.boundsValue.ToString("F8");break;
                    }
                    if(value!=null)rows.Add(path+"|"+index+"|"+component.GetType().FullName+"|"+p.propertyPath+"|"+p.propertyType+"|"+value.Length+":"+value);
                }
            }
        }
        lastTreeRows=rows.ToArray();
        using(var hash=SHA256.Create())return BitConverter.ToString(hash.ComputeHash(System.Text.Encoding.UTF8.GetBytes(String.Join("\n",rows)))).Replace("-","").ToLowerInvariant();
    }
    static void Patch(Step step) {
        var asset=AssetDatabase.LoadAssetAtPath<GameObject>(step.assetPath);if(asset==null)throw new Exception("Prefab missing");
        var originalGuid=AssetDatabase.AssetPathToGUID(step.assetPath);
        var originalIds=PersistentIds(asset);
        var kind=PrefabUtility.GetPrefabAssetType(asset);var root=PrefabUtility.LoadPrefabContents(step.assetPath);
        string expectedTree=null;string[] expectedRows=null;
        try {
            foreach(var a in step.actions) {
                var t=Find(root,a.path);var inherited=PrefabUtility.GetCorrespondingObjectFromSource(t.gameObject)!=null;
                var nearest=PrefabUtility.GetNearestPrefabInstanceRoot(t.gameObject);
                if(inherited&&(kind==PrefabAssetType.Variant||nearest!=null&&nearest!=root)&&(a.kind=="node.remove"||a.kind=="node.reparent"||a.kind=="node.order"||a.kind=="component.remove"))
                    throw new Exception("VARIANT_INHERITED_STRUCTURE: use a separate explicitly planned base edit, or add a new child override in this Variant; base is never modified or unpacked");
                if(a.kind=="binding.reference"||a.kind=="binding.event")BeaverWiring.Apply(root,a);
                else if(BeaverBatchUI.Handles(a.kind))BeaverBatchUI.Apply(root,a);
                else if(a.kind=="property.set")Property(root,a);
                else if(a.kind=="node.add") {
                    if(String.IsNullOrEmpty(a.name)||a.name.Contains('/')||a.name.Contains('\\')||a.name=="."||a.name=="..")throw new Exception("Simple child name required");
                    var parent=Find(root,a.parent);if(parent.Cast<Transform>().Any(c=>c.name==a.name))throw new Exception("Sibling name occupied");
                    var go=new GameObject(a.name,a.mode=="transform"?typeof(Transform):typeof(RectTransform));go.transform.SetParent(parent,false);go.transform.SetSiblingIndex(a.index);
                } else if(a.kind=="node.active") {t.gameObject.SetActive(a.flag);
                } else if(a.kind=="node.remove") {
                    if(t==root.transform)throw new Exception("Cannot delete prefab root");UnityEngine.Object.DestroyImmediate(t.gameObject);
                } else if(a.kind=="node.reparent") {
                    var parent=Find(root,a.parent);if(t==root.transform||parent==t||parent.IsChildOf(t))throw new Exception("Hierarchy cycle/root change forbidden");
                    if(parent.Cast<Transform>().Any(c=>c!=t&&c.name==t.name))throw new Exception("Sibling name collision");t.SetParent(parent,false);t.SetSiblingIndex(a.index);
                } else if(a.kind=="node.order") {
                    if(t==root.transform||a.index<0||a.index>=t.parent.childCount)throw new Exception("Sibling index out of range");t.SetSiblingIndex(a.index);
                } else if(a.kind=="component.add") {
                    var type=TypeFor(a.component);if(typeof(Transform).IsAssignableFrom(type)||t.GetComponent(type)!=null)throw new Exception("Transform or existing component cannot be added");
                    if(t.gameObject.AddComponent(type)==null)throw new Exception("Component add failed");
                } else if(a.kind=="component.remove") {
                    var component=ComponentAt(root,a.path,a.component);if(component is Transform)throw new Exception("Cannot remove Transform");UnityEngine.Object.DestroyImmediate(component);
                } else throw new Exception("Unknown action: "+a.kind);
            }
            expectedTree=TreeHash(root);expectedRows=lastTreeRows;bool success;PrefabUtility.SaveAsPrefabAsset(root,step.assetPath,out success);if(!success)throw new Exception("Prefab save failed");
        } finally {PrefabUtility.UnloadPrefabContents(root);}
        if(AssetDatabase.AssetPathToGUID(step.assetPath)!=originalGuid)throw new Exception("Prefab GUID changed");
        var observedIds=PersistentIds(AssetDatabase.LoadAssetAtPath<GameObject>(step.assetPath));
        foreach(var entry in originalIds)if(observedIds.ContainsKey(entry.Key)&&observedIds[entry.Key]!=entry.Value)throw new Exception("Existing localID changed: "+entry.Key);
        ReadPrefab(step.assetPath,expectedTree,expectedRows);
    }
    static Dictionary<string,long> PersistentIds(GameObject asset) {
        var rows=new Dictionary<string,long>();
        foreach(var t in asset.GetComponentsInChildren<Transform>(true)) {
            string guid;long id;var prefix=Relative(asset.transform,t);
            if(!AssetDatabase.TryGetGUIDAndLocalFileIdentifier(t.gameObject,out guid,out id)||id==0)throw new Exception("Persistent GameObject localID unavailable: "+prefix);
            rows[prefix+":GameObject"]=id;
            var cs=t.GetComponents<Component>();for(int i=0;i<cs.Length;i++)if(cs[i]!=null) {
                if(!AssetDatabase.TryGetGUIDAndLocalFileIdentifier(cs[i],out guid,out id)||id==0)throw new Exception("Persistent component localID unavailable: "+prefix+":"+cs[i].GetType().FullName);
                rows[prefix+":"+i+":"+cs[i].GetType().FullName]=id;
            }
        }
        return rows;
    }
    static void ReadPrefab(string path,string expectedTree=null,string[] expectedRows=null) {
        var asset=AssetDatabase.LoadAssetAtPath<GameObject>(path);var root=PrefabUtility.LoadPrefabContents(path);
        try {
            var actualTree=TreeHash(root);if(expectedTree!=null&&actualTree!=expectedTree) {
                report.diagnostics.AddRange(expectedRows.Except(lastTreeRows).Take(32).Select(s=>"READBACK_BEFORE:"+s));
                report.diagnostics.AddRange(lastTreeRows.Except(expectedRows).Take(32).Select(s=>"READBACK_AFTER:"+s));
                throw new Exception("SERIALIZED_TREE_READBACK: saved/reloaded properties or references differ");
            }
            var row=new PrefabRow{path=path,guid=AssetDatabase.AssetPathToGUID(path),kind=PrefabUtility.GetPrefabAssetType(asset).ToString(),contentSha256=Hash(Full(path)),serializedTreeSha256=actualTree,
                basePath=AssetDatabase.GetAssetPath(PrefabUtility.GetCorrespondingObjectFromSource(asset)),
                nodes=root.GetComponentsInChildren<Transform>(true).Select(t=> {
                    string guid;long id;var persistent=Find(asset,Relative(root.transform,t)).gameObject;
                    if(!AssetDatabase.TryGetGUIDAndLocalFileIdentifier(persistent,out guid,out id)||id==0)throw new Exception("Persistent node localID unavailable: "+Relative(root.transform,t));
                    return new NodeRow{path=Relative(root.transform,t),localId=id,components=t.GetComponents<Component>().Select(c=>c==null?"<missing>":c.GetType().FullName).ToArray()};
                }).ToArray()};
            row.missingScripts=row.nodes.Sum(n=>n.components.Count(c=>c=="<missing>"));report.prefabs.Add(row);
            if(row.missingScripts>0)throw new Exception("Prefab contains missing scripts after save/reload");
            report.diagnostics.AddRange(BeaverBatchUI.Diagnostics(root).Select(d=>path+":"+d));
        } finally {PrefabUtility.UnloadPrefabContents(root);}
    }
    static void Folder(string path) {
        var parent=Path.GetDirectoryName(path).Replace('\\','/');if(parent=="Assets"||AssetDatabase.IsValidFolder(parent))return;
        Folder(parent);var grand=Path.GetDirectoryName(parent).Replace('\\','/');
        if(String.IsNullOrEmpty(AssetDatabase.CreateFolder(grand,Path.GetFileName(parent))))throw new Exception("CreateFolder failed");
    }
    static int ReplaceObject(UnityEngine.Object obj,UnityEngine.Object oldValue,UnityEngine.Object newValue) {
        if(obj==null)return 0;var so=new SerializedObject(obj);var p=so.GetIterator();int changed=0;
        while(p.Next(true))if(p.propertyType==SerializedPropertyType.ObjectReference&&p.name!="m_Script"&&p.objectReferenceValue==oldValue){p.objectReferenceValue=newValue;changed++;}
        if(changed>0){so.ApplyModifiedPropertiesWithoutUndo();EditorUtility.SetDirty(obj);}return changed;
    }
    static string[] Consumers(string oldPath) {
        return AssetDatabase.GetAllAssetPaths().Where(p=>p.StartsWith("Assets/",StringComparison.Ordinal)&&new[]{".prefab",".unity",".asset",".mat",".controller",".anim"}.Contains(Path.GetExtension(p))&&p!=oldPath)
            .Where(p=>AssetDatabase.GetDependencies(p,false).Contains(oldPath)).OrderBy(p=>p,StringComparer.Ordinal).ToArray();
    }
    static string[] AssetIds(string path) {
        return AssetDatabase.LoadAllAssetsAtPath(path).Where(o=>o!=null).Select(o=>{
            string guid;long id;if(!AssetDatabase.TryGetGUIDAndLocalFileIdentifier(o,out guid,out id))throw new Exception("Asset localID unavailable");
            return guid+":"+id+":"+o.GetType().FullName;
        }).OrderBy(s=>s,StringComparer.Ordinal).ToArray();
    }
    static void Preflight() {
        foreach(var row in job.preconditions)if(row.exists?(!File.Exists(FullRead(row.path))||Hash(FullRead(row.path))!=row.sha256):File.Exists(FullRead(row.path)))throw new Exception("BATCH_IMPORT_SOURCE_DRIFT: "+row.path);
        foreach(var step in job.steps) {
            if(step.kind=="reference.replace") {
                var oldPath=AssetDatabase.GUIDToAssetPath(step.oldGuid);
                if(oldPath!=step.assetPath)throw new Exception("Replacement old GUID/path mismatch");
                Asset(step.oldGuid,step.oldLocalId);Asset(step.newGuid,step.newLocalId);
                var consumers=Consumers(oldPath);report.nativeConsumers.Add(new DependencyRow{path=oldPath,consumers=consumers});
                if(!consumers.SequenceEqual(step.consumers.OrderBy(p=>p,StringComparer.Ordinal)))throw new Exception("REFERENCE_UNPLANNED_CONSUMER: inspect nativeConsumers and regenerate complete consumer write set before any batch writes");
            }
        }
    }
    static string FullRead(string path) {
        if(path.StartsWith("Assets/",StringComparison.Ordinal))return Full(path);
        if(!(path.StartsWith("Packages/",StringComparison.Ordinal)||path.StartsWith("ProjectSettings/",StringComparison.Ordinal))||path.Contains("\\")||path.Split('/').Any(p=>p==""||p=="."||p==".."))throw new Exception("Unsafe read path");
        return Path.Combine(job.projectRoot,path);
    }
    static void Replace(Step step) {
        var oldValue=Asset(step.oldGuid,step.oldLocalId);var newValue=Asset(step.newGuid,step.newLocalId);
        if(!oldValue.GetType().IsAssignableFrom(newValue.GetType())&&!newValue.GetType().IsAssignableFrom(oldValue.GetType()))throw new Exception("Replacement object types incompatible");
        foreach(var path in step.consumers) {
            if(path.EndsWith(".prefab",StringComparison.Ordinal)) {
                var root=PrefabUtility.LoadPrefabContents(path);string expectedTree=null;string[] expectedRows=null;int changed=0;try {
                    foreach(var c in root.GetComponentsInChildren<Component>(true))changed+=ReplaceObject(c,oldValue,newValue);
                    if(changed==0)throw new Exception("REFERENCE_OBJECT_NOT_FOUND: consumer has no exact old GUID/localID reference: "+path);
                    expectedTree=TreeHash(root);expectedRows=lastTreeRows;
                    bool success;PrefabUtility.SaveAsPrefabAsset(root,path,out success);if(!success)throw new Exception("Consumer save failed");
                } finally {PrefabUtility.UnloadPrefabContents(root);}ReadPrefab(path,expectedTree,expectedRows);
            } else if(path.EndsWith(".unity",StringComparison.Ordinal)) {
                var scene=EditorSceneManager.OpenScene(path,OpenSceneMode.Single);
                foreach(var root in scene.GetRootGameObjects())foreach(var c in root.GetComponentsInChildren<Component>(true))ReplaceObject(c,oldValue,newValue);
                if(!EditorSceneManager.SaveScene(scene,path))throw new Exception("Scene consumer save failed");
            } else {
                foreach(var o in AssetDatabase.LoadAllAssetsAtPath(path)){ReplaceObject(o,oldValue,newValue);if(o!=null)AssetDatabase.SaveAssetIfDirty(o);}
            }
        }
    }
    static void Recover() {
        if(job.preconditions!=null)Preflight();
        var projectSettings=job.restores.FirstOrDefault(r=>r.exists&&r.path=="ProjectSettings/EditorSettings.asset");
        if(projectSettings!=null) {
            if(Hash(projectSettings.source)!=projectSettings.sha256)throw new Exception("Settings backup drift");
            var provider=AppDomain.CurrentDomain.GetAssemblies().Select(a=>a.GetType("Beaver.Tools.BeaverResourceProduction",false)).FirstOrDefault(t=>t!=null);
            if(provider==null)throw new Exception("Project settings restore provider unavailable");
            try {provider.GetMethod("RestoreProjectSettings").Invoke(null,new object[]{projectSettings.source});}
            catch(System.Reflection.TargetInvocationException error){throw error.InnerException??error;}
        }
        // Reverse engine moves before restoring byte-exact before-images.
        foreach(var step in job.steps.Reverse().Where(s=>s.kind=="asset.move")) {
            if(File.Exists(Full(step.target))&&!File.Exists(Full(step.source))) {
                Folder(step.source);var error=AssetDatabase.MoveAsset(step.target,step.source);if(!String.IsNullOrEmpty(error))throw new Exception(error);
            }
        }
        foreach(var r in job.restores.Where(r=>!r.exists&&!r.path.EndsWith(".meta",StringComparison.Ordinal))) {
            if(File.Exists(Full(r.path))&&!AssetDatabase.DeleteAsset(r.path))throw new Exception("Delete generated asset failed");
        }
        foreach(var r in job.restores.Where(r=>r.exists)) {
            if(Hash(r.source)!=r.sha256)throw new Exception("Backup drift");Directory.CreateDirectory(Path.GetDirectoryName(Full(r.path)));File.Copy(r.source,Full(r.path),true);
        }
        // Only newly created, now empty folders may be removed.
        foreach(var r in job.restores.Where(r=>!r.exists&&r.path.EndsWith(".meta",StringComparison.Ordinal)).OrderByDescending(r=>r.path.Length)) {
            var full=Full(r.path);var nativePath=r.path.Substring(0,r.path.Length-5);var directory=Full(nativePath);
            if(Directory.Exists(directory)) {
                if(Directory.EnumerateFileSystemEntries(directory).Any())throw new Exception("New folder has unknown contents; refusing deletion");
                if(!AssetDatabase.DeleteAsset(nativePath))throw new Exception("Remove generated empty folder failed");
            } else if(!File.Exists(directory)&&File.Exists(full))File.Delete(full);
        }
        AssetDatabase.Refresh(ImportAssetOptions.ForceSynchronousImport);
        foreach(var r in job.restores)if(r.exists?(!File.Exists(Full(r.path))||Hash(Full(r.path))!=r.sha256):File.Exists(Full(r.path)))throw new Exception("Recovery readback differs: "+r.path);
    }
    public static void Run() {
        try {
            var file=JobPath();if(String.IsNullOrEmpty(file))throw new Exception("Explicit job required");
            job=JsonUtility.FromJson<Job>(File.ReadAllText(file));report=new Report{status="running",planSha256=job.planSha256,mode=job.mode,editorVersion=Application.unityVersion};
            if(Application.unityVersion!="6000.6.2f1")throw new Exception("Unexpected native Editor version");
            if(Path.GetFullPath(job.projectRoot)!=Path.GetFullPath(Directory.GetCurrentDirectory()))throw new Exception("Target project identity mismatch");
            Save();
            if(job.mode=="recover")Recover();
            else if(job.mode=="apply") {
                Preflight();
                foreach(var step in job.steps) {
                    if(step.kind=="prefab.patch")Patch(step);
                    else if(step.kind=="prefab.inspect")ReadPrefab(step.assetPath);
                    else if(step.kind=="asset.move") {
                        Folder(step.target);var guid=AssetDatabase.AssetPathToGUID(step.source);var ids=AssetIds(step.source);var error=AssetDatabase.MoveAsset(step.source,step.target);
                        if(!String.IsNullOrEmpty(error)||AssetDatabase.AssetPathToGUID(step.target)!=guid||!ids.SequenceEqual(AssetIds(step.target)))throw new Exception("Move/GUID/localID check failed: "+error);
                    } else if(step.kind=="reference.replace")Replace(step);
                    else if(step.kind=="text.key"||step.kind=="script.key") {
                        var patch=job.patches.Single(p=>p.path==step.assetPath);
                        if(Hash(patch.source)!=patch.sha256)throw new Exception("Patch drift");
                        if(!File.Exists(Full(patch.path))||Hash(Full(patch.path))!=patch.sha256) {
                            File.Copy(patch.source,Full(patch.path),true);AssetDatabase.ImportAsset(patch.path,ImportAssetOptions.ForceSynchronousImport);
                        }
                    } else if(step.kind.StartsWith("resource.",StringComparison.Ordinal)) {
                        var provider=AppDomain.CurrentDomain.GetAssemblies().Select(a=>a.GetType("Beaver.Tools.BeaverResourceProduction",false)).FirstOrDefault(t=>t!=null);
                        if(provider==null)throw new Exception("Declared resource production provider is unavailable");
                        try {
                            provider.GetMethod("Apply").Invoke(null,new object[]{step.kind,step.payload});
                            var evidence=(string)provider.GetMethod(job.requiresReopen?"VerifyBeforeExit":"Verify").Invoke(null,new object[]{step.kind,step.payload});
                            report.resourceReadbacks.Add(evidence);
                            var state=JsonUtility.FromJson<ResourceState>(evidence);
                            if(state==null||String.IsNullOrEmpty(state.status))throw new Exception("Resource verification did not return a status");
                            if(state.status=="needs_action"||state.status=="not_run")throw new Exception("RESOURCE_NEEDS_ACTION: "+step.kind+": "+state.status);
                            if(state.status=="failed"||state.status=="blocked")throw new Exception("Resource verification rejected: "+step.kind+": "+state.status);
                        } catch(System.Reflection.TargetInvocationException error) {throw error.InnerException??error;}
                    } else throw new Exception("Unknown batch step");
                    report.completedSteps++;Save();
                }
            } else if(job.mode=="checkpoint") {
                // Recovery evidence only. Never a resource/business verdict.
                Preflight();
            } else if(job.mode=="verify") {
                Preflight();
                foreach(var step in job.steps) {
                    if(step.kind.StartsWith("resource.",StringComparison.Ordinal)) {
                        var provider=AppDomain.CurrentDomain.GetAssemblies().Select(a=>a.GetType("Beaver.Tools.BeaverResourceProduction",false)).FirstOrDefault(t=>t!=null);
                        if(provider==null)throw new Exception("Resource verification provider unavailable on reopen");
                        string evidence;
                        try {evidence=(string)provider.GetMethod("Verify").Invoke(null,new object[]{step.kind,step.payload});}
                        catch(System.Reflection.TargetInvocationException error){throw error.InnerException??error;}
                        var state=JsonUtility.FromJson<ResourceState>(evidence);
                        if(state==null||String.IsNullOrEmpty(state.status)||new[]{"failed","blocked","needs_action","not_run","pending_editor_reopen"}.Contains(state.status))throw new Exception("Resource reopen verification rejected: "+step.kind);
                        report.resourceReadbacks.Add(evidence);
                    } else if(step.kind=="prefab.patch"||step.kind=="prefab.inspect")ReadPrefab(step.assetPath);
                    report.completedSteps++;
                }
            } else throw new Exception("Unknown mode");
            report.status=job.mode=="checkpoint"?"checkpoint_verified":job.requiresReopen&&job.mode=="apply"?"pending_editor_reopen":"verified";Save();EditorApplication.Exit(0);
        } catch(Exception error) {
            if(report!=null){report.status="failed";report.error=error.ToString();Save();}
            Debug.LogException(error);EditorApplication.Exit(1);
        }
    }
}
#endif

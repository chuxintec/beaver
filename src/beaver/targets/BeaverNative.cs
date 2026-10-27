// beaver native Unity 6 companion. Editor-only, explicit versioned jobs, no shell/eval.
#if UNITY_EDITOR
using System;
using System.IO;
using System.Linq;
using System.Collections.Generic;
using UnityEngine;
using UnityEditor;
using UnityEditor.SceneManagement;
using UnityEditor.Build.Reporting;

public static class BeaverNative {
    static string JobPath() {
        var encoded=Environment.GetEnvironmentVariable("BEAVER_NATIVE_JOB_BASE64");
        if(String.IsNullOrEmpty(encoded)||encoded.Length>65536)throw new Exception("Explicit ASCII-encoded native job required");
        return new System.Text.UTF8Encoding(false,true).GetString(Convert.FromBase64String(encoded));
    }

    [Serializable] public class Node {
        public string id, name, parent, kind, text, spriteGuid;
        public long spriteLocalId;
        public float x,y,width,height,fontSize;
        public float[] color;
    }
    [Serializable] public class Edit {
        public string nodePath, component, property, kind, targetNode, targetComponent, assetGuid;
        public long localId;
        public string value;
    }
    [Serializable] public class Job {
        public int schemaVersion,width,height,durationSeconds,maxSize;
        public string operation, targetRoot, stage, mode, planSha256, sourceSha256;
        public string prefabPath, scenePath, fontAssetPath, sourceFontPath, tmpSettingsPath, productName;
        public string[] scenes;
        public Node[] nodes;
        public Edit[] edits;
        public Move[] moves;
        public string assetPath;
        public float pixelsPerUnit;
        public float[] pivot;
        public bool alphaIsTransparency;
    }
    [Serializable] public class Move { public string source,target; }
    [Serializable] public class MoveRow { public string source,target,guid; public string[] localIds; }
    [Serializable] public class Row {
        public string path,parent,guid;
        public long localId;
        public string[] components;
        public float width,height;
        public bool active,interactable,raycastTarget,fontPresent;
        public string text,spriteGuid;
        public long spriteLocalId;
        public string spriteTexturePath;
        public float spritePixelsPerUnit;
        public Vector2 spritePivot;
        public Rect spriteRect;
    }
    [Serializable] public class Report {
        public string status,operation,mode,planSha256,sourceSha256,error,prefabGuid;
        public List<Row> nodes=new List<Row>();
        public int missingScripts,eventSystems,canvasRaycasters,bindingChecks;
        public string buildResult,buildPath;
        public ulong buildBytes;
        public int buildErrors;
        public List<MoveRow> moves=new List<MoveRow>();
        public int playFrames,playErrors;
        public double playSeconds;
        public string spriteGuid;
        public long spriteLocalId;
        public float spritePixelsPerUnit;
        public Vector2 spritePivot;
        public Rect spriteRect;
    }
    static Job job;
    static Report report;
    const string PlayKey="Beaver.Native.PlayJob";
    static DateTime playStart;
    static bool playStopping;
    [InitializeOnLoadMethod] static void ResumePlay() {
        var path=SessionState.GetString(PlayKey,"");
        if(String.IsNullOrEmpty(path)||!File.Exists(path))return;
        job=JsonUtility.FromJson<Job>(File.ReadAllText(path));
        report=JsonUtility.FromJson<Report>(File.ReadAllText(Path.Combine(job.stage,"native-"+job.mode+".json")));
        playStart=DateTime.UtcNow;playStopping=false;
        EditorApplication.playModeStateChanged+=PlayState;
        EditorApplication.update+=PlayUpdate;
        Application.logMessageReceived+=PlayLog;
    }
    static void PlayLog(string message,string stack,LogType type) {
        if(type==LogType.Error||type==LogType.Exception||type==LogType.Assert){report.playErrors++;Save();}
    }
    static void PlayState(PlayModeStateChange state) {
        if(state==PlayModeStateChange.EnteredPlayMode){playStart=DateTime.UtcNow;}
        if(state==PlayModeStateChange.EnteredEditMode) {
            SessionState.EraseString(PlayKey);EditorApplication.update-=PlayUpdate;EditorApplication.playModeStateChanged-=PlayState;
            Application.logMessageReceived-=PlayLog;
            report.status=report.playErrors==0&&report.playFrames>0?"verified":"failed";Save();EditorApplication.Exit(report.status=="verified"?0:1);
        }
    }
    static void PlayUpdate() {
        if(!EditorApplication.isPlaying||playStopping)return;
        report.playFrames=Math.Max(report.playFrames,Time.frameCount);report.playSeconds=(DateTime.UtcNow-playStart).TotalSeconds;
        if(report.playSeconds>=job.durationSeconds) {
            playStopping=true;Save();EditorApplication.ExitPlaymode();
        }
    }
    static void Play() {
        EditorSceneManager.OpenScene(job.scenePath,OpenSceneMode.Single);
        SessionState.SetString(PlayKey,JobPath());
        ResumePlay();EditorApplication.EnterPlaymode();
    }
    static void Save() {
        File.WriteAllText(Path.Combine(job.stage,"native-"+job.mode+".json"),JsonUtility.ToJson(report,true));
    }
    static string Full(string relative) {
        if(String.IsNullOrEmpty(relative)||!relative.StartsWith("Assets/",StringComparison.Ordinal)||relative.Contains("\\")||relative.Split('/').Any(p=>p==""||p=="."||p==".."))throw new Exception("Unsafe asset path");
        return Path.Combine(job.targetRoot,relative);
    }
    static void Folder(string relative) {
        var parent=Path.GetDirectoryName(relative).Replace('\\','/');
        if(parent=="Assets"||AssetDatabase.IsValidFolder(parent))return;
        Folder(parent);
        var grand=Path.GetDirectoryName(parent).Replace('\\','/');
        if(String.IsNullOrEmpty(AssetDatabase.CreateFolder(grand,Path.GetFileName(parent))))throw new Exception("Could not create native folder: "+parent);
    }
    static void NewPath(string path) {
        if(File.Exists(Full(path))||File.Exists(Full(path+".meta")))throw new Exception("Create-only destination occupied: "+path);
        Folder(path);
    }
    static Color ColorFor(Node n,Color fallback) {
        return n.color!=null && n.color.Length==4?new Color(n.color[0],n.color[1],n.color[2],n.color[3]):fallback;
    }
    static UnityEngine.Object Asset(string guid,long id) {
        var path=AssetDatabase.GUIDToAssetPath(guid);
        if(String.IsNullOrEmpty(path))throw new Exception("Asset GUID missing: "+guid);
        foreach(var obj in AssetDatabase.LoadAllAssetsAtPath(path)) {
            string observed;long local;
            if(obj!=null && AssetDatabase.TryGetGUIDAndLocalFileIdentifier(obj,out observed,out local) && observed==guid && local==id)return obj;
        }
        throw new Exception("Asset localID missing: "+guid+":"+id);
    }
    static TMPro.TMP_FontAsset FontAsset() {
        NewPath(job.fontAssetPath);
        var font=AssetDatabase.LoadAssetAtPath<Font>(job.sourceFontPath);
        if(font==null)throw new Exception("Explicit source font missing: "+job.sourceFontPath);
        var asset=TMPro.TMP_FontAsset.CreateFontAsset(font);
        if(asset==null)throw new Exception("Could not generate self-contained TMP font");
        asset.name="BeaverFont";
        AssetDatabase.CreateAsset(asset,job.fontAssetPath);
        foreach(var texture in asset.atlasTextures)if(texture!=null && !EditorUtility.IsPersistent(texture))AssetDatabase.AddObjectToAsset(texture,asset);
        if(asset.material!=null && !EditorUtility.IsPersistent(asset.material))AssetDatabase.AddObjectToAsset(asset.material,asset);
        var characters=new String(("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789 .:-_+"+String.Join("",job.nodes.Where(n=>n.text!=null).Select(n=>n.text))).Distinct().ToArray());
        string missing;
        if(!asset.TryAddCharacters(characters,out missing))throw new Exception("Source font missing required characters: "+missing);
        EditorUtility.SetDirty(asset);AssetDatabase.SaveAssets();
        return asset;
    }
    static void CreateUI() {
        if(Shader.Find("TextMeshPro/Mobile/Distance Field")==null)throw new Exception("UI_TMP_PROVIDER_MISSING: TMP Essentials shaders must be explicitly initialized in the project before UI generation");
        NewPath(job.prefabPath);NewPath(job.scenePath);
        if(!String.IsNullOrEmpty(job.tmpSettingsPath)) {
            if(Resources.Load<TMPro.TMP_Settings>("TMP Settings")!=null)throw new Exception("Existing TMP settings must be reused, not duplicated");
            NewPath(job.tmpSettingsPath);
            AssetDatabase.CreateAsset(ScriptableObject.CreateInstance<TMPro.TMP_Settings>(),job.tmpSettingsPath);
            AssetDatabase.SaveAssets();
        }
        if(TMPro.TMP_Settings.GetSettings()==null)throw new Exception("UI_TMP_SETTINGS_MISSING: explicitly create TMP Settings or supply an existing project resource");
        var font=FontAsset();
        if(!String.IsNullOrEmpty(job.tmpSettingsPath)) {
            var settings=TMPro.TMP_Settings.GetSettings();var serialized=new SerializedObject(settings);
            serialized.FindProperty("m_defaultFontAsset").objectReferenceValue=font;
            serialized.ApplyModifiedPropertiesWithoutUndo();EditorUtility.SetDirty(settings);AssetDatabase.SaveAssets();
        }
        var scene=EditorSceneManager.NewScene(NewSceneSetup.EmptyScene,NewSceneMode.Single);
        var root=new GameObject("BeaverWindow",typeof(RectTransform),typeof(Canvas),typeof(UnityEngine.UI.CanvasScaler),typeof(UnityEngine.UI.GraphicRaycaster));
        root.GetComponent<Canvas>().renderMode=RenderMode.ScreenSpaceOverlay;
        var scaler=root.GetComponent<UnityEngine.UI.CanvasScaler>();
        scaler.uiScaleMode=UnityEngine.UI.CanvasScaler.ScaleMode.ScaleWithScreenSize;
        scaler.referenceResolution=new Vector2(job.width,job.height);scaler.matchWidthOrHeight=.5f;
        var table=new Dictionary<string,GameObject>();
        foreach(var n in job.nodes) {
            var parent=String.IsNullOrEmpty(n.parent)?root:table[n.parent];
            var go=new GameObject(n.name,typeof(RectTransform));
            go.transform.SetParent(parent.transform,false);
            var rect=go.GetComponent<RectTransform>();
            rect.anchorMin=rect.anchorMax=rect.pivot=new Vector2(.5f,.5f);
            rect.sizeDelta=new Vector2(n.width,n.height);rect.anchoredPosition=new Vector2(n.x,n.y);
            if(n.kind=="text") {
                var text=go.AddComponent<TMPro.TextMeshProUGUI>();
                text.font=font;text.text=n.text ?? "";text.fontSize=n.fontSize>0?n.fontSize:28;
                text.color=ColorFor(n,Color.white);text.alignment=TMPro.TextAlignmentOptions.Center;
                text.raycastTarget=false;
            } else {
                var image=go.AddComponent<UnityEngine.UI.Image>();
                image.color=ColorFor(n,n.kind=="button"?new Color(.15f,.45f,.85f,1):new Color(.08f,.1f,.16f,1));
                image.raycastTarget=n.kind=="button";
                if(!String.IsNullOrEmpty(n.spriteGuid)) {
                    image.sprite=Asset(n.spriteGuid,n.spriteLocalId) as Sprite;
                    if(image.sprite==null)throw new Exception("Image reference is not a Sprite");
                    image.preserveAspect=true;
                }
                if(n.kind=="button") {var button=go.AddComponent<UnityEngine.UI.Button>();button.targetGraphic=image;button.interactable=true;}
                if(n.kind=="list") {
                    var layout=go.AddComponent<UnityEngine.UI.VerticalLayoutGroup>();
                    layout.childControlWidth=false;layout.childControlHeight=false;
                    layout.childForceExpandWidth=false;layout.childForceExpandHeight=false;
                    layout.spacing=12;layout.childAlignment=TextAnchor.MiddleCenter;
                }
            }
            table.Add(n.id,go);
        }
        UnityEngine.UI.LayoutRebuilder.ForceRebuildLayoutImmediate(root.GetComponent<RectTransform>());
        bool success;
        var prefab=PrefabUtility.SaveAsPrefabAsset(root,job.prefabPath,out success);
        if(!success || prefab==null)throw new Exception("Prefab save failed");
        UnityEngine.Object.DestroyImmediate(root);
        PrefabUtility.InstantiatePrefab(prefab,scene);
        new GameObject("EventSystem",typeof(UnityEngine.EventSystems.EventSystem),typeof(UnityEngine.EventSystems.StandaloneInputModule));
        if(!EditorSceneManager.SaveScene(scene,job.scenePath))throw new Exception("Scene save failed");
        AssetDatabase.SaveAssets();
    }
    static Transform Find(GameObject root,string path) {
        var t=String.IsNullOrEmpty(path)?root.transform:root.transform.Find(path);
        if(t==null)throw new Exception("Unknown native node: "+path);
        return t;
    }
    static Component ComponentAt(GameObject root,string nodePath,string type) {
        var go=Find(root,nodePath).gameObject;
        var items=go.GetComponents<Component>().Where(c=>c!=null && c.GetType().FullName==type).ToArray();
        if(items.Length!=1)throw new Exception("Expected one existing component: "+nodePath+":"+type);
        return items[0];
    }
    static void Bind(GameObject root,Edit edit,bool write) {
        var component=ComponentAt(root,edit.nodePath,edit.component);
        var so=new SerializedObject(component);so.Update();
        var p=so.FindProperty(edit.property);
        if(p==null)throw new Exception("Unknown serialized property: "+edit.property);
        if(edit.property=="m_Script"||edit.property=="m_GameObject"||edit.property=="m_Father"||edit.property=="m_Children")throw new Exception("Identity fields cannot be changed");
        if(edit.kind=="string") {
            if(p.propertyType!=SerializedPropertyType.String)throw new Exception("Wrong property type");
            if(write)p.stringValue=edit.value;else if(p.stringValue!=edit.value)throw new Exception("String readback differs");
        } else if(edit.kind=="bool") {
            if(p.propertyType!=SerializedPropertyType.Boolean)throw new Exception("Wrong property type");
            var v=Boolean.Parse(edit.value);if(write)p.boolValue=v;else if(p.boolValue!=v)throw new Exception("Boolean readback differs");
        } else if(edit.kind=="int") {
            if(p.propertyType!=SerializedPropertyType.Integer)throw new Exception("Wrong property type");
            var v=Int32.Parse(edit.value,System.Globalization.CultureInfo.InvariantCulture);if(write)p.intValue=v;else if(p.intValue!=v)throw new Exception("Integer readback differs");
        } else if(edit.kind=="float") {
            if(p.propertyType!=SerializedPropertyType.Float)throw new Exception("Wrong property type");
            var v=Single.Parse(edit.value,System.Globalization.CultureInfo.InvariantCulture);if(Single.IsNaN(v)||Single.IsInfinity(v))throw new Exception("Nonfinite value");
            if(write)p.floatValue=v;else if(p.floatValue!=v)throw new Exception("Float readback differs");
        } else if(edit.kind=="object") {
            if(p.propertyType!=SerializedPropertyType.ObjectReference)throw new Exception("Wrong property type");
            UnityEngine.Object value;
            if(!String.IsNullOrEmpty(edit.assetGuid))value=Asset(edit.assetGuid,edit.localId);
            else if(!String.IsNullOrEmpty(edit.targetComponent))value=ComponentAt(root,edit.targetNode,edit.targetComponent);
            else value=Find(root,edit.targetNode).gameObject;
            if(write)p.objectReferenceValue=value;else if(p.objectReferenceValue!=value)throw new Exception("Reference readback differs");
        } else throw new Exception("Unknown edit kind");
        if(write){so.ApplyModifiedPropertiesWithoutUndo();EditorUtility.SetDirty(component);}
        else report.bindingChecks++;
    }
    static void EditPrefab() {
        var asset=AssetDatabase.LoadAssetAtPath<GameObject>(job.prefabPath);
        if(asset==null)throw new Exception("Prefab missing");
        if(PrefabUtility.GetPrefabAssetType(asset)==PrefabAssetType.Variant)throw new Exception("PREFAB_VARIANT_UNSUPPORTED: edit the base explicitly or use a later override adapter");
        var originalGuid=AssetDatabase.AssetPathToGUID(job.prefabPath);
        var root=PrefabUtility.LoadPrefabContents(job.prefabPath);
        try {
            foreach(var edit in job.edits)Bind(root,edit,true);
            bool saved;PrefabUtility.SaveAsPrefabAsset(root,job.prefabPath,out saved);
            if(!saved)throw new Exception("Edited prefab save failed");
        } finally {PrefabUtility.UnloadPrefabContents(root);}
        if(AssetDatabase.AssetPathToGUID(job.prefabPath)!=originalGuid)throw new Exception("Prefab GUID changed");
    }
    static string NodePath(Transform node,Transform root) {
        if(node==root)return "";
        var parts=new List<string>();
        while(node!=root){parts.Insert(0,node.name);node=node.parent;}
        return String.Join("/",parts);
    }
    static void InspectPrefab() {
        var asset=AssetDatabase.LoadAssetAtPath<GameObject>(job.prefabPath);
        if(asset==null)throw new Exception("Prefab missing");
        report.prefabGuid=AssetDatabase.AssetPathToGUID(job.prefabPath);
        var root=PrefabUtility.LoadPrefabContents(job.prefabPath);
        try {
            Canvas.ForceUpdateCanvases();
            foreach(var t in root.GetComponentsInChildren<Transform>(true)) {
                report.missingScripts+=GameObjectUtility.GetMonoBehavioursWithMissingScriptCount(t.gameObject);
                var rect=t as RectTransform;
                var row=new Row{path=NodePath(t,root.transform),parent=t==root.transform?null:NodePath(t.parent,root.transform),components=t.GetComponents<Component>().Where(c=>c!=null).Select(c=>c.GetType().FullName).ToArray(),active=t.gameObject.activeSelf,width=rect!=null?rect.rect.width:0,height=rect!=null?rect.rect.height:0};
                var persistent=String.IsNullOrEmpty(row.path)?asset.transform:asset.transform.Find(row.path);
                if(persistent==null || !AssetDatabase.TryGetGUIDAndLocalFileIdentifier(persistent,out row.guid,out row.localId))throw new Exception("Saved native node identity missing: "+row.path);
                var button=t.GetComponent<UnityEngine.UI.Button>();
                if(button!=null){row.interactable=button.interactable;row.raycastTarget=button.targetGraphic!=null&&button.targetGraphic.raycastTarget;}
                var text=t.GetComponent<TMPro.TMP_Text>();
                if(text!=null){row.text=text.text;row.fontPresent=text.font!=null;if(!row.fontPresent)throw new Exception("TMP font missing");}
                var image=t.GetComponent<UnityEngine.UI.Image>();
                if(image!=null&&image.sprite!=null){string guid;long id;AssetDatabase.TryGetGUIDAndLocalFileIdentifier(image.sprite,out guid,out id);row.spriteGuid=guid;row.spriteLocalId=id;row.spriteRect=image.sprite.rect;row.spritePixelsPerUnit=image.sprite.pixelsPerUnit;row.spritePivot=image.sprite.pivot;row.spriteTexturePath=AssetDatabase.GetAssetPath(image.sprite.texture);}
                report.nodes.Add(row);
            }
            if(report.missingScripts!=0)throw new Exception("Missing scripts retained; refusing successful validation");
            if(job.operation=="prefab.edit")foreach(var edit in job.edits)Bind(root,edit,false);
        } finally {PrefabUtility.UnloadPrefabContents(root);}
        if(job.operation=="ui.create") {
            if(report.nodes.Count!=job.nodes.Length+1)throw new Exception("Saved node count differs");
            var scene=EditorSceneManager.OpenScene(job.scenePath,OpenSceneMode.Single);
            foreach(var go in scene.GetRootGameObjects()) {
                report.eventSystems+=go.GetComponentsInChildren<UnityEngine.EventSystems.EventSystem>(true).Length;
                report.canvasRaycasters+=go.GetComponentsInChildren<UnityEngine.UI.GraphicRaycaster>(true).Length;
            }
            if(report.eventSystems!=1||report.canvasRaycasters!=1)throw new Exception("Scene interaction prerequisites missing or duplicated");
        }
    }
    static void Build() {
        foreach(var scene in job.scenes)if(AssetDatabase.LoadAssetAtPath<SceneAsset>(scene)==null)throw new Exception("Build scene missing: "+scene);
        var directory=Path.Combine(job.stage,"player");Directory.CreateDirectory(directory);
        var path=Path.Combine(directory,job.productName+".app");
        if(Directory.Exists(path))throw new Exception("Build output exists");
        var result=BuildPipeline.BuildPlayer(new BuildPlayerOptions{scenes=job.scenes,locationPathName=path,target=BuildTarget.StandaloneOSX,options=BuildOptions.None});
        report.buildResult=result.summary.result.ToString();report.buildBytes=result.summary.totalSize;report.buildErrors=result.summary.totalErrors;report.buildPath=path;
        if(result.summary.result!=BuildResult.Succeeded)throw new Exception("Build failed: "+report.buildResult);
    }
    static void SpriteImport(bool write) {
        var importer=AssetImporter.GetAtPath(job.assetPath) as TextureImporter;
        if(importer==null)throw new Exception("Source image has no TextureImporter");
        if(write) {
            importer.textureType=TextureImporterType.Sprite;importer.spriteImportMode=SpriteImportMode.Single;
            importer.spritePixelsPerUnit=job.pixelsPerUnit;importer.spritePivot=new Vector2(job.pivot[0],job.pivot[1]);
            var settings=new TextureImporterSettings();importer.ReadTextureSettings(settings);settings.spriteAlignment=(int)SpriteAlignment.Custom;settings.spritePivot=importer.spritePivot;importer.SetTextureSettings(settings);
            importer.maxTextureSize=job.maxSize;importer.mipmapEnabled=false;importer.alphaIsTransparency=job.alphaIsTransparency;
            importer.textureCompression=TextureImporterCompression.Uncompressed;importer.SaveAndReimport();
        }
        var sprite=AssetDatabase.LoadAssetAtPath<Sprite>(job.assetPath);
        if(sprite==null||importer.spriteImportMode!=SpriteImportMode.Single||importer.textureType!=TextureImporterType.Sprite)throw new Exception("Sprite import readback differs");
        if(importer.spritePixelsPerUnit!=job.pixelsPerUnit||importer.spritePivot!=new Vector2(job.pivot[0],job.pivot[1])||importer.maxTextureSize!=job.maxSize||importer.alphaIsTransparency!=job.alphaIsTransparency)throw new Exception("Sprite importer parameters differ");
        if(!AssetDatabase.TryGetGUIDAndLocalFileIdentifier(sprite,out report.spriteGuid,out report.spriteLocalId))throw new Exception("Sprite local identity missing");
        report.spritePixelsPerUnit=sprite.pixelsPerUnit;report.spritePivot=sprite.pivot;report.spriteRect=sprite.rect;
    }
    static string[] Ids(string path) {
        return AssetDatabase.LoadAllAssetsAtPath(path).Where(a=>a!=null).Select(a=>{
            string guid;long id;if(!AssetDatabase.TryGetGUIDAndLocalFileIdentifier(a,out guid,out id))throw new Exception("Missing asset identity");
            return guid+":"+id+":"+a.GetType().FullName;
        }).OrderBy(s=>s,StringComparer.Ordinal).ToArray();
    }
    static void Moves(bool write) {
        foreach(var move in job.moves) {
            var row=new MoveRow{source=move.source,target=move.target};
            if(write) {
                row.guid=AssetDatabase.AssetPathToGUID(move.source);row.localIds=Ids(move.source);
                if(String.IsNullOrEmpty(row.guid)||File.Exists(Full(move.target))||File.Exists(Full(move.target+".meta")))throw new Exception("Invalid move endpoints");
                report.moves.Add(row);Save();Folder(move.target);
                var error=AssetDatabase.MoveAsset(move.source,move.target);if(error!="")throw new Exception(error);
                if(AssetDatabase.AssetPathToGUID(move.target)!=row.guid||!row.localIds.SequenceEqual(Ids(move.target)))throw new Exception("Move changed GUID/localID");
                Save();
            } else {
                row.guid=AssetDatabase.AssetPathToGUID(move.target);row.localIds=Ids(move.target);report.moves.Add(row);
                if(String.IsNullOrEmpty(row.guid)||File.Exists(Full(move.source)))throw new Exception("Move save/reopen mismatch");
            }
        }
        AssetDatabase.SaveAssets();
    }
    public static void Run() {
        int code=1;
        try {
            job=JsonUtility.FromJson<Job>(File.ReadAllText(JobPath()));
            report=new Report{status="running",operation=job.operation,mode=job.mode,planSha256=job.planSha256,sourceSha256=job.sourceSha256};Save();
            if(Application.unityVersion!="6000.6.2f1")throw new Exception("Unexpected Editor version");
            if(Path.GetFullPath(Directory.GetParent(Application.dataPath).FullName)!=Path.GetFullPath(job.targetRoot))throw new Exception("Wrong project");
            if(job.operation=="validate.play") {Play();return;}
            if(job.mode=="apply") {
                if(job.operation=="ui.create")CreateUI();
                else if(job.operation=="prefab.edit")EditPrefab();
                else if(job.operation=="build.macos")Build();
                else if(job.operation=="assets.move")Moves(true);
                else if(job.operation=="sprite.configure")SpriteImport(true);
                else if(job.operation=="project.import") {AssetDatabase.Refresh(ImportAssetOptions.ForceSynchronousImport);AssetDatabase.SaveAssets();}
                else if(job.operation!="prefab.inspect")throw new Exception("Unknown operation");
            } else if(job.mode!="verify")throw new Exception("Unknown native mode");
            if(job.operation=="assets.move") {if(job.mode=="verify")Moves(false);}
            else if(job.operation=="sprite.configure") {if(job.mode=="verify")SpriteImport(false);}
            else if(job.operation=="project.import") { }
            else if(job.operation!="build.macos")InspectPrefab();
            report.status="verified";Save();code=0;
        } catch(Exception error) {
            if(report!=null){report.status="failed";report.error=error.ToString();Save();}
            Debug.LogException(error);
        }
        EditorApplication.Exit(code);
    }
}
#endif

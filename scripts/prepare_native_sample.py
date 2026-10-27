"""Create-only, self-authored Unity acceptance fixture; never reads a game project."""
from pathlib import Path
import json
import sys

root=Path(sys.argv[1]).absolute()
if root.exists():
    raise SystemExit('Fixture root exists; choose a fresh path')
(root/'Assets/Bootstrap/Editor').mkdir(parents=True)
(root/'Assets/Game/Scripts').mkdir(parents=True)
(root/'ProjectSettings').mkdir()
(root/'Packages').mkdir()
(root/'ProjectSettings/ProjectVersion.txt').write_text('m_EditorVersion: 6000.6.2f1\n')
(root/'Packages/manifest.json').write_text(json.dumps({'dependencies':{'com.unity.ugui':'2.0.0','com.unity.nuget.newtonsoft-json':'3.2.2','com.unity.modules.imageconversion':'1.0.0','com.unity.modules.ui':'1.0.0','com.unity.modules.jsonserialize':'1.0.0','com.unity.modules.physics':'1.0.0','com.unity.modules.physics2d':'1.0.0','com.unity.modules.screencapture':'1.0.0'}},indent=2)+'\n')
(root/'Assets/Game/Scripts/BeaverReferenceProbe.cs').write_text('''using UnityEngine;
public sealed class BeaverReferenceProbe : MonoBehaviour { public TextAsset data; }
''')
(root/'Assets/Bootstrap/Editor/BeaverBootstrap.cs').write_text('''#if UNITY_EDITOR
using System;
using System.IO;
using UnityEngine;
using UnityEditor;
public static class BeaverBootstrap {
    public static void Run() {
        try {
            PlayerSettings.companyName="Beaver";PlayerSettings.productName="BeaverSample";
            PlayerSettings.defaultScreenWidth=960;PlayerSettings.defaultScreenHeight=640;
            PlayerSettings.defaultIsNativeResolution=false;
            PlayerSettings.fullScreenMode=FullScreenMode.Windowed;
            if(!AssetDatabase.IsValidFolder("Assets/Game/Content"))AssetDatabase.CreateFolder("Assets/Game","Content");
            if(!AssetDatabase.IsValidFolder("Assets/Game/Content/Data"))AssetDatabase.CreateFolder("Assets/Game/Content","Data");
            File.WriteAllText("Assets/Game/Content/Data/Original.txt","self-authored acceptance payload");
            AssetDatabase.ImportAsset("Assets/Game/Content/Data/Original.txt",ImportAssetOptions.ForceSynchronousImport);
            var go=new GameObject("ReferenceWatcher");var probe=go.AddComponent<BeaverReferenceProbe>();
            probe.data=AssetDatabase.LoadAssetAtPath<TextAsset>("Assets/Game/Content/Data/Original.txt");
            PrefabUtility.SaveAsPrefabAsset(go,"Assets/Game/Content/ReferenceWatcher.prefab");
            UnityEngine.Object.DestroyImmediate(go);AssetDatabase.SaveAssets();
            File.WriteAllText(Path.Combine(Directory.GetParent(Application.dataPath).FullName,"bootstrap-result.json"),"{\\\"status\\\":\\\"verified\\\"}");
            EditorApplication.Exit(0);
        } catch(Exception e){Debug.LogException(e);EditorApplication.Exit(1);}
    }
}
#endif
''')
print(root)

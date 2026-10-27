#if UNITY_EDITOR
using System;
using System.Linq;
using System.Reflection;
using UnityEditor;
using UnityEditor.Events;
using UnityEngine;
using UnityEngine.UI;

// Runs inside the existing batch transaction. Never edits YAML or unpacks Prefabs.
public static class BeaverWiring {
    static Transform Node(GameObject root,string path) {
        var t=String.IsNullOrEmpty(path)?root.transform:root.transform.Find(path);
        if(t==null)throw new Exception("WIRING_NODE_MISSING: "+path);return t;
    }
    static Component ComponentAt(GameObject root,string path,string name) {
        var values=Node(root,path).GetComponents<Component>().Where(c=>c!=null&&c.GetType().FullName==name).ToArray();
        if(values.Length!=1)throw new Exception("WIRING_COMPONENT_AMBIGUOUS: "+path+":"+name);return values[0];
    }
    static string Key(GameObject root,UnityEngine.Object value) {
        if(value==null)return "null";
        var c=value as Component;var go=c==null?value as GameObject:c.gameObject;
        if(go!=null&&(go==root||go.transform.IsChildOf(root.transform)))
            return "node:"+(go==root?"":AnimationUtility.CalculateTransformPath(go.transform,root.transform))+":"+(c==null?"GameObject":c.GetType().FullName);
        string guid;long id;if(AssetDatabase.TryGetGUIDAndLocalFileIdentifier(value,out guid,out id))return "asset:"+guid+":"+id;
        throw new Exception("WIRING_IDENTITY_UNKNOWN");
    }
    static UnityEngine.Object Target(GameObject root,BeaverBatch.Action a) {
        if(String.IsNullOrEmpty(a.assetGuid))return String.IsNullOrEmpty(a.targetComponent)?(UnityEngine.Object)Node(root,a.targetNode).gameObject:ComponentAt(root,a.targetNode,a.targetComponent);
        var path=AssetDatabase.GUIDToAssetPath(a.assetGuid);
        if(String.IsNullOrEmpty(path))throw new Exception("WIRING_ASSET_MISSING: "+a.assetGuid);
        var found=AssetDatabase.LoadAllAssetsAtPath(path).Where(o=>{string g;long id;return o!=null&&AssetDatabase.TryGetGUIDAndLocalFileIdentifier(o,out g,out id)&&g==a.assetGuid&&id==a.localId;}).ToArray();
        if(found.Length!=1)throw new Exception("WIRING_SUBASSET_AMBIGUOUS: "+a.assetGuid+":"+a.localId);return found[0];
    }
    static FieldInfo Field(Type type,string name) {
        for(var t=type;t!=null;t=t.BaseType) {
            var field=t.GetField(name,BindingFlags.Instance|BindingFlags.Public|BindingFlags.NonPublic|BindingFlags.DeclaredOnly);
            if(field!=null)return field;
        }
        throw new Exception("WIRING_FIELD_MISSING: "+name);
    }
    static bool MissingReference(SerializedProperty p) {
        var field=typeof(SerializedProperty).GetProperty("objectReferenceEntityIdValue")??typeof(SerializedProperty).GetProperty("objectReferenceInstanceIDValue");
        if(field==null)throw new Exception("WIRING_REFERENCE_ID_API_UNAVAILABLE");
        var value=field.GetValue(p,null);
        return value!=null&&!value.Equals(Activator.CreateInstance(field.PropertyType));
    }
    public static void Apply(GameObject root,BeaverBatch.Action a) {
        if(a.kind=="binding.event") {
            var button=ComponentAt(root,a.path,"UnityEngine.UI.Button") as Button;
            if(button==null||a.component!="UnityEngine.UI.Button"||a.property!="onClick"||a.value!="empty")throw new Exception("WIRING_EVENT_CONTRACT");
            if(button.onClick.GetPersistentEventCount()!=0)throw new Exception("WIRING_EVENT_NONEMPTY: existing persistent listeners protected");
            var target=Target(root,a) as MonoBehaviour;
            if(target==null)throw new Exception("WIRING_EVENT_TARGET: existing MonoBehaviour required");
            var methods=target.GetType().GetMethods(BindingFlags.Public|BindingFlags.Instance).Where(m=>m.Name==a.method&&!m.IsGenericMethod&&m.ReturnType==typeof(void)&&m.GetParameters().Length==0).ToArray();
            if(methods.Length!=1)throw new Exception("WIRING_EVENT_METHOD: exact public instance void() required");
            var action=(UnityEngine.Events.UnityAction)Delegate.CreateDelegate(typeof(UnityEngine.Events.UnityAction),target,methods[0]);
            UnityEventTools.AddPersistentListener(button.onClick,action);
            if(button.onClick.GetPersistentEventCount()!=1||button.onClick.GetPersistentTarget(0)!=target||button.onClick.GetPersistentMethodName(0)!=a.method)throw new Exception("WIRING_EVENT_READBACK");
            EditorUtility.SetDirty(button);return;
        }
        if(a.kind!="binding.reference"||String.IsNullOrEmpty(a.property)||a.property.Contains(".")||a.property=="m_Script")throw new Exception("WIRING_FIELD_CONTRACT: direct serialized ObjectReference only");
        var owner=ComponentAt(root,a.path,a.component);var field=Field(owner.GetType(),a.property);
        if(!typeof(UnityEngine.Object).IsAssignableFrom(field.FieldType)||field.IsInitOnly)throw new Exception("WIRING_FIELD_UNSUPPORTED");
        var so=new SerializedObject(owner);so.Update();var p=so.FindProperty(a.property);
        if(p==null||p.propertyType!=SerializedPropertyType.ObjectReference)throw new Exception("WIRING_NOT_SERIALIZED_REFERENCE");
        if(p.objectReferenceValue==null&&MissingReference(p))throw new Exception("WIRING_MISSING_REFERENCE: resolve broken identity explicitly");
        var before=Key(root,p.objectReferenceValue);
        if(before!=a.value)throw new Exception("WIRING_PRECONDITION: "+a.property);
        if(p.objectReferenceValue!=null&&!a.flag)throw new Exception("WIRING_NONEMPTY: existing binding protected");
        var value=Target(root,a);
        if(!field.FieldType.IsInstanceOfType(value))throw new Exception("WIRING_TYPE_MISMATCH: "+field.FieldType.FullName);
        p.objectReferenceValue=value;so.ApplyModifiedPropertiesWithoutUndo();so.Update();
        if(so.FindProperty(a.property).objectReferenceValue!=value)throw new Exception("WIRING_FIELD_READBACK");
        EditorUtility.SetDirty(owner);
    }
}
#endif

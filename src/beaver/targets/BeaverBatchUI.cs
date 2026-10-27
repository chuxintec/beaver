#if UNITY_EDITOR
using System;
using System.Globalization;
using System.Linq;
using System.Reflection;
using UnityEditor;
using UnityEditor.Events;
using UnityEngine;
using UnityEngine.Events;
using UnityEngine.UI;
using TMPro;

public static class BeaverBatchUI {
    public static bool Handles(string kind) {
        return kind=="ui.rect" || kind=="ui.scroll" || kind=="ui.grid" || kind=="ui.mask" || kind=="ui.text" || kind=="ui.event" || kind=="ui.list" || kind=="ui.safeArea";
    }

    static Transform Find(GameObject root,string path) {
        if(root==null)throw new ArgumentNullException("root");
        var node=String.IsNullOrEmpty(path)?root.transform:root.transform.Find(path);
        if(node==null)throw new InvalidOperationException("UI_NODE_NOT_FOUND: "+path);
        return node;
    }
    static Transform Target(GameObject root,BeaverBatch.Action a) { return Find(root,a.targetNode); }
    static void Require(bool condition,string message) { if(!condition)throw new InvalidOperationException("UI_INVALID_ACTION: "+message); }
    static float[] Numbers(BeaverBatch.Action a,int length) {
        Require(a.numbers!=null&&a.numbers.Length==length,"numbers must contain exactly "+length+" values");
        foreach(var n in a.numbers)Require(!Single.IsNaN(n)&&!Single.IsInfinity(n),"numbers must be finite");
        return a.numbers;
    }
    static void ApplyRect(GameObject root,BeaverBatch.Action a) {
        var n=Numbers(a,10);var rect=Find(root,a.path) as RectTransform;
        Require(rect!=null,"ui.rect target needs RectTransform");
        rect.anchorMin=new Vector2(n[0],n[1]);rect.anchorMax=new Vector2(n[2],n[3]);rect.pivot=new Vector2(n[4],n[5]);
        rect.anchoredPosition=new Vector2(n[6],n[7]);rect.sizeDelta=new Vector2(n[8],n[9]);
    }
    static void ApplyScroll(GameObject root,BeaverBatch.Action a) {
        Require(a.mode=="vertical"||a.mode=="horizontal","mode must be vertical or horizontal");
        var scrollNode=Find(root,a.path);var viewport=Target(root,a);var content=Find(root,a.parent);
        Require(content!=scrollNode&&viewport!=content,"content must be distinct from viewport and scroll host");
        Require((viewport==scrollNode||viewport.IsChildOf(scrollNode))&&content.IsChildOf(viewport),"viewport must be scroll host or its descendant; content must descend from viewport");
        var rect=scrollNode.GetComponent<ScrollRect>()??scrollNode.gameObject.AddComponent<ScrollRect>();
        if(viewport.GetComponent<RectMask2D>()==null)viewport.gameObject.AddComponent<RectMask2D>();
        rect.viewport=viewport as RectTransform;rect.content=content as RectTransform;
        Require(rect.viewport!=null&&rect.content!=null,"viewport and content need RectTransform");
        rect.horizontal=a.mode=="horizontal";rect.vertical=a.mode=="vertical";
    }
    static void ApplyGrid(GameObject root,BeaverBatch.Action a) {
        var n=Numbers(a,4);Require(a.index>0,"index must be a positive column count");
        var grid=Find(root,a.path).GetComponent<GridLayoutGroup>()??Find(root,a.path).gameObject.AddComponent<GridLayoutGroup>();
        grid.cellSize=new Vector2(n[0],n[1]);grid.spacing=new Vector2(n[2],n[3]);
        grid.constraint=GridLayoutGroup.Constraint.FixedColumnCount;grid.constraintCount=a.index;
    }
    static void ApplyMask(GameObject root,BeaverBatch.Action a) {
        var node=Find(root,a.path);
        if(node.GetComponent<RectMask2D>()==null)node.gameObject.AddComponent<RectMask2D>();
    }
    static void ApplyText(GameObject root,BeaverBatch.Action a) {
        var text=Find(root,a.path).GetComponent<TMP_Text>();Require(text!=null,"ui.text target needs TMP_Text");
        Require(!String.IsNullOrEmpty(a.mode),"text mode is required");
        if(a.mode=="overflow")text.overflowMode=TextOverflowModes.Overflow;
        else if(a.mode=="truncate")text.overflowMode=TextOverflowModes.Truncate;
        else if(a.mode=="ellipsis")text.overflowMode=TextOverflowModes.Ellipsis;
        else if(a.mode=="masking")text.overflowMode=TextOverflowModes.Masking;
        else if(a.mode=="scrollRect")text.overflowMode=TextOverflowModes.ScrollRect;
        else throw new InvalidOperationException("UI_INVALID_ACTION: unsupported TMP overflow mode "+a.mode);
        if(a.property=="wrap")text.textWrappingMode=a.flag?TextWrappingModes.Normal:TextWrappingModes.NoWrap;
        else Require(String.IsNullOrEmpty(a.property),"property must be empty or wrap");
    }
    static void ApplyEvent(GameObject root,BeaverBatch.Action a) {
        Require(!String.IsNullOrEmpty(a.property)&&!String.IsNullOrEmpty(a.method),"property (UnityEvent field) and method are required");
        var source=Find(root,a.path).GetComponents<Component>().Where(c=>c!=null).ToArray();
        var fields=source.SelectMany(c=>FieldsIncludingBase(c.GetType())
            .Where(f=>f.Name==a.property).Select(f=>new { component=c,field=f })).ToArray();
        Require(fields.Length==1,"event field must resolve uniquely on the source node");
        var field=fields[0].field;Require(typeof(UnityEvent).IsAssignableFrom(field.FieldType),"event field must be a zero-argument UnityEvent");
        var unityEvent=field.GetValue(fields[0].component) as UnityEvent;Require(unityEvent!=null,"UnityEvent field is null");
        var target=Target(root,a).GetComponents<MonoBehaviour>().Where(c=>c!=null&&
            (String.IsNullOrEmpty(a.targetComponent)||c.GetType().FullName==a.targetComponent)).ToArray();
        var methods=target.SelectMany(c=>c.GetType().GetMethods(BindingFlags.Instance|BindingFlags.Public)
            .Where(m=>m.Name==a.method&&m.ReturnType==typeof(void)&&m.GetParameters().Length==0)
            .Select(m=>new { component=c,method=m })).ToArray();
        Require(methods.Length==1,"target must have exactly one public zero-argument void method with this name");
        Require(!Enumerable.Range(0,unityEvent.GetPersistentEventCount()).Any(i=>unityEvent.GetPersistentTarget(i)==methods[0].component&&unityEvent.GetPersistentMethodName(i)==a.method),"duplicate persistent binding");
        UnityEventTools.AddPersistentListener(unityEvent,(UnityAction)Delegate.CreateDelegate(typeof(UnityAction),methods[0].component,methods[0].method));
        EditorUtility.SetDirty(fields[0].component);
    }
    static System.Collections.Generic.IEnumerable<FieldInfo> FieldsIncludingBase(Type type) {
        for(var current=type;current!=null;current=current.BaseType)
            foreach(var field in current.GetFields(BindingFlags.Instance|BindingFlags.Public|BindingFlags.NonPublic|BindingFlags.DeclaredOnly))yield return field;
    }
    static void ApplyList(GameObject root,BeaverBatch.Action a) {
        Require(!String.IsNullOrEmpty(a.parent)&&!String.IsNullOrEmpty(a.targetNode)&&!String.IsNullOrEmpty(a.property),"parent content, targetNode template, and property text-child path are required");
        Require(a.items!=null,"items is required (an empty array clears generated items)");
        var node=Find(root,a.path);var content=Find(root,a.parent);var template=Find(root,a.targetNode);
        Require(content!=template&&content.IsChildOf(node)&&template.IsChildOf(node),"content and template must be existing descendants of the list node");
        Require(!template.gameObject.activeSelf,"item template must be explicitly inactive so it does not participate in layout");
        var text=template.Find(a.property);Require(text!=null&&text.GetComponent<TMP_Text>()!=null,"property must resolve to a child with TMP_Text");
        var type=RuntimeType("Beaver.Tools.BeaverListView");var list=node.GetComponent(type)??node.gameObject.AddComponent(type);
        var so=new SerializedObject(list);
        SetObject(so,"content",content as RectTransform);SetObject(so,"itemTemplate",template.gameObject);
        SetString(so,"textPath",a.property);var items=so.FindProperty("items");Require(items!=null&&items.isArray,"installed BeaverListView has incompatible items field");
        items.arraySize=a.items.Length;for(int i=0;i<a.items.Length;i++)items.GetArrayElementAtIndex(i).stringValue=a.items[i]??"";
        so.ApplyModifiedPropertiesWithoutUndo();EditorUtility.SetDirty(list);
    }
    static void ApplySafeArea(GameObject root,BeaverBatch.Action a) {
        var node=Find(root,a.path);var rect=node as RectTransform;Require(rect!=null,"safe area target needs RectTransform");
        var type=RuntimeType("Beaver.Tools.BeaverSafeArea");var safe=node.GetComponent(type);if(safe==null)node.gameObject.AddComponent(type);
    }
    static Type RuntimeType(string fullName) {
        var type=AppDomain.CurrentDomain.GetAssemblies().Select(x=>x.GetType(fullName,false)).FirstOrDefault(x=>x!=null);
        Require(type!=null,"runtime component must be explicitly installed: "+fullName);return type;
    }
    static void SetObject(SerializedObject so,string name,UnityEngine.Object value) { var p=so.FindProperty(name);Require(p!=null&&p.propertyType==SerializedPropertyType.ObjectReference,"runtime field missing/incompatible: "+name);p.objectReferenceValue=value; }
    static void SetString(SerializedObject so,string name,string value) { var p=so.FindProperty(name);Require(p!=null&&p.propertyType==SerializedPropertyType.String,"runtime field missing/incompatible: "+name);p.stringValue=value; }
    public static void Apply(GameObject root,BeaverBatch.Action a) {
        Require(root!=null&&a!=null,"root and action are required");Require(Handles(a.kind),"unsupported kind");
        switch(a.kind) {
            case "ui.rect":ApplyRect(root,a);break;case "ui.scroll":ApplyScroll(root,a);break;case "ui.grid":ApplyGrid(root,a);break;
            case "ui.mask":ApplyMask(root,a);break;case "ui.text":ApplyText(root,a);break;case "ui.event":ApplyEvent(root,a);break;
            case "ui.list":ApplyList(root,a);break;case "ui.safeArea":ApplySafeArea(root,a);break;
        }
    }
    public static string[] Diagnostics(GameObject root) {
        if(root==null)return new[]{"UI_ROOT_MISSING"};
        var results=new System.Collections.Generic.List<string>();
        foreach(var t in root.GetComponentsInChildren<Transform>(true)) {
            var path=PathOf(t,root.transform);var rect=t as RectTransform;
            var image=t.GetComponent<Image>();
            if(image!=null&&image.sprite==null&&image.overrideSprite==null)results.Add("UI_IMAGE_SPRITE_UNASSIGNED:"+path);
            foreach(var component in t.GetComponents<Component>()) {
                if(component==null){results.Add("UI_COMPONENT_MISSING:"+path);continue;}
                var serialized=new SerializedObject(component);var field=serialized.GetIterator();
                while(field.NextVisible(true))if(field.propertyType==SerializedPropertyType.ObjectReference&&field.objectReferenceValue==null&&!EmptyId(field.objectReferenceEntityIdValue))
                    results.Add("UI_OBJECT_REFERENCE_MISSING:"+path+":"+component.GetType().FullName+":"+field.propertyPath);
            }
            if(rect!=null&&(rect.rect.width<=0||rect.rect.height<=0))results.Add("UI_ZERO_SIZE:"+path);
            var button=t.GetComponent<Button>();if(button!=null&&(!button.interactable||!button.gameObject.activeInHierarchy))results.Add("UI_BUTTON_INACTIVE:"+path);
            if(button!=null&&(button.targetGraphic==null||!button.targetGraphic.raycastTarget))results.Add("UI_BUTTON_RAYCAST_RISK:"+path);
            var scroll=t.GetComponent<ScrollRect>();if(scroll!=null) {
                if(scroll.viewport==null||scroll.content==null)results.Add("UI_SCROLL_REFERENCE_MISSING:"+path);
                else {
                    if(scroll.viewport.GetComponent<RectMask2D>()==null&&scroll.viewport.GetComponent<Mask>()==null)results.Add("UI_SCROLL_MASK_MISSING:"+path);
                    var surface=scroll.viewport.GetComponent<Graphic>();if(surface==null||!surface.raycastTarget)results.Add("UI_SCROLL_RAYCAST_RISK:"+path);
                    if(t.parent!=null)foreach(Transform sibling in t.parent) {
                        var graphic=sibling.GetComponent<Image>();var siblingRect=sibling as RectTransform;
                        if(sibling==t||sibling.GetSiblingIndex()<t.GetSiblingIndex()||!sibling.gameObject.activeSelf||graphic==null||graphic.color.a<.98f||siblingRect==null)continue;
                        var viewportBounds=RectBounds(root.transform,scroll.viewport);var cover=RectBounds(root.transform,siblingRect);
                        var overlapX=Mathf.Max(0,Mathf.Min(viewportBounds.xMax,cover.xMax)-Mathf.Max(viewportBounds.xMin,cover.xMin));
                        var overlapY=Mathf.Max(0,Mathf.Min(viewportBounds.yMax,cover.yMax)-Mathf.Max(viewportBounds.yMin,cover.yMin));
                        if(viewportBounds.width>0&&viewportBounds.height>0&&overlapX*overlapY/(viewportBounds.width*viewportBounds.height)>.2f)results.Add("UI_SCROLL_SIBLING_OCCLUSION_RISK:"+path+":"+PathOf(sibling,root.transform));
                    }
                }
            }
            var text=t.GetComponent<TMP_Text>();if(text!=null) {
                if(text.font==null)results.Add("UI_TMP_FONT_MISSING:"+path);
                if(text.overflowMode==TextOverflowModes.Overflow&&rect!=null&&rect.rect.width>0&&text.preferredWidth>rect.rect.width)results.Add("UI_TEXT_POTENTIAL_OVERFLOW:"+path);
            }
        }
        return results.ToArray();
    }
    static bool EmptyId<T>(T value) {return System.Collections.Generic.EqualityComparer<T>.Default.Equals(value,default(T));}
    static Rect RectBounds(Transform root,RectTransform rect) {
        var corners=new Vector3[4];rect.GetWorldCorners(corners);var min=new Vector2(float.PositiveInfinity,float.PositiveInfinity);var max=new Vector2(float.NegativeInfinity,float.NegativeInfinity);
        foreach(var point in corners){var local=root.InverseTransformPoint(point);min=Vector2.Min(min,new Vector2(local.x,local.y));max=Vector2.Max(max,new Vector2(local.x,local.y));}
        return Rect.MinMaxRect(min.x,min.y,max.x,max.y);
    }
    static string PathOf(Transform t,Transform root) { if(t==root)return "";var parts=new System.Collections.Generic.List<string>();while(t!=root){parts.Insert(0,t.name);t=t.parent;}return String.Join("/",parts); }
}
#endif

using UnityEngine;

namespace Beaver.Tools {
    /// <summary>Fits this RectTransform to Screen.safeArea; contains no product behavior.</summary>
    [RequireComponent(typeof(RectTransform))]
    public sealed class BeaverSafeArea : MonoBehaviour {
        Rect lastArea;
        Vector2 lastScreen;
        RectTransform rect;
        void Awake() { rect=GetComponent<RectTransform>();RefreshNow(); }
        void Update() {
            var area=Screen.safeArea;var size=new Vector2(Screen.width,Screen.height);
            if(area!=lastArea||size!=lastScreen)RefreshNow();
        }
        public void RefreshNow() {
            if(rect==null)rect=GetComponent<RectTransform>();
            if(rect==null||Screen.width<=0||Screen.height<=0)return;
            var area=Screen.safeArea;
            var parent=rect.parent as RectTransform;
            if(parent==null||parent.rect.width<=0||parent.rect.height<=0)return;
            var canvas=GetComponentInParent<Canvas>();
            var camera=canvas!=null&&canvas.rootCanvas.renderMode!=RenderMode.ScreenSpaceOverlay?canvas.rootCanvas.worldCamera:null;
            Vector2 bottomLeft,topLeft,topRight,bottomRight;
            if(!RectTransformUtility.ScreenPointToLocalPointInRectangle(parent,new Vector2(area.xMin,area.yMin),camera,out bottomLeft)||
               !RectTransformUtility.ScreenPointToLocalPointInRectangle(parent,new Vector2(area.xMin,area.yMax),camera,out topLeft)||
               !RectTransformUtility.ScreenPointToLocalPointInRectangle(parent,new Vector2(area.xMax,area.yMax),camera,out topRight)||
               !RectTransformUtility.ScreenPointToLocalPointInRectangle(parent,new Vector2(area.xMax,area.yMin),camera,out bottomRight))return;
            var min=Vector2.Min(Vector2.Min(bottomLeft,topLeft),Vector2.Min(topRight,bottomRight));
            var max=Vector2.Max(Vector2.Max(bottomLeft,topLeft),Vector2.Max(topRight,bottomRight));
            var parentRect=parent.rect;
            var anchorMin=new Vector2((min.x-parentRect.xMin)/parentRect.width,(min.y-parentRect.yMin)/parentRect.height);
            var anchorMax=new Vector2((max.x-parentRect.xMin)/parentRect.width,(max.y-parentRect.yMin)/parentRect.height);
            rect.anchorMin=anchorMin;rect.anchorMax=anchorMax;
            rect.offsetMin=Vector2.zero;rect.offsetMax=Vector2.zero;
            lastArea=area;lastScreen=new Vector2(Screen.width,Screen.height);
        }
    }
}

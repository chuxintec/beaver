using System.Collections.Generic;
using TMPro;
using UnityEngine;

namespace Beaver.Tools {
    /// <summary>Creates text rows from an authored template and owns only rows in generatedItems.</summary>
    public sealed class BeaverListView : MonoBehaviour {
        public RectTransform content;
        public GameObject itemTemplate;
        public string textPath;
        public string[] items = new string[0];
        [SerializeField] List<GameObject> generatedItems = new List<GameObject>();
        public int OwnedCount { get { return generatedItems==null?0:generatedItems.Count; } }

        void OnEnable() { Refresh(); }
        public void Refresh() {
            if(content==null||itemTemplate==null||string.IsNullOrEmpty(textPath))return;
            if(generatedItems==null)generatedItems=new List<GameObject>();
            foreach(var item in generatedItems)if(item!=null) { item.SetActive(false);Destroy(item); }
            generatedItems.Clear();
            if(items==null)return;
            foreach(var value in items) {
                var instance=Instantiate(itemTemplate,content,false);instance.name=itemTemplate.name+"_Generated";
                var text=instance.transform.Find(textPath);
                var component=text!=null?text.GetComponent<TMP_Text>():null;
                if(component==null) { Destroy(instance);continue; }
                component.text=value??"";generatedItems.Add(instance);instance.SetActive(true);
            }
        }
    }
}

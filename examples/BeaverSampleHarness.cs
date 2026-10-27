// Self-authored acceptance behavior. No recovered game rules, assets or accounts.
using System;
using System.Collections;
using System.IO;
using UnityEngine;
using UnityEngine.UI;
using UnityEngine.EventSystems;
using TMPro;

public sealed class BeaverSampleHarness : MonoBehaviour {
    [Serializable] public class Result {
        public string status,error;
        public int jsonRows,price,counter,screenWidth,screenHeight;
        public bool rejectedMissingField,rejectedWrongType,syntheticUIEvent,positiveLayout;
        public string screenshot;
    }
    [RuntimeInitializeOnLoadMethod(RuntimeInitializeLoadType.AfterSceneLoad)]
    static void Attach() {Application.runInBackground=true;Debug.Log("BEAVER_SAMPLE_ATTACHED");new GameObject("BeaverSampleHarness").AddComponent<BeaverSampleHarness>();}
    IEnumerator Start() {
        var report=new Result{status="running"};
        var output=Environment.GetEnvironmentVariable("BEAVER_SAMPLE_RESULT");
        var shot=Environment.GetEnvironmentVariable("BEAVER_SAMPLE_SCREENSHOT");
        bool auto=Environment.GetEnvironmentVariable("BEAVER_SAMPLE_AUTO")=="1";
        yield return null;
        try {
            var rows=BeaverItemJson.Load(Resources.Load<TextAsset>("BeaverItems"));
            report.jsonRows=rows.Length;report.price=rows[0].price;
            if(rows.Length!=1||rows[0].id!="sample"||rows[0].price!=7)throw new Exception("Runtime JSON read differs");
            try {BeaverItemJson.Load(new TextAsset("{\"rows\":[{\"id\":\"sample\",\"enabled\":true}]}"));}
            catch(FormatException){report.rejectedMissingField=true;}
            try {BeaverItemJson.Load(new TextAsset("{\"rows\":[{\"id\":\"sample\",\"price\":\"7\",\"enabled\":true}]}"));}
            catch(FormatException){report.rejectedWrongType=true;}
            if(!report.rejectedMissingField||!report.rejectedWrongType)throw new Exception("Bad runtime config was accepted");
            var button=GameObject.Find("Action").GetComponent<Button>();
            var label=GameObject.Find("Counter").GetComponent<TMP_Text>();
            button.onClick.AddListener(()=>{report.counter++;label.text="Count: "+report.counter;Debug.Log("BEAVER_COUNTER="+report.counter);});
            Canvas.ForceUpdateCanvases();
            report.positiveLayout=button.GetComponent<RectTransform>().rect.width>0&&button.GetComponent<RectTransform>().rect.height>0&&label.GetComponent<RectTransform>().rect.width>0;
            if(!report.positiveLayout||EventSystem.current==null||!button.interactable)throw new Exception("Runtime UI prerequisites missing");
            if(auto) {
                ExecuteEvents.Execute(button.gameObject,new PointerEventData(EventSystem.current){button=PointerEventData.InputButton.Left},ExecuteEvents.pointerClickHandler);
                report.syntheticUIEvent=true;
                if(report.counter!=1)throw new Exception("Synthetic UI click did not reach the real listener");
            }
            report.screenWidth=Screen.width;report.screenHeight=Screen.height;report.status="verified";
        } catch(Exception e){report.status="failed";report.error=e.ToString();Debug.LogException(e);}
        if(!String.IsNullOrEmpty(shot)) {
            yield return new WaitForEndOfFrame();
            var capture=Type.GetType("UnityEngine.ScreenCapture, UnityEngine.ScreenCaptureModule");
            if(capture==null)throw new Exception("ScreenCapture built-in module is unavailable");
            capture.GetMethod("CaptureScreenshot",new[]{typeof(string),typeof(int)}).Invoke(null,new object[]{shot,1});report.screenshot=shot;
            yield return new WaitForSecondsRealtime(1);
        }
        if(!String.IsNullOrEmpty(output))File.WriteAllText(output,JsonUtility.ToJson(report,true));
        Debug.Log("BEAVER_SAMPLE="+JsonUtility.ToJson(report));
        if(auto)Application.Quit(report.status=="verified"?0:1);
    }
}

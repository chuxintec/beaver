using System;
using System.Text;
using System.Threading.Tasks;
using UnityEngine.Networking;
// Services adapter only: persist pending identity in Gameplay before invoking.
public sealed class UnityGateway {
    private readonly string root, session;
    public UnityGateway(string root, string session) { this.root=root; this.session=session; }
    public async Task<string> Command(string immutableJson) {
        using(var request=new UnityWebRequest(root+"/v1/command","POST")) {
            request.uploadHandler=new UploadHandlerRaw(Encoding.UTF8.GetBytes(immutableJson));
            request.downloadHandler=new DownloadHandlerBuffer(); request.timeout=5;
            request.SetRequestHeader("Content-Type","application/json"); request.SetRequestHeader("Authorization","Bearer "+session);
            var operation=request.SendWebRequest(); while(!operation.isDone) await Task.Yield();
            if(request.result!=UnityWebRequest.Result.Success) throw new Exception("Result may be unknown; retain same operation ID and content");
            return request.downloadHandler.text; // Parse business code; transport success is not a grant.
        }
    }
}

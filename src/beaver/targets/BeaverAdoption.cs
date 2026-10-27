// Derived from monkey, MIT license retained in licenses/MONKEY-MIT.txt.
// MIT License. Native asset moves/imports for monkey's reviewed adoption plans.
#if UNITY_EDITOR
using System;
using System.IO;
using System.Linq;
using System.Collections.Generic;
using System.Security.Cryptography;
using UnityEditor;
using UnityEngine;

public static class BeaverAdoption {
    static string JobPath() {
        var encoded=Environment.GetEnvironmentVariable("BEAVER_ADOPT_JOB_BASE64");
        if(String.IsNullOrEmpty(encoded)||encoded.Length>65536)throw new Exception("Explicit ASCII-encoded native job required");
        return new System.Text.UTF8Encoding(false,true).GetString(Convert.FromBase64String(encoded));
    }

    [Serializable] public class Op {
        public string action, source, target, guid;
        public string beforeFileHash, beforeMetaHash, afterFileHash, afterMetaHash;
        public bool preserveLocalIds;
    }
    [Serializable] public class Job {
        public string targetRoot, stage, mode, deadlineUtc, planSha256;
        public int budgetSeconds;
        public Op[] operations;
        public string[] watch;
    }
    [Serializable] public class Entry {
        public int index;
        public string path, guid;
        public string[] localIds;
    }
    [Serializable] public class Progress {
        public string status, mode, planSha256, error;
        public List<int> written=new List<int>();
        public List<int> restored=new List<int>();
        public List<Entry> assets=new List<Entry>();
        public List<Entry> beforeAssets=new List<Entry>();
        public List<Entry> createdFolders=new List<Entry>();
        public List<string> retainedFolders=new List<string>();
        public bool referencesPreserved;
        public List<Observed> observedAfter=new List<Observed>();
    }
    [Serializable] public class Observed { public int index; public string path,fileHash,metaHash; }
    static Job job;
    static Progress report;
    static DateTime deadline;
    static string root;
    static readonly Dictionary<string,string[]> originalIds=new Dictionary<string,string[]>();
    static void Checkpoint() { if(DateTime.UtcNow>=deadline) throw new Exception("ADOPT_DEADLINE"); }
    static string Full(string relative) {
        if(String.IsNullOrEmpty(relative) || !relative.StartsWith("Assets/",StringComparison.Ordinal) || relative.Contains("\\") || relative.Split('/').Any(p=>p==".."||p=="."||p=="")) throw new Exception("Unsafe asset path");
        var path=Path.GetFullPath(Path.Combine(root,relative));
        if(!path.StartsWith(root+Path.DirectorySeparatorChar,StringComparison.Ordinal)) throw new Exception("Path escaped project");
        return path;
    }
    static string Hash(string path) {
        if(!File.Exists(path))return null;
        using(var input=File.OpenRead(path))using(var sha=SHA256.Create())return BitConverter.ToString(sha.ComputeHash(input)).Replace("-","").ToLowerInvariant();
    }
    static void Expected(string relative,string hash) {
        Checkpoint();if(Hash(Full(relative))!=hash)throw new Exception("ADOPT_DRIFT "+relative);
    }
    static string FileFor(int index,string side,string kind) { return Path.Combine(job.stage,side,index.ToString(),kind); }
    static void Save() {
        var path=Path.Combine(job.stage,"native-"+job.mode+".json");
        File.WriteAllText(path+".tmp",JsonUtility.ToJson(report,true));
        if(File.Exists(path)) File.Replace(path+".tmp",path,null);
        else File.Move(path+".tmp",path);
    }
    static void Folder(string relative) {
        var parent=Path.GetDirectoryName(relative).Replace('\\','/');
        if(parent=="Assets" || AssetDatabase.IsValidFolder(parent))return;
        Folder(parent);
        var grand=Path.GetDirectoryName(parent).Replace('\\','/');
        var guid=AssetDatabase.CreateFolder(grand,Path.GetFileName(parent));
        if(String.IsNullOrEmpty(guid))throw new Exception("CreateFolder failed "+parent);
        report.createdFolders.Add(new Entry{index=-1,path=parent,guid=guid,localIds=new string[0]});Save();
    }
    static string[] Ids(string relative) {
        return AssetDatabase.LoadAllAssetsAtPath(relative).Where(a=>a!=null).Select(a=>{
            string guid;long id;
            if(!AssetDatabase.TryGetGUIDAndLocalFileIdentifier(a,out guid,out id))throw new Exception("No local identity "+relative);
            return guid+":"+id+":"+a.GetType().FullName;
        }).OrderBy(v=>v,StringComparer.Ordinal).ToArray();
    }
    static string RefSignature(string path) {
        return String.Join("|",AssetDatabase.GetDependencies(path,false).Select(p=>AssetDatabase.AssetPathToGUID(p)).Where(g=>g!="").OrderBy(g=>g,StringComparer.Ordinal));
    }
    static void WritePayload(int index,string side,string target,string fileHash,string metaHash) {
        var file=FileFor(index,side,"file");var meta=FileFor(index,side,"meta");
        if(Hash(file)!=fileHash || Hash(meta)!=metaHash)throw new Exception("Staged payload changed");
        Folder(target);File.Copy(file,Full(target),true);
        if(metaHash!=null)File.Copy(meta,Full(target+".meta"),true);
        AssetDatabase.ImportAsset(target,ImportAssetOptions.ForceSynchronousImport|ImportAssetOptions.ForceUpdate);
        if(side=="after") {
            report.observedAfter.RemoveAll(row=>row.index==index);
            report.observedAfter.Add(new Observed{index=index,path=target,fileHash=Hash(Full(target)),metaHash=Hash(Full(target+".meta"))});
            Save();
        }
        Expected(target,fileHash);
        if(metaHash!=null)Expected(target+".meta",metaHash);
    }
    static void VerifyAsset(int index,Op op) {
        Expected(op.target,op.afterFileHash);
        if(op.afterMetaHash!=null)Expected(op.target+".meta",op.afterMetaHash);
        var guid=AssetDatabase.AssetPathToGUID(op.target);
        if(op.guid!=null && guid!=op.guid)throw new Exception("GUID changed "+op.target);
        var ids=Ids(op.target);
        if(op.preserveLocalIds && originalIds.ContainsKey(op.target) && !originalIds[op.target].SequenceEqual(ids))throw new Exception("Local IDs changed "+op.target);
        report.assets.Add(new Entry{index=index,path=op.target,guid=guid,localIds=ids});
    }
    public static void Run() {
        int code=1;
        try {
            var jobPath=JobPath();
            if(Application.unityVersion!="6000.6.2f1")throw new Exception("Unexpected Unity version");
            job=JsonUtility.FromJson<Job>(File.ReadAllText(jobPath));root=Path.GetFullPath(job.targetRoot).TrimEnd(Path.DirectorySeparatorChar);
            if(root!=Path.GetFullPath(Directory.GetParent(Application.dataPath).FullName).TrimEnd(Path.DirectorySeparatorChar))throw new Exception("Wrong target project");
            foreach(var op in job.operations) {
                if(op.source=="")op.source=null;if(op.guid=="")op.guid=null;
                if(op.beforeFileHash=="")op.beforeFileHash=null;if(op.beforeMetaHash=="")op.beforeMetaHash=null;if(op.afterMetaHash=="")op.afterMetaHash=null;
            }
            deadline=DateTime.UtcNow.AddSeconds(job.budgetSeconds);
            var outer=DateTime.Parse(job.deadlineUtc,null,System.Globalization.DateTimeStyles.RoundtripKind).ToUniversalTime();if(outer<deadline)deadline=outer;
            report=new Progress{status="running",mode=job.mode,planSha256=job.planSha256};Save();
            if(job.mode=="rollback") {
                var previous=JsonUtility.FromJson<Progress>(File.ReadAllText(Path.Combine(job.stage,"native-apply.json")));
                foreach(var i in previous.written.AsEnumerable().Reverse()) {
                    Checkpoint();var op=job.operations[i];var at=File.Exists(Full(op.target))?op.target:op.source;
                    if(at==null || !File.Exists(Full(at))) { if(op.action=="add"){report.restored.Add(i);Save();continue;}throw new Exception("Rollback asset missing"); }
                    var fh=Hash(Full(at));var mh=Hash(Full(at+".meta"));
                    var observed=(previous.observedAfter ?? new List<Observed>()).FirstOrDefault(row=>row.index==i&&row.path==at);
                    bool ownedObserved=observed!=null&&fh==observed.fileHash&&mh==observed.metaHash;
                    if(!ownedObserved&&(fh!=op.afterFileHash && fh!=op.beforeFileHash || mh!=op.afterMetaHash && mh!=op.beforeMetaHash))throw new Exception("Rollback conflicts with later changes "+at);
                    if(op.action=="add") { if(!AssetDatabase.DeleteAsset(at))throw new Exception("Delete added asset failed"); }
                    else {
                        if(at!=op.source){Folder(op.source);var error=AssetDatabase.MoveAsset(at,op.source);if(error!="")throw new Exception(error);}
                        WritePayload(i,"before",op.source,op.beforeFileHash,op.beforeMetaHash);
                    }
                    report.restored.Add(i);Save();
                }
                foreach(var folder in (previous.createdFolders ?? new List<Entry>()).AsEnumerable().Reverse()) {
                    if(!Directory.Exists(Full(folder.path)))continue;
                    if(Directory.EnumerateFileSystemEntries(Full(folder.path)).Any()){report.retainedFolders.Add(folder.path);continue;}
                    if(AssetDatabase.AssetPathToGUID(folder.path)!=folder.guid)throw new Exception("Created folder identity changed");
                    if(!AssetDatabase.DeleteAsset(folder.path))throw new Exception("Delete empty owned folder failed");
                }
                foreach(var old in previous.beforeAssets ?? new List<Entry>()) {
                    if(AssetDatabase.AssetPathToGUID(old.path)!=old.guid || !Ids(old.path).SequenceEqual(old.localIds))throw new Exception("Restored local identities differ "+old.path);
                }
                var refs=Path.Combine(job.stage,"reference-signatures.json");
                if(File.Exists(refs))foreach(var line in File.ReadAllLines(refs)){var parts=line.Split('\t');if(parts.Length!=2||RefSignature(parts[0])!=parts[1])throw new Exception("Restored watcher dependencies differ");}
                report.status="rolled-back";report.referencesPreserved=true;
            } else {
                var signatures=new Dictionary<string,string>();
                if(job.mode=="apply") {
                    foreach(var watch in job.watch ?? new string[0])signatures[watch]=RefSignature(watch);
                    for(int i=0;i<job.operations.Length;i++) {
                        Checkpoint();var op=job.operations[i];
                        if(op.source!=null){Expected(op.source,op.beforeFileHash);Expected(op.source+".meta",op.beforeMetaHash);if(op.guid!=null && AssetDatabase.AssetPathToGUID(op.source)!=op.guid)throw new Exception("Source GUID differs");var ids=Ids(op.source);report.beforeAssets.Add(new Entry{index=i,path=op.source,guid=AssetDatabase.AssetPathToGUID(op.source),localIds=ids});if(op.preserveLocalIds)originalIds[op.target]=ids;}
                        else {Expected(op.target,null);Expected(op.target+".meta",null);}
                        report.written.Add(i);Save();
                        if(op.action=="move") {Folder(op.target);var error=AssetDatabase.MoveAsset(op.source,op.target);if(error!="")throw new Exception(error);}
                        WritePayload(i,"after",op.target,op.afterFileHash,op.afterMetaHash);VerifyAsset(i,op);Save();
                    }
                    foreach(var watch in signatures.Keys)if(RefSignature(watch)!=signatures[watch])throw new Exception("Unchanged watcher dependencies changed "+watch);
                    File.WriteAllText(Path.Combine(job.stage,"reference-signatures.json"),String.Join("\n",signatures.Select(p=>p.Key+"\t"+p.Value)));
                } else if(job.mode=="verify") {
                    for(int i=0;i<job.operations.Length;i++){Checkpoint();VerifyAsset(i,job.operations[i]);}
                    foreach(var line in File.ReadAllLines(Path.Combine(job.stage,"reference-signatures.json"))){var parts=line.Split('\t');if(parts.Length!=2||RefSignature(parts[0])!=parts[1])throw new Exception("Reopened dependencies changed");}
                    var prior=JsonUtility.FromJson<Progress>(File.ReadAllText(Path.Combine(job.stage,"native-apply.json")));
                    if(prior.assets.Count!=report.assets.Count)throw new Exception("Readback count differs");
                    for(int i=0;i<prior.assets.Count;i++)if(!prior.assets[i].localIds.SequenceEqual(report.assets[i].localIds))throw new Exception("Reopened local IDs differ");
                } else throw new Exception("Unknown adoption mode");
                report.status="verified";report.referencesPreserved=true;
            }
            Save();code=0;
        } catch(Exception error) {if(report!=null){report.status="failed";report.error=error.ToString();Save();}Debug.LogException(error);}
        EditorApplication.Exit(code);
    }
}
#endif

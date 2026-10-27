"""Read-only bounded aggregation of provider, Unity and UI diagnostic evidence."""
from __future__ import annotations

import hashlib
import json
import re

from .errors import fail
from .io import load, read, safe

MAX_INPUTS = 256
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_LINE = 4096
_SECRET = re.compile(r"(?i)(bearer\s+|(?:token|password|secret|api[_-]?key)=)[^\s\"']+")
_COMPILER_FIRST = re.compile(r"(?P<file>.+?\.(?:cs|go|py))\((?P<line>\d+),(?P<col>\d+)\)\s*:\s*(?P<severity>error|warning)\s+(?P<code>[A-Z]+\d+)\s*:\s*(?P<message>.*)", re.I)
_COMPILER = re.compile(r"\b(?P<severity>error|warning)\s+(?P<code>[A-Z]+\d+)\s*:\s*(?P<message>.*)", re.I)
_STACK = re.compile(r"\bin\s+(?P<file>.+?\.(?:cs|go|py))\s*:\s*line\s+(?P<line>\d+)", re.I)
_EXIT = re.compile(r"(?i)(?:return code|exit code|exit_code)\s*[:=]?\s*(?P<code>-?\d+)")
_ENV_LICENSE = re.compile(r"(?i)(readonly database|read-only database|entitlement.*(?:not found|0 free entitlements|unavailable)|(?:editor\.)?headless.*(?:not found|unavailable)|access token is unavailable)")
_STAGE_PATTERNS = (("import", re.compile(r"(?i)(failed to import|import failed|could not import|asset import failed)")),
                   ("attach", re.compile(r"(?i)(attach(?:ed)? failed|failed to attach|editor connection failed|could not connect to editor)")),
                   ("compile", re.compile(r"(?i)(compilation failed|compile failed|script compilation failed)")),
                   ("play", re.compile(r"(?i)(play mode failed|play failed|exception during play)")),
                   ("player", re.compile(r"(?i)(player process timed out|player timeout|player exited with|player launch failed)")))


def _redact(value):
    if isinstance(value, str): return _SECRET.sub(lambda m: m.group(1) + "[REDACTED]", value)[:MAX_LINE]
    if isinstance(value, list): return [_redact(x) for x in value[:1000]]
    if isinstance(value, dict):
        return {str(k): ("[REDACTED]" if re.search(r"(?i)(token|password|secret|api[_-]?key|credential)", str(k)) else _redact(v)) for k, v in list(value.items())[:1000]}
    return value


def _walk(obj, pointer=""):
    yield pointer or "/", obj
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, (dict, list)): yield from _walk(v, pointer + "/" + str(k).replace("~", "~0").replace("/", "~1"))
    elif isinstance(obj, list):
        for i, v in enumerate(obj[:10000]):
            if isinstance(v, (dict, list)): yield from _walk(v, pointer + "/" + str(i))


def _identity(row):
    return {k: row[k] for k in ("operationId", "transactionId", "transaction", "planSha256", "invocationId", "requestId") if isinstance(row, dict) and row.get(k) is not None}


def _outer_rejection(row):
    if not isinstance(row, dict): return False
    text = " ".join(str(row.get(k, "")) for k in ("error", "diagnostic", "reason", "message", "status", "code")).lower()
    return "schema" in text and any(token in text for token in ("reject", "invalid", "not of type", "expected")) and "actualchanges" in text


def _exec(row):
    executed = row.get("executed") if isinstance(row, dict) else None
    code = row.get("exitCode", row.get("exit_code", row.get("returncode"))) if isinstance(row, dict) else None
    applied = row.get("actualChanges", row.get("applied", row.get("changes"))) if isinstance(row, dict) else None
    not_applied = row.get("notApplied", row.get("not_applied")) if isinstance(row, dict) else None
    cp = row.get("checkpoint", row.get("transaction")) if isinstance(row, dict) else None
    return dict(executed=executed if isinstance(executed, bool) else "unknown",
                exitCode=code if type(code) is int else "unknown", applied=_redact(applied) if applied is not None else "unknown",
                notApplied=_redact(not_applied) if not_applied is not None else "unknown", checkpoint=_redact(cp) if cp is not None else "unknown",
                outerSchemaRejected=_outer_rejection(row))


def _state(row):
    if not isinstance(row, dict): return "unknown"
    execution = _exec(row)
    status = str(row.get("status", row.get("state", ""))).lower()
    stage = str(row.get("stage", row.get("phase", ""))).lower()
    code = row.get("exitCode", row.get("exit_code", row.get("returncode")))
    # Explicit execution facts outrank downstream verification fields such as player=not_run.
    if execution["outerSchemaRejected"]:
        return "postexecution_outer_schema_rejection" if execution["executed"] is True else "execution_unknown"
    if execution["executed"] is False or status in ("not_run", "not-run", "skipped") or row.get("started") is False:
        return "not_run"
    if status in ("blocked", "requires_permission", "provider_unavailable") or row.get("blocked") is True:
        return "blocked"
    if code not in (None, 0) or status in ("failed", "error", "timeout", "timed_out"):
        return "failed"
    if status in ("partial", "execution_unknown_or_partial"):
        return "partial"
    if execution["executed"] is True and status in ("ok", "complete", "completed", "verified", "success", "succeeded"):
        return "ok"
    if stage in ("preflight", "license", "environment") and status in ("unavailable", "blocked"):
        return "blocked"
    return "unknown"


def _diag(stage, message, evidence_index, pointer, state="unknown", severity="error", code=None, location=None, execution=None, certainty="unknown"):
    loc = {"file": None, "line": None, "assembly": None, "asset": None, "node": None, "field": None}
    if location: loc.update(location)
    msg = _redact(message)
    responsibility = target = "unknown"
    next_step = "Inspect the referenced evidence and identify the responsible owner; cause is unknown."
    if stage == "compile" and loc["file"]:
        responsibility, target = "code_owner", loc["file"]
        next_step = f"Resolve compiler diagnostic {code or 'reported'} at {loc['file']}:{loc['line'] or 'unknown'}, then rerun the affected compile."
    elif stage == "outer_schema":
        responsibility, target = "result_contract_owner", "outer result schema"
        next_step = "Keep the execution receipt and checkpoint; correct the consumer schema and validate the saved response offline. Do not replay."
    elif stage in ("import", "attach", "play", "player"):
        responsibility = {"import":"asset_or_import_pipeline_owner", "attach":"provider_operator", "play":"game_code_owner", "player":"player_process_owner"}[stage]
        target = loc["asset"] or loc["file"] or stage
        next_step = f"Inspect the {stage} evidence and source location before retrying the affected stage."
    occ = [{"evidenceIndex": evidence_index, "jsonPointer": pointer}]
    return dict(severity=severity, stage=stage, state=state, certainty=certainty, code=code or "unknown", message=msg, location=loc,
                responsibility=responsibility, target=target, nextStep=next_step, evidence=occ, occurrences=occ.copy(), execution=execution or {})


def _structured(data, evidence_index):
    out = []
    for pointer, row in _walk(data):
        if not isinstance(row, dict): continue
        state, execution = _state(row), _exec(row)
        if _outer_rejection(row):
            out.append(_diag("outer_schema", "Explicit outer-schema rejection recorded; preserve the original receipt.", evidence_index, pointer, state, execution=execution))
        for key in ("diagnostics", "errors", "messages"):
            entries = row.get(key)
            if not isinstance(entries, list): continue
            for i, entry in enumerate(entries):
                ep = pointer.rstrip("/") + "/" + key + "/" + str(i)
                if isinstance(entry, dict):
                    stage = str(entry.get("stage", entry.get("phase", entry.get("kind", "provider")))).lower()
                    loc = {"file":entry.get("file"), "line":entry.get("line"), "assembly":entry.get("assembly"), "asset":entry.get("asset"), "node":entry.get("nodePath",entry.get("node")), "field":entry.get("field")}
                    out.append(_diag(stage, entry.get("message",entry.get("error",entry.get("text",entry.get("code","unknown issue")))), evidence_index, ep, state if state != "unknown" else str(entry.get("state","unknown")), str(entry.get("severity","error")), entry.get("code"), loc, execution))
                elif isinstance(entry, str): out.extend(_log_line(entry, evidence_index, ep))
        # Beaver UI analyzer and wiring contracts have concrete identities and certainty.
        if isinstance(row.get("issues"), list):
            for i, issue in enumerate(row["issues"]):
                if not isinstance(issue, dict): continue
                loc = {"asset":issue.get("asset"), "node":issue.get("nodePath",issue.get("node")), "field":issue.get("field")}
                classification=str(issue.get("classification","unknown")).lower()
                issue_status=str(issue.get("status","unknown")).lower()
                severity=str(issue.get("severity","warning")).lower()
                if classification in ("failure","failed","error") or issue_status in ("failed","failure","error") or severity in ("error","fatal"):
                    issue_state="failed"
                elif classification=="partial" or issue_status=="partial": issue_state="partial"
                else: issue_state="unknown"
                out.append(_diag("ui_inspection", issue.get("code",issue.get("message","UI issue")), evidence_index, pointer+"/issues/"+str(i), issue_state, severity, issue.get("code"), loc, certainty=str(issue.get("certainty","unknown"))))
        if isinstance(row.get("bindings"), list):
            for i, binding in enumerate(row["bindings"]):
                if not isinstance(binding, dict): continue
                stateval = str(binding.get("status","unknown"))
                if stateval in ("blocked", "failed", "ambiguous") or binding.get("reason"):
                    req = binding.get("request", {})
                    loc = {"asset":req.get("asset"), "node":req.get("nodePath"), "field":req.get("field")}
                    out.append(_diag("ui_wiring", binding.get("reason",stateval), evidence_index, pointer+"/bindings/"+str(i), "blocked" if stateval in ("blocked","ambiguous") else stateval, "error", stateval, loc))
        # Explicit process and verification status, without searching unrelated JSON text.
        if row.get("timedOut") is True or row.get("timeout") is True:
            out.append(_diag("player", "Player process timed out.", evidence_index, pointer, "failed", execution=execution))
        elif row.get("status") in ("blocked", "not_run", "failed", "partial", "execution_unknown_or_partial"):
            stage = str(row.get("stage",row.get("operation",row.get("provider","provider"))))
            out.append(_diag(stage, row.get("reason",row.get("error",row.get("status"))), evidence_index, pointer, _state(row), execution=execution))
        # Explicit verification subresults are retained as separate checkpoints.
        ver = row.get("verification")
        if isinstance(ver, dict):
            for key, val in ver.items():
                if val in ("not_run", "failed", "blocked", "partial", "timeout", "timed_out"):
                    out.append(_diag(key, val, evidence_index, pointer+"/verification/"+key, {"not_run":"not_run","failed":"failed","blocked":"blocked","partial":"partial","timeout":"failed","timed_out":"failed"}[val], execution=execution))
    return out


def _log_line(line, evidence_index, pointer, exit_code="unknown"):
    clean = _redact(line); first = _COMPILER_FIRST.search(clean)
    if _ENV_LICENSE.search(clean):
        execution=dict(executed="unknown",exitCode=exit_code,applied="unknown",notApplied="unknown",checkpoint="unknown",outerSchemaRejected=False)
        return [_diag("environment_license",clean,evidence_index,pointer,"not_run","warning",location={},execution=execution)]
    if first:
        sev = first.group("severity").lower(); loc={"file":first.group("file"),"line":int(first.group("line"))}
        return [_diag("compile", first.group("message"), evidence_index, pointer, "failed" if sev=="error" else "unknown", sev, first.group("code"), loc)]
    match = _COMPILER.search(clean)
    if match:
        loc={}; stack=_STACK.search(clean)
        if stack: loc={"file":stack.group("file"),"line":int(stack.group("line"))}
        sev=match.group("severity").lower()
        return [_diag("compile",match.group("message"),evidence_index,pointer,"failed" if sev=="error" else "unknown",sev,match.group("code"),loc)]
    loc={}; stack=_STACK.search(clean)
    if stack:
        loc={"file":stack.group("file"),"line":int(stack.group("line"))}
        return [_diag("play",clean,evidence_index,pointer,"failed",location=loc)]
    for stage, pattern in _STAGE_PATTERNS:
        if pattern.search(clean):
            state="failed" if stage in ("compile","play","player") else "unknown"
            if "timed out" in clean.lower() or "timeout" in clean.lower(): state="failed"
            return [_diag(stage,clean,evidence_index,pointer,state,location=loc)]
    return []


def _parse_embedded(value):
    if not isinstance(value,str): return None
    try: return json.loads(value)
    except (ValueError,TypeError): return None


def _link_execution(parsed, evidence):
    """Join only explicit operation identities or receipt->artifact path/hash references."""
    joins=[]
    for source_i, root in enumerate(parsed):
        for ptr,row in _walk(root):
            if not isinstance(row,dict) or not _outer_rejection(row): continue
            base=_identity(row); matches=[]
            for target_i,target_root in enumerate(parsed):
                if target_i==source_i: continue
                for tptr,target in _walk(target_root):
                    if not isinstance(target,dict): continue
                    tid=_identity(target)
                    common=set(base)&set(tid)
                    if common and all(base[k]==tid[k] for k in common): matches.append((target_i,tptr,target))
                    stdout=_parse_embedded(target.get("stdout"))
                    if stdout and isinstance(stdout,dict):
                        for artifact in stdout.get("artifacts",[]):
                            if not isinstance(artifact,dict): continue
                            for j,ev in enumerate(evidence):
                                if ev["path"]==artifact.get("path") and ev["sha256"]==artifact.get("sha256"):
                                    joins.append((source_i,ptr,target_i,tptr,target,j,artifact))
            if len(matches)==1:
                target_i,tptr,target=matches[0]
                joins.append((source_i,ptr,target_i,tptr,target,None,None))
    return joins


def summarize(inputs, output=None, max_items=100):
    if not isinstance(inputs,(list,tuple)) or len(inputs)>MAX_INPUTS: fail("DIAGNOSTIC_INPUT","inputs",f"Expected at most {MAX_INPUTS} explicit paths")
    if type(max_items) is not int or not 1<=max_items<=10000: fail("DIAGNOSTIC_LIMIT","max_items","Expected integer from 1..10000")
    evidence,parsed,diagnostics,total=[],[],[],0
    for idx,raw_path in enumerate(inputs):
        path=safe(raw_path); raw=read(path,MAX_TOTAL_BYTES); total+=len(raw)
        if total>MAX_TOTAL_BYTES: fail("DIAGNOSTIC_BUDGET","inputs","Combined input exceeds 64 MiB")
        ev=dict(path=str(path),sha256=hashlib.sha256(raw).hexdigest(),bytes=len(raw),kind="unknown"); evidence.append(ev)
        try:
            data=load(raw); ev["kind"]="json"; parsed.append(data); diagnostics.extend(_structured(data,idx))
        except (ValueError,UnicodeDecodeError):
            text=raw.decode("utf-8","replace");ev["kind"]="log";parsed.append(None)
            exit_match=_EXIT.search(text); exit_code=int(exit_match.group("code")) if exit_match else "unknown"
            for line_no,line in enumerate(text.splitlines(),1): diagnostics.extend(_log_line(line,idx,f"line:{line_no}",exit_code))
    # Normalize and collapse duplicates while retaining every evidence pointer.
    merged={}
    for item in diagnostics:
        key=(item["stage"],item["code"],item["message"],json.dumps(item["location"],sort_keys=True,ensure_ascii=False))
        if key not in merged: merged[key]=item
        else:
            merged[key]["occurrences"].extend(item["occurrences"])
            merged[key]["evidence"]=[{"evidenceIndex":i} for i in sorted({o["evidenceIndex"] for o in merged[key]["occurrences"]})]
    diagnostics=list(merged.values())
    # Link outer rejection only through a shared explicit operation identity, or exact artifact path+hash.
    for source_i,ptr,target_i,tptr,target,artifact_i,artifact in _link_execution(parsed,evidence):
        for d in diagnostics:
            if d["stage"]!="outer_schema" or not any(o["evidenceIndex"]==source_i and o["jsonPointer"]==ptr for o in d["occurrences"]): continue
            if artifact_i is not None:
                stdout=_parse_embedded(target.get("stdout")); result=stdout if isinstance(stdout,dict) else {}
                if result.get("executed") is True or result.get("status") in ("ok","verified","complete"):
                    d["execution"].update(executed=True, exitCode=target.get("exit_code",target.get("exitCode","unknown")), outerSchemaRejected=True)
                    d["execution"]["applied"]=_redact(result.get("actualChanges",result.get("data",{}).get("changes","unknown")))
                    d["execution"]["checkpoint"]=_redact(result.get("data",{}).get("transaction",artifact.get("path","unknown")))
                    d["occurrences"].append({"evidenceIndex":target_i,"jsonPointer":tptr+"/stdout"})
                    d["occurrences"].append({"evidenceIndex":artifact_i,"jsonPointer":"/"})
                    d["state"]="postexecution_outer_schema_rejection"
    all_count=len(diagnostics); truncated=max(0,all_count-max_items)
    states={d["state"] for d in diagnostics}
    diagnostics=diagnostics[:max_items]
    root_success=any(isinstance(root,dict) and root.get("executed") is True and str(root.get("status","")).lower() in ("ok","complete","completed","verified","success","succeeded") for root in parsed)
    status=("postexecution_outer_schema_rejection" if "postexecution_outer_schema_rejection" in states else
            "execution_unknown" if "execution_unknown" in states else "failed" if "failed" in states else
            "blocked" if "blocked" in states else "partial" if "partial" in states else
            "ok" if root_success else
            "not_run" if not inputs or states=={"not_run"} else "unknown")
    result=dict(schemaVersion="beaver-diagnostics/1",status=status,rawStatus=status,summary=f"{len(diagnostics)} diagnostic(s) shown from {all_count} unique; {len(evidence)} evidence file(s), {total} input bytes.",
                diagnostics=diagnostics,evidence=evidence,counts={"diagnostics":len(diagnostics),"total":all_count,"truncated":truncated,"evidence":len(evidence),"inputBytes":total})
    if output is not None:
        out=safe(output)
        if out.exists(): fail("OUTPUT_EXISTS",str(out),"Diagnostic output is create-only")
        out.parent.mkdir(parents=True,exist_ok=True);out.mkdir(exist_ok=False)
        dest=out/"diagnostics.json"
        fd=__import__("os").open(dest,__import__("os").O_WRONLY|__import__("os").O_CREAT|__import__("os").O_EXCL,0o600)
        with __import__("os").fdopen(fd,"w",encoding="utf-8") as stream: json.dump(result,stream,ensure_ascii=False,indent=2);stream.write("\n")
        return {"path":str(dest),"report":result}
    return result

"""Explicit strict JSON authoring and C# DTO/reader subset. No recovered game tables."""
import re
import math
from .io import safe, read, load, sha, new_output
from .errors import fail

IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
KINDS = {"int", "double", "string", "bool"}


def schema(path):
    raw = read(path)
    doc = load(raw)
    if set(doc) != {"schemaVersion", "name", "key", "fields"} or doc["schemaVersion"] != 1:
        fail("CONFIG_SCHEMA", str(path), "Expected closed version-1 DTO schema")
    if not IDENTIFIER.fullmatch(doc["name"]) or doc["name"] in ("class", "namespace", "public"):
        fail("CONFIG_NAME", str(path), "Expected safe C# type identifier")
    fields = doc["fields"]
    if not isinstance(fields, list) or not 1 <= len(fields) <= 256:
        fail("CONFIG_FIELDS", str(path), "Expected 1..256 fields")
    names = set()
    for field in fields:
        if not {"name", "type"} <= set(field) or set(field) - {"name", "type", "min", "max"}:
            fail("CONFIG_FIELD", str(path), "Unknown field attributes")
        if not IDENTIFIER.fullmatch(field["name"]) or field["type"] not in KINDS or field["name"] in names:
            fail("CONFIG_FIELD", str(path), "Duplicate field, invalid name or unsupported type")
        names.add(field["name"])
        for bound in ("min", "max"):
            if bound in field and (field["type"] not in ("int", "double") or type(field[bound]) not in (int, float) or not math.isfinite(field[bound])):
                fail("CONFIG_BOUND", field["name"], "Numeric finite bounds required")
        if field.get("min", -math.inf) > field.get("max", math.inf):
            fail("CONFIG_BOUND", field["name"], "Reversed bounds")
    key = next((f for f in fields if f["name"] == doc["key"]), None)
    if key is None or key["type"] not in ("string", "int"):
        fail("CONFIG_KEY", str(path), "Required scalar string/int key")
    return doc, sha(raw)


def validate(schema_path, input_path):
    doc, digest = schema(schema_path)
    raw = read(input_path)
    data = load(raw)
    if set(data) != {"rows"} or not isinstance(data["rows"], list) or not 1 <= len(data["rows"]) <= 10000:
        fail("CONFIG_ROWS", str(input_path), "Expected nonempty rows array (up to 10000)")
    keys = set()
    names = {f["name"] for f in doc["fields"]}
    for i, row in enumerate(data["rows"]):
        if not isinstance(row, dict) or set(row) != names:
            fail("CONFIG_ROW_FIELDS", str(i), "Missing or unknown field; no silent defaults")
        for f in doc["fields"]:
            value = row[f["name"]]
            kind = f["type"]
            valid = ((type(value) is int and -2147483648 <= value <= 2147483647) if kind == "int" else
                     type(value) is bool if kind == "bool" else
                     type(value) is str and len(value) <= 4096 if kind == "string" else
                     type(value) in (float, int) and math.isfinite(value) and abs(value) <= 2**53)
            if not valid:
                fail("CONFIG_TYPE", f"rows/{i}/{f['name']}", "Type or range mismatch")
            if kind in ("int", "double") and not f.get("min", -math.inf) <= value <= f.get("max", math.inf):
                fail("CONFIG_RANGE", f"rows/{i}/{f['name']}", "Outside declared bounds")
        key = row[doc["key"]]
        if key in keys:
            fail("CONFIG_DUPLICATE_KEY", str(i), "Duplicate ID")
        keys.add(key)
    return dict(schemaSha256=digest, sourceSha256=sha(raw), rows=len(data["rows"]),
                types="strict scalar int/double/string/bool", authoring="runtime UTF-8 JSON only")


def generate(schema_path, output):
    doc, digest = schema(schema_path)
    name = doc["name"]
    fields = "\n".join(f'    [JsonProperty(Required = Required.Always)] public {f["type"]} @{f["name"]};' for f in doc["fields"])
    allowed = ", ".join('"'+f['name']+'"' for f in doc['fields'])
    validators = []
    for f in doc['fields']:
        n, t = f['name'], f['type']
        token = 'Integer' if t == 'int' else 'String' if t == 'string' else 'Boolean' if t == 'bool' else 'Float'
        check = f't["{n}"].Type != JTokenType.{token}'
        if t == 'double':
            check = f'(t["{n}"].Type != JTokenType.Float && t["{n}"].Type != JTokenType.Integer)'
        validators.append(f'            if ({check}) throw new FormatException("Wrong type: {n}");')
        if t == 'string':
            validators.append(f'            if (((string)t["{n}"]).Length > 4096) throw new FormatException("String budget: {n}");')
        if t in ('int','double'):
            validators.append(f'            double v_{n} = (double)t["{n}"];')
            checks = [f'double.IsNaN(v_{n})', f'double.IsInfinity(v_{n})']
            low = f.get('min', -2147483648 if t == 'int' else -(2**53))
            high = f.get('max', 2147483647 if t == 'int' else 2**53)
            checks.extend([f'v_{n} < {low!r}', f'v_{n} > {high!r}'])
            validators.append('            if ('+' || '.join(checks)+f') throw new FormatException("Range: {n}");')
    code = '''// Generated by beaver. Runtime JSON is the only numeric authoring source.
using System;
using System.Linq;
using System.Collections.Generic;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;
[Serializable] public sealed class NAME {
FIELDS
}
public static class NAMEJson {
    public static NAME[] Load(UnityEngine.TextAsset source) {
        if (source == null) throw new ArgumentNullException(nameof(source));
        var root = JObject.Parse(source.text, new JsonLoadSettings { DuplicatePropertyNameHandling = DuplicatePropertyNameHandling.Error });
        if (root.Count != 1 || !(root["rows"] is JArray rows) || rows.Count < 1 || rows.Count > 10000) throw new FormatException("Expected nonempty rows only");
        var allowed = new HashSet<string>(new [] { ALLOWED });
        var keys = new HashSet<string>();
        var result = new List<NAME>();
        foreach (var item in rows) {
            if (!(item is JObject t) || t.Count != allowed.Count || t.Properties().Any(p => !allowed.Contains(p.Name))) throw new FormatException("Missing or unknown fields");
VALIDATORS
            if (!keys.Add(t["KEY"].ToString(Formatting.None))) throw new FormatException("Duplicate ID");
            result.Add(t.ToObject<NAME>());
        }
        return result.ToArray();
    }
}
'''.replace('FIELDS','@@FIELDS@@').replace('ALLOWED','@@ALLOWED@@').replace('VALIDATORS','@@VALIDATORS@@').replace('KEY','@@KEY@@').replace('NAME','@@TYPE@@')
    code=code.replace('@@FIELDS@@',fields).replace('@@ALLOWED@@',allowed).replace('@@VALIDATORS@@','\n'.join(validators)).replace('@@KEY@@',doc['key']).replace('@@TYPE@@','@'+name)
    out = new_output(output, [safe(schema_path)])
    p = out / (name + '.cs')
    p.write_text(code)
    return dict(report=str(p), sourceSchemaSha256=digest, provider='com.unity.nuget.newtonsoft-json 3.2.2',
                generated='strict DTO and TextAsset reader only', runtimeAuthoring='JSON; no duplicated numeric table')

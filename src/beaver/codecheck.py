"""Read-only Unity asmdef reference checks; native compilation is a separate layer."""
from pathlib import Path
from .io import inside, safe, read, load
from .context import inspect
from .output_limits import digest_file
import re


def inspect_code(project):
    project=safe(project)
    context=inspect(project)
    if context['engine']!='unity':
        return dict(status='not_run',reason='Creator code checks not implemented')
    assemblies={}
    guids={}
    sources=[]
    for base in (inside(project,'Assets'),inside(project,'Packages'),inside(project,'Library/PackageCache')):
        if not base.is_dir():continue
        for p in base.rglob('*.asmdef'):
            p=safe(p);doc=load(read(p));name=doc.get('name')
            if not isinstance(name,str):continue
            row=dict(path=str(p),sha256=digest_file(p),references=doc.get('references',[]))
            assemblies.setdefault(name,[]).append(row)
            meta=Path(str(p)+'.meta')
            if meta.is_file():
                match=re.search(rb'^guid:\s*([0-9a-f]{32})\s*$',read(meta),re.M)
                if match:guids[match.group(1).decode()]=name
    diagnostics=[]
    for name,rows in assemblies.items():
        if len(rows)>1:
            diagnostics.append(dict(code='DUPLICATE_ASSEMBLY',location=name,paths=[r['path'] for r in rows]))
        for row in rows:
            for reference in row['references']:
                target=guids.get(reference[5:]) if reference.startswith('GUID:') else reference
                if target not in assemblies:
                    diagnostics.append(dict(code='UNRESOLVED_ASMDEF_REFERENCE',location=row['path'],reference=reference))
    for p in inside(project,'Assets').rglob('*.cs'):
        p=safe(p)
        source=read(p).decode('utf-8-sig')
        sources.append(dict(path=p.relative_to(project).as_posix(),sha256=digest_file(p),
                            usingNamespaces=sorted(set(re.findall(r'^using\s+([A-Za-z_][\w.]*)\s*;',source,re.M)))))
    return dict(status='partial' if diagnostics else 'ok',assemblies=len(assemblies),sources=sources,
                diagnostics=diagnostics,nativeCompilation='not_run',
                scope='asmdef names/GUID references across Assets, embedded Packages and imported PackageCache; using list is inventory only')

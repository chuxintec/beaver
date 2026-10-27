from beaver.io import write_json
from beaver.output_limits import digest_file

def finish(root, report):
    report['files'] = [dict(path=p.relative_to(root).as_posix(), sha256=digest_file(p), bytes=p.stat().st_size, owner='generated') for p in sorted(root.rglob('*')) if p.is_file() and p != root/'manifest.json']
    write_json(root/'manifest.json', report)

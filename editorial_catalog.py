"""Descubrimiento por contenido; catálogo relativo reconstruible, nunca autoridad."""
from __future__ import annotations
import os
from pathlib import Path
from editorial_io import atomic_write_json, read_json
from editorial_chunks import source_master_digest
from editorial_trims import identity, same_identity

EXCLUDED = {".git", ".work", ".venv", ".bootstrap", "runtimes", "releases", "shared",
            "node_modules", "__pycache__", ".transcriptor", "tracks", "updates", "dist"}


def scan(root):
    root = Path(root).resolve()
    index = root / ".transcriptor" / "catalog.json"
    try:
        previous = read_json(index)
        cache = previous.get("entries", {}) if previous.get("schema") == "editorial-catalog/1" else {}
    except (OSError,ValueError):
        cache = {}
    entries, warnings = {}, []
    for folder, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = [d for d in dirs if d not in EXCLUDED and not d.startswith(".podcast-")
                   and not Path(folder,d).is_symlink() and not Path(folder,d).is_junction()]
        for filename in files:
            if not filename.endswith(".editorial.master.json"):
                continue
            path = Path(folder,filename)
            if path.is_symlink():
                continue
            relative = path.relative_to(root).as_posix()
            try:
                stat = path.stat()
                stamp = [stat.st_size,stat.st_mtime_ns]
                old = cache.get(relative)
                if old and old.get("stamp") == stamp:
                    entries[relative] = old
                    continue
                data=read_json(path)
                if data.get("schema") != "editorial-master/1":
                    continue
                entries[relative]=dict(stamp=stamp,fingerprint=identity(data['media']['fingerprint']),
                                       digest=source_master_digest(data),project=data.get('project',{}).get('name'))
            except (OSError, ValueError, KeyError,TypeError) as error:
                warnings.append(f"{relative}: {error}")
    result=dict(schema="editorial-catalog/1",entries=entries)
    if result != {"schema":"editorial-catalog/1","entries":cache} or not index.exists():
        try:
            atomic_write_json(index,result)
        except OSError as error:
            warnings.append(f"Catálogo no persistido (descubrimiento disponible): {error}")
    return result,warnings


def discover(source, fingerprint, *, root=None):
    source=Path(source).resolve()
    if root is None:
        ancestors=[p for p in source.parents if (p/'.transcriptor/catalog.json').is_file()]
        root=ancestors[0] if ancestors else source.parent
        if not ancestors and root.parent != Path(root.anchor):
            root=root.parent
    root=Path(root).resolve()
    catalog,warnings=scan(root)
    candidates=[]
    for relative,entry in catalog['entries'].items():
        if not same_identity(entry.get('fingerprint'),fingerprint):
            continue
        path=root/relative
        try:
            master=read_json(path)
            if master.get('schema')=='editorial-master/1' and same_identity(master['media']['fingerprint'],fingerprint):
                candidates.append(dict(path=str(path),digest=source_master_digest(master),
                                       name=master.get('project',{}).get('name',path.stem)))
        except (OSError,ValueError,KeyError) as error:
            warnings.append(str(error))
    candidates.sort(key=lambda c: (len(os.path.relpath(c['path'],source.parent).split(os.sep)),c['path']))
    unique={}
    for c in candidates:
        unique.setdefault(c['digest'],c)
    return list(unique.values()),warnings

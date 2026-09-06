"""Cachés de navegación: frames exactos acotados y waveform por identidad de contenido."""
from collections import OrderedDict
from pathlib import Path
import math

from editorial_io import atomic_write_json, digest_json, read_json
from editorial_trims import identity


class FrameCache:
    def __init__(self, budget=48*1024*1024):
        self.budget=budget
        self.clear()

    def clear(self):
        self.frames=OrderedDict()
        self.bytes=0

    def put(self, timestamp, image):
        key=(timestamp,image.width)
        old=self.frames.pop(key,None)
        if old is not None:
            self.bytes-=old.width*old.height*3
        self.frames[key]=image
        self.bytes+=image.width*image.height*3
        while self.bytes>self.budget and self.frames:
            _,old=self.frames.popitem(last=False)
            self.bytes-=old.width*old.height*3

    def get(self,timestamp,width):
        key=(timestamp,width)
        if key in self.frames:
            self.frames.move_to_end(key)
            return self.frames[key]
        return None


def envelope(video, track, fingerprint, duration, *, buckets=4000, cancel=None, progress=None, cache_dir=None):
    import medios
    if cache_dir is None:
        import app_paths
        cache_dir=app_paths.CACHE_DIR/'waveforms'
    key=dict(schema='editorial-waveform/1',fingerprint=identity(fingerprint),track=track,
             duration=duration,buckets=buckets,sample_rate=8000)
    path=Path(cache_dir)/(digest_json(key)+'.json')
    try:
        cached=read_json(path)
        values=cached.get('values')
        if (cached.get('key')==key and isinstance(values,list) and len(values)==buckets
                and all(isinstance(row,list) and len(row)==3
                        and all(isinstance(v,(int,float)) and math.isfinite(v) for v in row) for row in values)):
            if progress:
                progress(1.0)
            return values
    except (OSError,ValueError,TypeError):
        pass
    values=medios.envolvente(video,track,buckets=buckets,dur=duration,cancel=cancel,progreso=progress)
    if (cancel is None or not cancel.is_set()) and len(values)==buckets:
        try:
            atomic_write_json(path,dict(key=key,values=values))
        except OSError:
            pass  # la caché es prescindible; la waveform calculada sigue disponible
    return values

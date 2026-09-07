"""Mide el mini-editor Tk real y compara frame presentado con reloj de audio FFplay.

`--rates 1,2,3,4,8` añade la medición por velocidad (diseño docs/diseno-navegacion-editor.md
§5): por posición y velocidad, desfase mediano y P95, respawns del stream, frames
mostrados por segundo de pared y latencia de la tecla al primer frame nuevo.
"""
from pathlib import Path
import argparse
import json
import statistics
import subprocess
import sys
import threading
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
parser=argparse.ArgumentParser()
parser.add_argument('--source',required=True)
parser.add_argument('--output',required=True)
parser.add_argument('--baseline-dir')
parser.add_argument('--rates',default='1',help='velocidades a medir, p.ej. 1,2,3,4,8')
parser.add_argument('--seconds',type=float,default=3.0,
                    help='segundos de muestreo por posición y velocidad (el diseño pide 60)')
parser.add_argument('--positions',default='0,1800,5400')
args=parser.parse_args()
if args.baseline_dir:
    sys.path.insert(0,args.baseline_dir)
import medios
from playback_clock import AudioClock
from editor_medios import EditorMedios
import customtkinter as ctk

RATES=[float(r) for r in args.rates.split(',') if r.strip()]
POSITIONS=[float(p) for p in args.positions.split(',') if p.strip()]

observed_clock=None
native=hasattr(medios.Reproductor,'position')
if not native:
    original=medios._popen
    def instrument(command,**kw):
        global observed_clock
        is_player=command[0]=='ffplay'
        if is_player:
            command=[*command]
            command[command.index('-loglevel')+1]='info'
            command.insert(1,'-stats')
            kw['stderr']=subprocess.PIPE
        process=original(command,**kw)
        if is_player:
            clock=observed_clock=AudioClock()
            def read():
                buffer=bytearray()
                try:
                    for value in iter(lambda:process.stderr.read(1),b''):
                        if value in (b'\r',b'\n'):
                            clock.feed(buffer.decode('utf-8','replace'));buffer.clear()
                        else:
                            buffer.extend(value)
                finally:
                    process.stderr.close()
            threading.Thread(target=read,daemon=True).start()
        return process
    medios._popen=instrument

original_frame=medios.VideoStream.frame_hasta
def frame(self,t):
    result=original_frame(self,t)
    if result:
        self.presented_timestamp=result[0]
    return result
medios.VideoStream.frame_hasta=frame

app=ctk.CTk()
app.title('Transcriptor — medición del reproductor')
app.geometry('1200x850')
editor=EditorMedios(app)
editor.f.pack(fill='both',expand=True)
errors=[]
app.report_callback_exception=lambda *args:errors.append(str(args))
def spin(predicate,timeout=60):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        app.update()
        if errors:
            raise AssertionError(errors)
        if predicate():
            return
        time.sleep(.01)
    raise AssertionError('timeout: '+str(predicate))

tick_costs=[]
if hasattr(editor,'_anim_tick'):
    original_tick=editor._anim_tick
    def timed_tick(*a,**k):
        t=time.perf_counter()
        try:
            return original_tick(*a,**k)
        finally:
            tick_costs.append((time.perf_counter()-t)*1000)
    editor._anim_tick=timed_tick

def play_and_sample(target,rate,seconds):
    """Reproduce desde `target` a `rate` y muestrea `seconds` de pared."""
    global tick_costs
    if rate!=1.0 and hasattr(editor,'set_rate'):
        editor._play(reiniciar=True,desde=target,rate=rate)
    else:
        editor._play(reiniciar=True,desde=target)
    spin(lambda:editor._warmup is None and editor.repro.playing(),timeout=20)
    spin(lambda:(editor.repro.position() if native else observed_clock.position()) is not None,timeout=20)
    clock=editor.repro.clock if native else observed_clock
    samples=[]
    tick_costs=[]
    frames_before=editor._frame_rev
    started=time.monotonic()
    deadline=started+seconds
    while time.monotonic()<deadline:
        app.update()
        c=clock.position()
        s=editor._vses._stream if editor._vses else None
        shown=getattr(s,'presented_timestamp',None)
        if c is not None and shown is not None:
            samples.append(shown-(c if native else target+c))
        time.sleep(.005)
    wall=time.monotonic()-started
    row=dict(start=target,rate=rate,first_frame_ms=round(editor._vses.primer_frame_s()*1000,1),
             audio_clock_ms=round(clock.first_clock_s*1000,1),
             median_frame_minus_audio_ms=round(statistics.median(samples)*1000,1) if samples else None,
             p95_abs_error_ms=round(sorted(abs(v) for v in samples)[int(len(samples)*.95)]*1000,1) if samples else None,
             sample_count=len(samples),
             frames_per_wall_s=round((editor._frame_rev-frames_before)/wall,1),
             respawns=getattr(editor._vses,'_respawns',None),
             video_failed=bool(getattr(editor._vses,'fallida',False)),
             audio_muted=bool(getattr(editor.repro,'mudo',False)),
             tick_mean_ms=round(statistics.mean(tick_costs),2) if tick_costs else None,
             seconds=round(wall,1))
    return row

result=dict(mode='after' if native else 'before',source=Path(args.source).name,playback=[],seeks=[],
            rate_changes=[])
try:
    start=time.monotonic()
    editor.cargar(args.source)
    spin(lambda:editor.info is not None)
    result['inspection_s']=round(time.monotonic()-start,3)
    result['duration_s']=editor.info['duracion']
    for rate in RATES:
        for target in POSITIONS:
            row=play_and_sample(target,rate,args.seconds)
            result['playback'].append(row)
            print(json.dumps(row),flush=True)
            editor._stop_preview()
    # cambio de velocidad DURANTE la reproducción: de la tecla al primer frame nuevo
    if hasattr(editor,'set_rate') and len(RATES)>1:
        for target in POSITIONS:
            editor._play(reiniciar=True,desde=target,rate=RATES[0])
            spin(lambda:editor._warmup is None and editor.repro.playing(),timeout=20)
            spin(lambda:editor.repro.position() is not None,timeout=20)
            for rate in RATES[1:]:
                session=editor._vses
                t=time.monotonic()
                editor.set_rate(rate)
                spin(lambda:editor._vses is not session and editor._vses is not None
                     and editor._vses.lista() and editor.repro.playing(),timeout=20)
                ms=round((time.monotonic()-t)*1000,1)
                result['rate_changes'].append(dict(start=target,to_rate=rate,ms=ms))
                print(json.dumps(result['rate_changes'][-1]),flush=True)
                spin(lambda:editor.repro.position() is not None,timeout=20)
            editor._stop_preview()
        result['rate_change_median_ms']=round(statistics.median(r['ms'] for r in result['rate_changes']),1)
    spin(lambda:editor._wave_s is not None,timeout=120)
    result['waveforms_ready_s']=editor._wave_s
    editor._prefetch.parar()
    editor._reapuntar_prefetch=lambda:None
    editor._fcache.clear()
    for phase in ('first','repeat'):
        for target in (1800,5400,1800):
            rev=editor._frame_rev
            t=time.monotonic()
            editor._set_playhead(target)
            spin(lambda:editor._frame_rev>rev,timeout=20)
            result['seeks'].append(dict(phase=phase,target=target,ms=round((time.monotonic()-t)*1000,1)))
    zoom=[]
    for _ in range(10):
        t=time.monotonic();editor._zoom(2);editor._zoom(.5);app.update_idletasks()
        zoom.append((time.monotonic()-t)*1000)
    result['zoom_pair_median_ms']=round(statistics.median(zoom),1)
    Path(args.output).parent.mkdir(parents=True,exist_ok=True)
    Path(args.output).write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps(result),flush=True)
finally:
    editor.cerrar()
    app.destroy()

import tempfile
import time
import unittest
from unittest import mock
from types import SimpleNamespace
from pathlib import Path
import shutil

import medios
import preview_cache
from playback_clock import AudioClock


class PlaybackTests(unittest.TestCase):
    def test_clock_follows_output_not_spawn_and_loses_stale_clock(self):
        clock=AudioClock(1800)
        self.assertIsNone(clock.position())
        self.assertFalse(clock.feed('nan M-A: nan',now=100))
        self.assertTrue(clock.feed('   -0.04 M-A: 0.000 fd=0',now=100))
        self.assertEqual(clock.position(now=100.02),1800)
        self.assertTrue(clock.feed('    2.50 M-A: -0.000',now=102.54))
        self.assertAlmostEqual(clock.position(now=102.64),1802.6)
        self.assertIsNone(clock.position(now=104))
        other=AudioClock(5400)
        self.assertIsNone(other.position())

    def test_frame_cache_is_exact_and_bounded(self):
        cache=preview_cache.FrameCache(budget=600)
        image=SimpleNamespace(width=10,height=10)
        cache.put(1.25,image)
        self.assertIs(cache.get(1.25,10),image)
        self.assertIsNone(cache.get(1.3,10))
        self.assertIsNone(cache.get(1.25,20))
        cache.put(2,image)
        cache.put(3,image)
        self.assertIsNone(cache.get(1.25,10))
        self.assertEqual(cache.bytes,600)
        cache.clear()
        self.assertEqual(cache.bytes,0)

    def test_waveform_reused_by_content_and_invalidated(self):
        fp=dict(size=1,hash_muestreado='a',inventario_sha256='b')
        values=[[0,.5,.2],[0,.3,.1]]
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(medios,'envolvente',return_value=values) as decode:
            first=preview_cache.envelope('one',0,fp,2,buckets=2,cache_dir=tmp)
            second=preview_cache.envelope('moved',0,fp,2,buckets=2,cache_dir=tmp)
            self.assertEqual(first,second)
            self.assertEqual(decode.call_count,1)
            preview_cache.envelope('moved',1,fp,2,buckets=2,cache_dir=tmp)
            preview_cache.envelope('moved',0,{**fp,'hash_muestreado':'changed'},2,buckets=2,cache_dir=tmp)
            self.assertEqual(decode.call_count,3)
            for path in Path(tmp).glob('*.json'):
                path.write_text('{')
            preview_cache.envelope('one',0,fp,2,buckets=2,cache_dir=tmp)
            self.assertEqual(decode.call_count,4)

    # ---- velocidad de reproducción (docs/diseno-navegacion-editor.md §1, Fase 1) ----
    def test_clock_scales_media_position_by_rate_and_still_loses_stale_samples(self):
        clock=AudioClock(1800,rate=2)
        self.assertTrue(clock.feed('   -0.04 M-A: 0.000 fd=0',now=100))
        self.assertEqual(clock.position(now=100.02),1800)
        self.assertTrue(clock.feed('    2.50 M-A: -0.000',now=102.54))
        # 2,6 s de pared = 5,2 s de medio a ×2
        self.assertAlmostEqual(clock.position(now=102.64),1805.2)
        # el umbral de reloj perdido sigue en segundos de PARED
        self.assertIsNone(clock.position(now=103.1))
        self.assertEqual(AudioClock(5).rate,1.0)

    def test_mix_command_stretches_audio_and_keeps_x1_untouched(self):
        base=medios.comando_mezcla('v.mp4',[1],12,1.0,rubberband=True,max_rate=4)
        self.assertEqual(base,['ffmpeg','-v','error','-nostdin','-ss','12.000','-i','v.mp4',
                               '-map','0:a:1','-f','wav','-'])
        two=medios.comando_mezcla('v.mp4',[1],12,2.0,rubberband=True,max_rate=4)
        self.assertEqual(two[two.index('-af')+1],'rubberband=tempo=2:pitchq=quality:transients=smooth')
        self.assertLess(two.index('-ss'),two.index('-i'))
        self.assertLess(two.index('-map'),two.index('-af'))
        self.assertEqual(two[-3:],['-f','wav','-'])
        # sin rubberband en el ffmpeg de la máquina → atempo (WSOLA)
        four=medios.comando_mezcla('v.mp4',[1],0,4.0,rubberband=False,max_rate=4)
        self.assertEqual(four[four.index('-af')+1],'atempo=4')
        self.assertNotIn('volume',' '.join(four))
        # volume=0 SOLO por encima de preview_audio_max_rate (el skim a ×8)
        eight=medios.comando_mezcla('v.mp4',[1],0,8.0,rubberband=False,max_rate=4)
        self.assertEqual(eight[eight.index('-af')+1],'atempo=8,volume=0')
        self.assertEqual(medios.filtro_velocidad(1.0,rubberband=True,max_rate=4),'')
        self.assertEqual(medios.filtro_velocidad(3.0,rubberband=False,max_rate=2),'atempo=3,volume=0')

    def test_mix_command_with_three_tracks_chains_the_stretch_after_amix(self):
        one=medios.comando_mezcla('v.mp4',[0,1,2],5,1.0,rubberband=True,max_rate=4)
        self.assertEqual(one[one.index('-filter_complex')+1],
                         '[0:a:0][0:a:1][0:a:2]amix=inputs=3:normalize=1[mix]')
        self.assertEqual(one[one.index('-map')+1],'[mix]')
        self.assertNotIn('-af',one)
        two=medios.comando_mezcla('v.mp4',[0,1,2],5,2.0,rubberband=True,max_rate=4)
        self.assertEqual(two[two.index('-filter_complex')+1],
                         '[0:a:0][0:a:1][0:a:2]amix=inputs=3:normalize=1[mix];'
                         '[mix]rubberband=tempo=2:pitchq=quality:transients=smooth[out]')
        self.assertEqual(two[two.index('-map')+1],'[out]')
        eight=medios.comando_mezcla('v.mp4',[0,1,2],5,8.0,rubberband=False,max_rate=4)
        self.assertTrue(eight[eight.index('-filter_complex')+1].endswith('[mix]atempo=8,volume=0[out]'))
        self.assertEqual(eight[-3:],['-f','wav','-'])

    def test_video_stream_fps_and_skip_flags_follow_the_rate(self):
        self.assertIsNone(medios.skip_para_rate(1))
        self.assertEqual(medios.skip_para_rate(2),'bidir')
        self.assertEqual(medios.skip_para_rate(4),'bidir')
        self.assertEqual(medios.skip_para_rate(8),'nokey')
        plain=medios.comando_stream('v.mp4',10,640,360,30)
        self.assertEqual(plain,['ffmpeg','-v','error','-nostdin','-ss','10.000','-i','v.mp4',
                                '-an','-sn','-dn','-vf','fps=30,scale=640:360,setsar=1',
                                '-pix_fmt','rgb24','-f','rawvideo','-'])
        fast=medios.comando_stream('v.mp4',10,640,360,30/4,skip='bidir')
        self.assertEqual(fast[fast.index('-skip_frame')+1],'bidir')
        self.assertLess(fast.index('-skip_frame'),fast.index('-i'))   # opción de ENTRADA
        self.assertIn('fps=7.5,scale=640:360,setsar=1',fast)
        window=medios.comando_stream('v.mp4',10,640,360,.5,dur=70)
        self.assertEqual(window[window.index('-t')+1],'70.000')

    def test_session_reanchors_and_degrades_keeping_the_rate(self):
        created=[]
        class FakeStream:
            def __init__(self,video,t0,w,h,fps=medios.VS_FPS,dur=None,skip=None):
                created.append(dict(t0=t0,w=w,h=h,fps=fps,skip=skip))
                self.primer_frame_s=None
            def parar(self):
                pass
        with mock.patch.object(medios,'VideoStream',FakeStream):
            session=medios.SesionVideo('v.mp4',100,960,540,rate=4)
            self.assertEqual((session.fps,session.skip),(7.5,'bidir'))
            self.assertEqual(created[-1],dict(t0=100,w=960,h=540,fps=7.5,skip='bidir'))
            session._respawn(130)
            self.assertEqual(created[-1]['t0'],130)          # re-ancla los ts sintéticos
            self.assertEqual(created[-1]['fps'],7.5)
            session._ultimo_respawn=0
            session._respawn(140)
            self.assertEqual(created[-1]['fps'],medios.VS_FPS_DEGRADADO/4)   # degradado ×4
            self.assertEqual(created[-1]['skip'],'bidir')
            self.assertEqual(created[-1]['w'],720)
            skim=medios.SesionVideo('v.mp4',0,960,540,rate=8)
            self.assertEqual((skim.fps,skim.skip),(3.75,'nokey'))
            self.assertEqual(medios.SesionVideo('v.mp4',0,960,540).skip,None)

    @unittest.skipUnless(shutil.which('ffplay') and shutil.which('ffmpeg'), 'FFplay no instalado')
    def test_real_ffplay_clock_at_double_speed(self):
        import subprocess,os
        with tempfile.TemporaryDirectory() as tmp:
            source=Path(tmp)/'audio.wav'
            subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','sine=duration=6',str(source)],
                           check=True,**medios.flags_subprocess())
            with mock.patch.dict(os.environ,{'SDL_AUDIODRIVER':'dummy'}), \
                    mock.patch.object(medios,'audio_max_rate',return_value=4.0):
                player=medios.Reproductor()
                try:
                    self.assertIsNone(player.play(source,[0],1,rate=2))
                    self.assertEqual(player.clock.rate,2)
                    self.assertFalse(player.mudo)
                    deadline=time.monotonic()+8
                    while player.position() is None and time.monotonic()<deadline:
                        time.sleep(.02)
                    self.assertIsNotNone(player.position(),player.error_tail)
                    self.assertGreaterEqual(player.position(),1)
                    self.assertIsNone(player.play(source,[0],1,rate=8))
                    self.assertTrue(player.mudo)
                    self.assertEqual(player.clock.rate,8)
                finally:
                    player.stop()

    @unittest.skipUnless(shutil.which('ffplay') and shutil.which('ffmpeg'), 'FFplay no instalado')
    def test_real_ffplay_clock_and_cleanup(self):
        # SDL dummy solo para CI: verifica el protocolo, no latencia perceptual.
        import subprocess,os
        with tempfile.TemporaryDirectory() as tmp:
            source=Path(tmp)/'audio.wav'
            subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','sine=duration=3',str(source)],
                           check=True,**medios.flags_subprocess())
            with mock.patch.dict(os.environ,{'SDL_AUDIODRIVER':'dummy'}):
                player=medios.Reproductor()
                try:
                    self.assertIsNone(player.play(source,[0],.5))
                    deadline=time.monotonic()+8
                    while player.position() is None and time.monotonic()<deadline:
                        time.sleep(.02)
                    self.assertIsNotNone(player.position(),player.error_tail)
                    self.assertGreaterEqual(player.position(),.5)
                    procs=player._procs
                finally:
                    player.stop()
                self.assertTrue(all(p.poll() is not None for p in procs))

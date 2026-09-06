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

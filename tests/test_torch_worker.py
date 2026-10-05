import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

import hardware
import torch_worker


class WorkerTests(unittest.TestCase):
    def test_initial_unknown_backends_preserve_detected_gpu(self):
        self.assertTrue(hardware.cuda_candidate({"name": "NVIDIA RTX", "torch_cuda": None, "ct2_cuda": None}))
        self.assertFalse(hardware.cuda_candidate({"name": None, "torch_cuda": None, "ct2_cuda": None}))
        self.assertFalse(hardware.cuda_candidate({"name": "NVIDIA RTX", "torch_cuda": False, "ct2_cuda": 0}))

    def test_child_removes_whisper_dll_paths_without_changing_parent(self):
        path = os.pathsep.join([r"C:\runtime\Lib\site-packages\nvidia\cudnn\bin", r"C:\Windows", r"C:\runtime\Lib\site-packages\torch\lib"])
        with patch.dict(os.environ, {"PATH": path}):
            env = torch_worker.child_environment("cuda")
            self.assertNotIn("nvidia", env["PATH"])
            self.assertIn("torch", env["PATH"])
            self.assertEqual(os.environ["PATH"], path)

    def test_worker_device_overrides_preferences_for_cpu_retry(self):
        with patch.dict(os.environ, {torch_worker.DEVICE_ENV: "cpu"}), patch.object(hardware, "torch_cuda_available", return_value=True):
            self.assertFalse(hardware.use_gpu_torch())
            self.assertFalse(torch_worker.should_isolate())

    def test_oom_retries_entire_operation_in_new_cpu_process(self):
        devices = []
        class Process:
            def __init__(self, command, **kw):
                devices.append(kw["env"][torch_worker.DEVICE_ENV])
                self.returncode = 1 if len(devices) == 1 else 0
                output = ({"ok": False, "cuda_error": True, "error_type": "OutOfMemoryError"}
                          if self.returncode else {"ok": True, "value": ["complete"]})
                Path(command[3]).write_text(json.dumps(output), encoding="utf-8")
            def poll(self):
                return self.returncode
            def wait(self):
                return self.returncode
        with patch.object(torch_worker.subprocess, "Popen", Process):
            self.assertEqual(torch_worker.run("align.align_words", {"words": []}), ["complete"])
        self.assertEqual(devices, ["cuda", "cpu"])

    def test_invalid_operation_is_rejected(self):
        with self.assertRaises(ValueError):
            torch_worker.run("os.system", {})

    def test_cancellation_does_not_start_worker(self):
        import threading
        cancel = threading.Event()
        cancel.set()
        with patch.object(torch_worker.subprocess, "Popen") as start:
            with self.assertRaises(InterruptedError):
                torch_worker.run("align.align_words", {}, cancel=cancel)
            start.assert_not_called()

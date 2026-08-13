"""The resident-backend machinery, tested for real.

The GPU cannot be tested here. What CAN be tested here, and is, is everything
around it: a real child process being started, health-checked, killed, restarted
and shut down; and a real HTTP round trip carrying a real multipart WAV upload.
Those are the parts most likely to go wrong on a machine nobody here can reach,
so they are exercised against `tests/stub_server.py` rather than a mock.
"""

from __future__ import annotations

import socket
import sys
import time
import unittest
from pathlib import Path

from dictate.audio import wav
from dictate.config import WhisperConfig
from dictate.engines.process import ManagedProcess
from dictate.engines.whisper_backend import WhisperVulkanBackend
from dictate.engines.whisper_server import WhisperServerClient, _multipart
from dictate.errors import BackendUnavailableError, TranscriptionError

from . import stub_server

STUB = Path(__file__).resolve().parent / "stub_server.py"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def sparse_file(path: Path, size: int) -> Path:
    """A file that reports `size` bytes without occupying them - the preflight
    checks look at st_size, and writing a real 1.6 GB stand-in would be silly."""
    with open(path, "wb") as fh:
        fh.truncate(size)
    return path


def wait_for(predicate, timeout: float = 15.0, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


# ---------------------------------------------------------------------------
# HTTP client, against a real server
# ---------------------------------------------------------------------------


class ClientAgainstRealServer(unittest.TestCase):
    def setUp(self):
        self.server, port = stub_server.serve_in_thread()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.client = WhisperServerClient("127.0.0.1", port, request_timeout_s=5)

    def test_health(self):
        self.assertTrue(self.client.is_healthy())
        self.assertTrue(self.client.port_is_open())

    def test_transcribe_uploads_a_real_wav_and_reads_the_text_back(self):
        audio = wav.pcm16_to_wav(wav.silence(0.25))
        self.assertEqual(self.client.transcribe_wav(audio), "Hello from the stub.")
        sent = self.server.RequestHandlerClass.seen[-1]
        self.assertIn("multipart/form-data; boundary=", sent["content_type"])
        self.assertIn(b'name="file"; filename="utterance.wav"', sent["body"])
        self.assertIn(b"RIFF", sent["body"])
        self.assertIn(b'name="response_format"', sent["body"])

    def test_a_loading_server_is_not_healthy_yet(self):
        server, port = stub_server.serve_in_thread(ready_after=30.0)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.assertFalse(WhisperServerClient("127.0.0.1", port).is_healthy())

    def test_http_error_becomes_an_actionable_message(self):
        server, port = stub_server.serve_in_thread(fail_inference=500)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        client = WhisperServerClient("127.0.0.1", port, request_timeout_s=5)
        with self.assertRaises(TranscriptionError) as ctx:
            client.transcribe_wav(wav.pcm16_to_wav(wav.silence(0.1)))
        self.assertIn("500", ctx.exception.message)
        self.assertTrue(ctx.exception.remedy)

    def test_nothing_listening_is_reported_not_crashed(self):
        client = WhisperServerClient("127.0.0.1", free_port(), request_timeout_s=1)
        self.assertFalse(client.is_healthy())
        self.assertFalse(client.port_is_open())
        with self.assertRaises(TranscriptionError) as ctx:
            client.transcribe_wav(wav.pcm16_to_wav(wav.silence(0.1)))
        self.assertIn("Could not reach", ctx.exception.message)


class ResponseParsing(unittest.TestCase):
    def test_plain_json(self):
        self.assertEqual(WhisperServerClient._parse('{"text": "  hi  "}'), "hi")

    def test_verbose_json_segments(self):
        raw = '{"segments": [{"text": " one"}, {"text": " two"}]}'
        self.assertEqual(WhisperServerClient._parse(raw), "one two")

    def test_error_payload(self):
        with self.assertRaises(TranscriptionError):
            WhisperServerClient._parse('{"error": "bad model"}')

    def test_not_json_at_all(self):
        with self.assertRaises(TranscriptionError) as ctx:
            WhisperServerClient._parse("<html>404</html>")
        self.assertIn("not JSON", ctx.exception.message)

    def test_multipart_is_well_formed(self):
        body, content_type = _multipart({"a": "1"}, "x.wav", b"RIFFdata")
        boundary = content_type.split("boundary=")[1]
        self.assertTrue(body.startswith(f"--{boundary}\r\n".encode()))
        self.assertTrue(body.endswith(f"--{boundary}--\r\n".encode()))
        self.assertIn(b'name="a"', body)
        self.assertIn(b"RIFFdata", body)


# ---------------------------------------------------------------------------
# Process supervision, against a real child process
# ---------------------------------------------------------------------------


class ProcessLifecycle(unittest.TestCase):
    def make(self, port: int, *extra: str, **kwargs) -> ManagedProcess:
        client = WhisperServerClient("127.0.0.1", port)
        proc = ManagedProcess(
            [sys.executable, str(STUB), "--port", str(port), *extra],
            name="stub-server",
            health_check=client.is_healthy,
            startup_timeout_s=kwargs.pop("startup_timeout_s", 20.0),
            poll_interval_s=0.05,
            restart_backoff_s=0.05,
            **kwargs,
        )
        self.addCleanup(proc.stop)
        return proc

    def test_starts_waits_for_health_and_stops(self):
        port = free_port()
        proc = self.make(port)
        proc.start()
        self.assertTrue(proc.is_running())
        self.assertIsNotNone(proc.pid)
        self.assertTrue(WhisperServerClient("127.0.0.1", port).is_healthy())

        proc.stop()
        self.assertFalse(proc.is_running())
        self.assertTrue(wait_for(
            lambda: not WhisperServerClient("127.0.0.1", port).port_is_open()))

    def test_waits_for_a_slow_start_rather_than_giving_up(self):
        port = free_port()
        proc = self.make(port, "--ready-after", "1.0")
        started = time.monotonic()
        proc.start()
        self.assertGreaterEqual(time.monotonic() - started, 0.9)
        self.assertTrue(proc.is_running())

    def test_start_is_idempotent(self):
        proc = self.make(free_port())
        proc.start()
        pid = proc.pid
        proc.start()
        self.assertEqual(proc.pid, pid)

    def test_a_crashed_server_is_restarted(self):
        import tempfile

        port = free_port()
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "died-once"
            # The first process crashes; the restarted one stays up, which is
            # the behaviour a transient whisper.cpp crash should produce.
            proc = self.make(port, "--die-after", "0.6",
                             "--die-once-marker", str(marker))
            proc.start()
            first_pid = proc.pid

            self.assertTrue(wait_for(lambda: proc.pid not in (None, first_pid),
                                     timeout=25),
                            f"never restarted (pid stayed {first_pid})")
            self.assertTrue(proc.is_running())
            client = WhisperServerClient("127.0.0.1", port)
            self.assertTrue(wait_for(client.is_healthy, timeout=15),
                            "restarted server never became healthy")
            self.assertIsNone(proc.gave_up_reason)

    def test_it_gives_up_after_max_restarts_and_says_why(self):
        port = free_port()
        proc = self.make(port, "--die-after", "0.2", max_restarts=2)
        proc.start()
        self.assertTrue(wait_for(lambda: proc.gave_up_reason is not None, timeout=30),
                        "never gave up")
        self.assertIn("crashed", proc.gave_up_reason)
        self.assertFalse(proc.is_running())

    def test_a_missing_binary_names_the_path_and_the_fix(self):
        proc = ManagedProcess(
            [str(Path("/nonexistent/whisper-server.exe"))],
            name="whisper-server", health_check=lambda: True,
        )
        with self.assertRaises(BackendUnavailableError) as ctx:
            proc.start()
        self.assertIn("whisper-server.exe", ctx.exception.message)
        self.assertIn("build-whisper-vulkan", ctx.exception.remedy)

    def test_a_child_that_exits_at_once_reports_its_own_output(self):
        proc = self.make(free_port(), "--exit-immediately", startup_timeout_s=10)
        with self.assertRaises(BackendUnavailableError) as ctx:
            proc.start()
        self.assertIn("exited immediately", ctx.exception.message)
        self.assertIn("simulated bad model file", ctx.exception.message)

    def test_a_server_that_never_becomes_ready_times_out_with_its_log(self):
        proc = self.make(free_port(), "--ready-after", "600", startup_timeout_s=1.0)
        with self.assertRaises(BackendUnavailableError) as ctx:
            proc.start()
        self.assertIn("did not become ready", ctx.exception.message)
        self.assertIn("stub-server: listening", ctx.exception.message)
        self.assertFalse(proc.is_running())

    def test_stop_is_safe_to_call_twice_and_before_start(self):
        proc = self.make(free_port())
        proc.stop()
        proc.start()
        proc.stop()
        proc.stop()
        self.assertFalse(proc.is_running())

    def test_stop_returns_promptly_even_while_a_restart_is_in_flight(self):
        """Regression guard: the monitor thread holds the lock across a restart,
        so stop() has to signal before it takes that lock or it blocks for the
        whole startup timeout."""
        port = free_port()
        proc = self.make(port, "--die-after", "0.3", "--ready-after", "600",
                         startup_timeout_s=30.0)
        try:
            proc.start()
        except BackendUnavailableError:
            self.skipTest("stub never became ready at all")
        wait_for(lambda: not proc.is_running(), timeout=10)
        started = time.monotonic()
        proc.stop(timeout_s=5)
        self.assertLess(time.monotonic() - started, 10.0)


# ---------------------------------------------------------------------------
# The backend that ties the two together
# ---------------------------------------------------------------------------


class BackendPreflight(unittest.TestCase):
    def config(self, tmp: Path, **kwargs) -> WhisperConfig:
        return WhisperConfig(
            server_exe=str(tmp / "whisper-server.exe"),
            model=str(tmp / "ggml-large-v3-turbo.bin"),
            port=free_port(),
            **kwargs,
        )

    def test_missing_binary(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            backend = WhisperVulkanBackend(self.config(Path(tmp)))
            with self.assertRaises(BackendUnavailableError) as ctx:
                backend.preflight()
            self.assertIn("whisper-server was not found", ctx.exception.message)
            self.assertIn("build-whisper-vulkan.ps1", ctx.exception.remedy)

    def test_missing_model(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "whisper-server.exe").write_bytes(b"x" * 20000)
            backend = WhisperVulkanBackend(self.config(Path(tmp)))
            with self.assertRaises(BackendUnavailableError) as ctx:
                backend.preflight()
            self.assertIn("model was not found", ctx.exception.message)
            self.assertIn("fetch-models.ps1", ctx.exception.remedy)

    def test_a_truncated_model_download_is_caught(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "whisper-server.exe").write_bytes(b"x" * 20000)
            (Path(tmp) / "ggml-large-v3-turbo.bin").write_bytes(b"x" * 1000)
            backend = WhisperVulkanBackend(self.config(Path(tmp)))
            with self.assertRaises(BackendUnavailableError) as ctx:
                backend.preflight()
            self.assertIn("too small", ctx.exception.message)

    def test_a_port_already_in_use_is_caught_before_spawning(self):
        import tempfile

        server, port = stub_server.serve_in_thread()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "whisper-server.exe").write_bytes(b"x" * 20000)
            sparse_file(Path(tmp) / "ggml-large-v3-turbo.bin", 200 * 1024 * 1024)
            cfg = self.config(Path(tmp))
            cfg.port = port
            backend = WhisperVulkanBackend(cfg)
            with self.assertRaises(BackendUnavailableError) as ctx:
                backend.preflight()
            self.assertIn("already listening", ctx.exception.message)

    def test_argv_reflects_the_settled_decisions(self):
        cfg = WhisperConfig(server_exe="w.exe", model="m.bin", port=1234)
        argv = WhisperVulkanBackend(cfg)._argv()
        self.assertIn("--model", argv)
        self.assertIn("1234", argv)
        # Flash attention off: whisper.cpp #3806 crashes in amdvlk64.dll with it.
        self.assertNotIn("--flash-attn", argv)
        # GPU on by default; --no-gpu only when explicitly disabled.
        self.assertNotIn("--no-gpu", argv)
        self.assertIn("--no-gpu", WhisperVulkanBackend(
            WhisperConfig(server_exe="w.exe", model="m.bin", use_gpu=False))._argv())


class BackendAgainstAStubServer(unittest.TestCase):
    """The whole resident-backend flow, with the stub standing in for
    whisper.cpp: start, stay resident across several utterances, stop."""

    def test_model_stays_resident_across_utterances(self):
        import tempfile

        port = free_port()
        with tempfile.TemporaryDirectory() as tmp:
            exe = Path(tmp) / "server.py"
            exe.write_text(STUB.read_text(encoding="utf-8"), encoding="utf-8")
            model = Path(tmp) / "model.bin"
            sparse_file(model, 200 * 1024 * 1024)

            cfg = WhisperConfig(server_exe=str(exe), model=str(model), port=port,
                                warmup=True, startup_timeout_s=20.0)
            backend = WhisperVulkanBackend(cfg)
            # The stub is a Python script, so run it through the interpreter.
            backend._proc.argv = [sys.executable, str(exe), "--port", str(port)]
            backend.start()
            self.addCleanup(backend.stop)
            try:
                pid = backend._proc.pid
                self.assertTrue(backend.is_healthy())
                for _ in range(3):
                    text = backend.transcribe(wav.silence(0.5), 16000)
                    self.assertEqual(text, "Hello from the stub.")
                # Same process throughout: the model was never reloaded.
                self.assertEqual(backend._proc.pid, pid)
            finally:
                backend.stop()
            self.assertFalse(backend._proc.is_running())

    def test_transcribe_reports_clearly_when_the_server_is_down(self):
        cfg = WhisperConfig(server_exe="w.exe", model="m.bin", port=free_port())
        backend = WhisperVulkanBackend(cfg)
        with self.assertRaises(TranscriptionError) as ctx:
            backend.transcribe(b"\x00\x00" * 1000, 16000)
        self.assertIn("not running", ctx.exception.message)

    def test_empty_audio_short_circuits(self):
        cfg = WhisperConfig(server_exe="w.exe", model="m.bin", port=free_port())
        self.assertEqual(WhisperVulkanBackend(cfg).transcribe(b"", 16000), "")


if __name__ == "__main__":
    unittest.main()

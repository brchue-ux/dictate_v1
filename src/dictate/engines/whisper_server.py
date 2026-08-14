"""HTTP client for whisper.cpp's `whisper-server`.

Endpoints used (whisper.cpp `examples/server`):

* ``GET  /health``     -> ``{"status":"ok"}`` 200, or ``{"status":"loading model"}`` 503
* ``POST /inference``  -> multipart form, field ``file`` = a WAV; returns
  ``{"text": "..."}`` when ``response_format=json``

stdlib ``urllib`` only, deliberately: the client is the piece most likely to need
debugging on a machine nobody here can reach, and a dependency-free client is one
fewer thing that can be missing or the wrong version.

**The ``text`` field is one line per segment, not one paragraph.** whisper.cpp's
``output_str`` writes ``result << speaker << text << "\\n"`` after *every*
segment (``examples/server/server.cpp``), and that string is what goes into
``{"text": ...}``. So a two-sentence utterance comes back as
``"Sentence one.\\nSentence two.\\n"``: the newlines are the server's delimiter
between segments, not punctuation Whisper chose. `_parse` therefore joins them
with a space - which is exactly what this module already did on the
``verbose_json`` branch below, where the segments arrive separately and the
delimiter is not in the way. Leaving them in is what pressed Enter in the
product owner's terminal; see `platform/line_breaks.py`.
"""

from __future__ import annotations

import json
import logging
import socket
import urllib.error
import urllib.request
import uuid

from ..errors import TranscriptionError

log = logging.getLogger(__name__)


def _multipart(fields: dict[str, str], filename: str, content: bytes) -> tuple[bytes, str]:
    """Build a multipart/form-data body. Returns (body, content_type)."""
    boundary = f"----dictate{uuid.uuid4().hex}"
    out = bytearray()
    for name, value in fields.items():
        out += f"--{boundary}\r\n".encode()
        out += f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
        out += str(value).encode("utf-8")
        out += b"\r\n"
    out += f"--{boundary}\r\n".encode()
    out += (
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: audio/wav\r\n\r\n"
    ).encode()
    out += content
    out += b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return bytes(out), f"multipart/form-data; boundary={boundary}"


class WhisperServerClient:
    """Talks to one whisper-server. Knows nothing about starting or stopping it."""

    def __init__(self, host: str, port: int, *, request_timeout_s: float = 120.0,
                 language: str = "en") -> None:
        self.host = host
        self.port = port
        self.request_timeout_s = request_timeout_s
        self.language = language

    @property
    def base_url(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"http://{host}:{self.port}"

    def port_is_open(self, timeout_s: float = 0.5) -> bool:
        """Cheap pre-check: is anything listening at all?"""
        try:
            with socket.create_connection((self.host, self.port), timeout=timeout_s):
                return True
        except OSError:
            return False

    def is_healthy(self, timeout_s: float = 2.0) -> bool:
        """True only when the server says the model is loaded and it is ready."""
        try:
            req = urllib.request.Request(f"{self.base_url}/health", method="GET")
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                if resp.status != 200:
                    return False
                body = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            # 503 while the model loads. HTTPError is also a response object, so
            # it has to be closed or it leaks the socket.
            exc.close()
            return False
        except (urllib.error.URLError, OSError, TimeoutError):
            return False
        try:
            return json.loads(body).get("status") == "ok"
        except (ValueError, AttributeError):
            # An older server without /health returning something else: as long
            # as it answered 200 on that path, treat it as up.
            return True

    def transcribe_wav(self, wav: bytes, *, beam_size: int = 1, best_of: int = 1,
                       prompt: str = "") -> str:
        """POST a WAV to /inference and return the transcript text."""
        fields = {
            "temperature": "0.0",
            "temperature_inc": "0.2",
            "response_format": "json",
            "language": self.language,
            "beam_size": str(beam_size),
            "best_of": str(best_of),
            # We paste a paragraph, not a subtitle file.
            "no_timestamps": "true",
        }
        if prompt:
            fields["prompt"] = prompt
        body, content_type = _multipart(fields, "utterance.wav", wav)
        req = urllib.request.Request(
            f"{self.base_url}/inference",
            data=body,
            method="POST",
            headers={"Content-Type": content_type, "Content-Length": str(len(body))},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.request_timeout_s) as resp:
                raw = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:400] if exc.fp else ""
            exc.close()
            raise TranscriptionError(
                f"The transcription server rejected the audio (HTTP {exc.code}). {detail}".strip(),
                "This is usually a bad model file or an out-of-memory GPU. "
                "Check the dictate log for whisper.cpp's own message.",
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise TranscriptionError(
                f"Could not reach the transcription server at {self.base_url}: {exc}",
                "The server may have crashed. dictate will try to restart it; "
                "if this keeps happening, run `dictate doctor`.",
            ) from exc
        return self._parse(raw)

    @staticmethod
    def _join_segments(text: str) -> str:
        """One line per segment -> one paragraph.

        See the module docstring: the newlines in the ``text`` field are
        whisper.cpp's delimiter between segments. Blank lines are dropped and
        each line is stripped of the leading space whisper.cpp puts on every
        segment, so the result is the sentence anybody would have written.

        Nothing else about the transcript is touched. The words, their order,
        their capitals and their punctuation are the words Whisper produced -
        which is what the cleanup pass's subsequence check is measured against,
        and what the history records as "as Whisper heard it".
        """
        lines = [line.strip() for line in text.splitlines()]
        return " ".join(line for line in lines if line)

    @classmethod
    def _parse(cls, raw: str) -> str:
        try:
            payload = json.loads(raw)
        except ValueError as exc:
            raise TranscriptionError(
                "The transcription server sent a reply that was not JSON: "
                f"{raw[:200]!r}",
                "This usually means the server binary is a different version "
                "than expected. Rebuild it with scripts/build-whisper-vulkan.ps1.",
            ) from exc
        if isinstance(payload, dict):
            if "text" in payload:
                return cls._join_segments(str(payload["text"]))
            if "error" in payload:
                raise TranscriptionError(
                    f"The transcription server reported an error: {payload['error']}",
                    "Check the dictate log for whisper.cpp's own output.",
                )
            # verbose_json shape, in case someone changes response_format.
            if "segments" in payload and isinstance(payload["segments"], list):
                return " ".join(
                    str(s.get("text", "")).strip() for s in payload["segments"]
                ).strip()
        raise TranscriptionError(
            f"The transcription server sent an unexpected reply: {raw[:200]!r}",
            "Rebuild whisper.cpp with scripts/build-whisper-vulkan.ps1 and try again.",
        )

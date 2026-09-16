"""Speech-to-text for the videos attached to a post.

Claude has no audio input, so a claim that is only spoken aloud is invisible to the
rest of the pipeline. This module turns a video's audio into text, through whichever
backend is available:

  openai - POST the audio to OpenAI's transcription endpoint. Fast, costs ~0.3c/min,
           needs OPENAI_API_KEY.
  local  - faster-whisper on this machine. Free and private, slower, needs
           `pip install faster-whisper` (the model downloads itself on first use).
  auto   - openai when a key is set, else local when the package is installed, else off.

Every failure degrades to an empty transcript. Audio is a bonus signal, never a blocker.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import aiohttp

log = logging.getLogger(__name__)

OPENAI_ENDPOINT = "https://api.openai.com/v1/audio/transcriptions"
# The endpoint caps uploads at 25MB. Mono 16kHz mp3 at 32kbps is ~4KB/s, so this
# is roughly 100 minutes of speech - far more than any post we will look at.
OPENAI_MAX_BYTES = 24 * 1024 * 1024
EXTRACT_TIMEOUT = 60
TRANSCRIBE_TIMEOUT = 180


def faster_whisper_available() -> bool:
    try:
        import faster_whisper  # noqa: F401
    except ImportError:
        return False
    return True


async def extract_audio(video: Path, destination: Path, *, max_seconds: int) -> bool:
    """Pull a small mono mp3 out of a video. False if there is no audio track."""
    from .media import ffmpeg_path

    binary = ffmpeg_path()
    if not binary:
        return False
    try:
        process = await asyncio.create_subprocess_exec(
            binary, "-v", "error", "-y",
            "-i", str(video),
            "-t", str(max_seconds),
            "-vn",              # drop the video stream
            "-ac", "1",         # mono
            "-ar", "16000",     # 16kHz is what speech models want anyway
            "-b:a", "32k",
            "-f", "mp3", str(destination),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        _, err = await asyncio.wait_for(process.communicate(), timeout=EXTRACT_TIMEOUT)
        if process.returncode != 0:
            log.debug("no audio extracted: %s", err.decode()[:200])
            return False
        return destination.exists() and destination.stat().st_size > 512
    except asyncio.TimeoutError:
        log.warning("ffmpeg timed out extracting audio")
    except Exception as exc:  # noqa: BLE001
        log.warning("audio extraction failed: %s", exc)
    return False


class Transcriber:
    """Resolves a backend once at startup, then transcribes on demand."""

    def __init__(
        self,
        backend: str,
        *,
        openai_api_key: str = "",
        openai_model: str = "gpt-4o-mini-transcribe",
        local_model: str = "base",
        max_seconds: int = 180,
        max_chars: int = 3000,
        timeout: int = 60,
    ) -> None:
        self._openai_key = openai_api_key
        self._openai_model = openai_model
        self._local_model_name = local_model
        self._max_seconds = max_seconds
        self._max_chars = max_chars
        self._timeout = timeout
        self._local_model = None
        self._local_lock = asyncio.Lock()
        self.backend = self._resolve(backend.lower().strip())

    def _resolve(self, requested: str) -> str:
        if requested in ("off", "none", "false", ""):
            return "off"
        if requested == "openai":
            if not self._openai_key:
                log.warning("TRANSCRIBE_BACKEND=openai but OPENAI_API_KEY is empty - audio is off")
                return "off"
            return "openai"
        if requested == "local":
            if not faster_whisper_available():
                log.warning(
                    "TRANSCRIBE_BACKEND=local but faster-whisper is not installed - audio is off. "
                    "Fix with: pip install faster-whisper"
                )
                return "off"
            return "local"
        # auto: an explicit key means you want the API; otherwise use what is installed.
        if self._openai_key:
            return "openai"
        if faster_whisper_available():
            return "local"
        return "off"

    @property
    def enabled(self) -> bool:
        return self.backend != "off"

    def describe(self) -> str:
        if self.backend == "openai":
            return f"openai ({self._openai_model})"
        if self.backend == "local":
            return f"local faster-whisper ({self._local_model_name})"
        return "off"

    async def transcribe(self, video: Path, session: aiohttp.ClientSession) -> str:
        """Transcript of the video's speech, or '' for anything that did not work."""
        if not self.enabled:
            return ""

        audio = video.with_suffix(".mp3")
        if not await extract_audio(video, audio, max_seconds=self._max_seconds):
            log.debug("video has no usable audio track")
            return ""

        try:
            if self.backend == "openai":
                text = await self._via_openai(audio, session)
            else:
                text = await self._via_local(audio)
        except Exception:  # noqa: BLE001 - audio never breaks a fact-check
            log.exception("transcription failed")
            return ""

        text = " ".join((text or "").split())
        if not text:
            return ""
        if len(text) > self._max_chars:
            text = text[: self._max_chars].rstrip() + " […truncated]"
        log.info("transcribed %d characters of speech via %s", len(text), self.backend)
        return text

    async def _via_openai(self, audio: Path, session: aiohttp.ClientSession) -> str:
        payload = audio.read_bytes()
        if len(payload) > OPENAI_MAX_BYTES:
            log.warning("audio is %.1fMB, over the API limit - skipping", len(payload) / 1e6)
            return ""

        form = aiohttp.FormData()
        form.add_field("file", payload, filename="audio.mp3", content_type="audio/mpeg")
        form.add_field("model", self._openai_model)

        async with session.post(
            OPENAI_ENDPOINT,
            data=form,
            headers={"Authorization": f"Bearer {self._openai_key}"},
            timeout=aiohttp.ClientTimeout(total=TRANSCRIBE_TIMEOUT),
        ) as response:
            body = await response.json(content_type=None)
            if response.status != 200:
                detail = (body or {}).get("error", {}).get("message", "") if isinstance(body, dict) else ""
                log.error("transcription API returned HTTP %s: %s", response.status, detail[:200])
                return ""
            return (body or {}).get("text", "") if isinstance(body, dict) else ""

    def _load_local(self):
        """Blocking: downloads the model on first use, then keeps it in memory."""
        from faster_whisper import WhisperModel

        log.info("loading faster-whisper '%s' (the first run downloads it)", self._local_model_name)
        return WhisperModel(self._local_model_name, device="cpu", compute_type="int8")

    async def _via_local(self, audio: Path) -> str:
        # One lock so two videos arriving together don't each load a copy of the model.
        async with self._local_lock:
            if self._local_model is None:
                self._local_model = await asyncio.to_thread(self._load_local)

        def run() -> str:
            segments, _info = self._local_model.transcribe(str(audio), vad_filter=True)
            return " ".join(segment.text.strip() for segment in segments)

        try:
            return await asyncio.wait_for(asyncio.to_thread(run), timeout=TRANSCRIBE_TIMEOUT)
        except asyncio.TimeoutError:
            log.warning("local transcription timed out after %ds", TRANSCRIBE_TIMEOUT)
            return ""

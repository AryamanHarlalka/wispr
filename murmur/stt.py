"""STT backend abstraction (Task 0c). ~/.murmur/config.toml [stt].backend
picks the engine; daemon.py's IncrementalTranscriber and whole-utterance
decode path both go through get_backend().transcribe() so the choice is a
config swap, not a rewrite. See config.stt_backend() for the benchmark
finding that keeps faster-whisper the default on this (Intel) machine.
"""
from __future__ import annotations

import json
import subprocess
import tempfile
import wave
from pathlib import Path

import numpy as np

from .config import MURMUR_HOME


class Segment:
    __slots__ = ("text", "start", "end")

    def __init__(self, text: str, start: float, end: float) -> None:
        self.text = text
        self.start = start
        self.end = end


class Backend:
    def transcribe(self, audio: np.ndarray, *, language: str,
                    initial_prompt: str, beam_size: int, vad_filter: bool,
                    condition_on_previous_text: bool) -> list[Segment]:
        raise NotImplementedError


class FasterWhisperBackend(Backend):
    """CTranslate2 int8 on CPU. The fastest kernel measured on this
    hardware (0c bench) -- default backend."""

    def __init__(self, model_name: str) -> None:
        from faster_whisper import WhisperModel
        self._model = WhisperModel(model_name, device="cpu", compute_type="int8")

    def transcribe(self, audio, *, language, initial_prompt, beam_size,
                    vad_filter, condition_on_previous_text):
        segments, _ = self._model.transcribe(
            audio, language=language, beam_size=beam_size,
            initial_prompt=initial_prompt, vad_filter=vad_filter,
            condition_on_previous_text=condition_on_previous_text,
        )
        return [Segment(s.text, s.start, s.end) for s in segments]


def _write_wav(audio: np.ndarray, path: str, sample_rate: int = 16000) -> None:
    pcm16 = np.clip(audio * 32768.0, -32768, 32767).astype(np.int16)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm16.tobytes())


class WhisperCppBackend(Backend):
    """Shells out to whisper-cli (Homebrew whisper-cpp). Confirmed working
    on this machine, but measured slower than FasterWhisperBackend at every
    model size tried (0c bench) because ggml compiles Metal OUT on Intel
    macOS and its BLAS/Accelerate CPU path loses to CTranslate2's int8
    kernels here. Kept as a config option for portability: on Apple Silicon
    this same backend gets Metal for free and should be revisited there.
    """

    _MODEL_DIR = MURMUR_HOME / "stt-models"

    def __init__(self, model_name: str) -> None:
        import shutil
        self._bin = shutil.which("whisper-cli") or self._find_brew_bin()
        if not self._bin:
            raise RuntimeError(
                "whisper-cli not found -- `brew install whisper-cpp`")
        self._vad_model = self._MODEL_DIR / "ggml-silero-v5.1.2.bin"
        self._model_path = self._resolve_model(model_name)

    @staticmethod
    def _find_brew_bin() -> str | None:
        for prefix in ("/usr/local/opt/whisper-cpp", "/opt/homebrew/opt/whisper-cpp"):
            p = Path(prefix) / "bin" / "whisper-cli"
            if p.exists():
                return str(p)
        return None

    def _resolve_model(self, model_name: str) -> Path:
        name = model_name if model_name.startswith("ggml-") else f"ggml-{model_name}"
        for cand in (self._MODEL_DIR / f"{name}.bin", self._MODEL_DIR / name):
            if cand.exists():
                return cand
        raise FileNotFoundError(
            f"ggml model not found for '{model_name}' in {self._MODEL_DIR}")

    def transcribe(self, audio, *, language, initial_prompt, beam_size,
                    vad_filter, condition_on_previous_text):
        with tempfile.TemporaryDirectory() as tmpdir:
            wav_path = str(Path(tmpdir) / "clip.wav")
            out_stem = str(Path(tmpdir) / "clip")
            _write_wav(audio, wav_path)
            args = [self._bin, "-m", str(self._model_path), "-f", wav_path,
                    "-l", language, "-bs", str(beam_size), "-np", "-nt",
                    "-oj", "-of", out_stem]
            if initial_prompt:
                args += ["--prompt", initial_prompt[:400]]
            if vad_filter and self._vad_model.exists():
                args += ["--vad", "--vad-model", str(self._vad_model)]
            r = subprocess.run(args, capture_output=True, text=True, timeout=120)
            if r.returncode != 0:
                raise RuntimeError(f"whisper-cli failed: {r.stderr[:300]}")
            with open(out_stem + ".json") as f:
                data = json.load(f)
        segs = []
        for t in data.get("transcription", []):
            segs.append(Segment(t["text"].strip(),
                                 t["offsets"]["from"] / 1000.0,
                                 t["offsets"]["to"] / 1000.0))
        return segs


class MlxWhisperBackend(Backend):
    """Apple Silicon only (MLX has no wheel for Intel macOS -- confirmed
    0c). Not usable on this machine; wired for when this code runs on an
    M-series Mac, where it's the expected winner per the original brief."""

    def __init__(self, model_name: str) -> None:
        import mlx_whisper  # noqa: F401 -- raises ImportError on Intel macOS
        self._model_repo = model_name
        self._mlx_whisper = mlx_whisper

    def transcribe(self, audio, *, language, initial_prompt, beam_size,
                    vad_filter, condition_on_previous_text):
        result = self._mlx_whisper.transcribe(
            audio, path_or_hf_repo=self._model_repo, language=language,
            initial_prompt=initial_prompt, condition_on_previous_text=condition_on_previous_text)
        return [Segment(s["text"].strip(), s["start"], s["end"])
                for s in result.get("segments", [])]


_BACKENDS = {
    "faster-whisper": FasterWhisperBackend,
    "whispercpp": WhisperCppBackend,
    "mlx-whisper": MlxWhisperBackend,
}


def build_backend(name: str, model_name: str) -> Backend:
    cls = _BACKENDS.get(name)
    if cls is None:
        raise ValueError(f"unknown stt.backend '{name}' "
                          f"(known: {', '.join(_BACKENDS)})")
    return cls(model_name)

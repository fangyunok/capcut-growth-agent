"""Render confirmed text and local media with FFmpeg, without model or TTS claims.

The template planner copies the approved wording exactly. The caller owns the
human approval record; the digest prevents accidental reuse after text changes.
The render pipeline uses the Python standard library and never installs binaries;
an already installed optional imageio-ffmpeg package can provide the executable.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import time
import unicodedata
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal


_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".ppm"}
_VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm"}
_AUDIO_EXTENSIONS = {".wav", ".mp3", ".m4a", ".aac", ".ogg", ".flac"}
_MAX_FILE_BYTES = 200 * 1024 * 1024
_ID = re.compile(r"[A-Za-z0-9_-]{1,160}\Z")


def _text(value: Any, label: str, maximum: int) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{label} must be a nonempty string of at most {maximum} characters")
    if any((ord(character) < 32 and character != "\n") or 0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise ValueError(f"{label} contains unsupported control characters")


def _identifier(value: Any, label: str) -> None:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError(f"{label} must contain only letters, numbers, underscores, or hyphens")


def _integer(value: Any, label: str, minimum: int, maximum: int) -> None:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{label} must be an integer from {minimum} to {maximum}")


def _fields(record: Any, required: set[str], optional: set[str] | None = None) -> dict[str, Any]:
    if not isinstance(record, dict):
        raise ValueError("Expected a JSON object")
    if required - record.keys() or record.keys() - required - (optional or set()):
        raise ValueError("Missing required fields or unknown fields in media schema")
    return record


def copy_digest(text: str) -> str:
    """Bind the confirmation to the exact UTF-8 text, including whitespace."""
    if not isinstance(text, str):
        raise ValueError("Copy must be a string")
    try:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()
    except UnicodeError as exc:
        raise ValueError("Copy must contain valid Unicode characters") from exc


@dataclass(frozen=True)
class ApprovedCopy:
    run_id: str
    copy_version: str
    text: str
    reviewer: str
    confirmed: bool

    def __post_init__(self) -> None:
        _identifier(self.run_id, "run_id")
        _text(self.text, "approved copy", 4000)
        _text(self.reviewer, "reviewer", 100)
        if self.confirmed is not True:
            raise ValueError("An explicit human confirmation is required")
        if not isinstance(self.copy_version, str) or self.copy_version != copy_digest(self.text):
            raise ValueError("Approved text no longer matches its confirmed copy_version digest")

    @classmethod
    def from_dict(cls, record: dict[str, Any]) -> ApprovedCopy:
        return cls(**_fields(record, {"run_id", "copy_version", "text", "reviewer", "confirmed"}))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class MediaAsset:
    asset_id: str
    path: str | Path
    kind: Literal["image", "video"]

    def __post_init__(self) -> None:
        _identifier(self.asset_id, "asset_id")
        if not isinstance(self.path, (str, Path)) or not str(self.path).strip() or "\0" in str(self.path):
            raise ValueError("Media asset path must be a nonempty local path")
        if not isinstance(self.kind, str) or self.kind not in {"image", "video"}:
            raise ValueError("Media kind must be image or video")

    @classmethod
    def from_dict(cls, record: dict[str, Any]) -> MediaAsset:
        return cls(**_fields(record, {"asset_id", "path", "kind"}))

    def to_dict(self) -> dict[str, Any]:
        return {"asset_id": self.asset_id, "path": str(self.path), "kind": self.kind}


@dataclass(frozen=True)
class StoryboardSegment:
    segment_id: str
    text: str
    duration_ms: int
    asset: MediaAsset

    def __post_init__(self) -> None:
        _identifier(self.segment_id, "segment_id")
        _text(self.text, "segment text", 1400)
        _integer(self.duration_ms, "duration_ms", 500, 15000)
        if not isinstance(self.asset, MediaAsset):
            raise ValueError("Segment asset must be a MediaAsset")

    @classmethod
    def from_dict(cls, record: dict[str, Any]) -> StoryboardSegment:
        checked = _fields(record, {"segment_id", "text", "duration_ms", "asset"})
        return cls(**{**checked, "asset": MediaAsset.from_dict(checked["asset"])})

    def to_dict(self) -> dict[str, Any]:
        return {
            "segment_id": self.segment_id, "text": self.text,
            "duration_ms": self.duration_ms, "asset": self.asset.to_dict(),
        }


@dataclass(frozen=True)
class Storyboard:
    approved_copy: ApprovedCopy
    segments: tuple[StoryboardSegment, ...]
    width: int = 720
    height: int = 1280
    fps: int = 24
    planning_mode: Literal["template"] = "template"

    def __post_init__(self) -> None:
        if not isinstance(self.approved_copy, ApprovedCopy):
            raise ValueError("Storyboard requires an ApprovedCopy")
        if not isinstance(self.segments, tuple) or len(self.segments) != 3:
            raise ValueError("Storyboard must contain exactly three immutable segments")
        if any(not isinstance(segment, StoryboardSegment) for segment in self.segments):
            raise ValueError("Invalid storyboard segment")
        if len({segment.segment_id for segment in self.segments}) != 3:
            raise ValueError("Segment IDs must be unique")
        if "".join(segment.text for segment in self.segments) != self.approved_copy.text:
            raise ValueError("Segment text must reproduce the exact approved copy in order")
        if self.planning_mode != "template":
            raise ValueError("This planner is an explicit template, not model generation")
        _integer(self.width, "width", 180, 1080)
        _integer(self.height, "height", 320, 1920)
        _integer(self.fps, "fps", 24, 30)
        if self.width % 2 or self.height % 2 or self.width * 16 != self.height * 9:
            raise ValueError("Output must use even dimensions with a 9:16 vertical aspect ratio")
        if self.fps not in {24, 25, 30}:
            raise ValueError("Supported frame rates are 24, 25, and 30")
        if sum(segment.duration_ms for segment in self.segments) > 30000:
            raise ValueError("Maximum template video duration is 30 seconds")
        seen: dict[str, MediaAsset] = {}
        for segment in self.segments:
            asset = segment.asset
            if asset.asset_id in seen and asset != seen[asset.asset_id]:
                raise ValueError("An asset ID cannot refer to different source files")
            seen[asset.asset_id] = asset

    @classmethod
    def from_dict(cls, record: dict[str, Any]) -> Storyboard:
        checked = _fields(record, {"approved_copy", "segments"}, {"width", "height", "fps", "planning_mode"})
        if not isinstance(checked["segments"], list):
            raise ValueError("segments must be a JSON array")
        return cls(**{
            **checked, "approved_copy": ApprovedCopy.from_dict(checked["approved_copy"]),
            "segments": tuple(StoryboardSegment.from_dict(item) for item in checked["segments"]),
        })

    def to_dict(self) -> dict[str, Any]:
        return {
            "approved_copy": self.approved_copy.to_dict(),
            "segments": [segment.to_dict() for segment in self.segments],
            "width": self.width, "height": self.height, "fps": self.fps,
            "planning_mode": self.planning_mode,
        }


def _split_copy(text: str) -> tuple[str, str, str]:
    if sum(not character.isspace() for character in text) < 3:
        raise ValueError("Three segments require at least three non-whitespace characters")
    cuts: list[int] = []
    start = 0
    for part in (1, 2):
        target = round(len(text) * part / 3)
        remaining = 3 - part
        candidates = [index for index in range(start + 1, len(text))
                      if text[start:index].strip()
                      and sum(not char.isspace() for char in text[index:]) >= remaining]
        nearby = [index for index in candidates if abs(index - target) <= max(8, len(text) // 8)
                  and (text[index - 1].isspace() or text[index - 1] in ".!?。！？；;，,")]
        cut = min(nearby or candidates, key=lambda index: abs(index - target))
        cuts.append(cut)
        start = cut
    return text[:cuts[0]], text[cuts[0]:cuts[1]], text[cuts[1]:]


def build_template_storyboard(
    approved_copy: ApprovedCopy,
    assets: list[MediaAsset] | tuple[MediaAsset, ...],
    *,
    duration_seconds: float = 15,
    width: int = 720,
    height: int = 1280,
    fps: int = 24,
) -> Storyboard:
    """Deterministically divide approved wording; repeat 1-2 supplied assets.

    This function makes no API calls, generates no new claims or images, and
    does not decide whether the human approval was substantively correct.
    """
    if not isinstance(approved_copy, ApprovedCopy):
        raise ValueError("approved_copy must be an ApprovedCopy")
    if not isinstance(assets, (list, tuple)) or not 1 <= len(assets) <= 3:
        raise ValueError("Supply one to three local media assets")
    if any(not isinstance(asset, MediaAsset) for asset in assets):
        raise ValueError("Every asset must be a MediaAsset")
    if len({asset.asset_id for asset in assets}) != len(assets):
        raise ValueError("Input asset IDs must be unique")
    if type(duration_seconds) not in {int, float} or not math.isfinite(duration_seconds) or not 3 <= duration_seconds <= 30:
        raise ValueError("duration_seconds must be a finite number from 3 to 30")
    total_ms = round(duration_seconds * 1000)
    per_segment, remainder = divmod(total_ms, 3)
    text_parts = _split_copy(approved_copy.text)
    segments = tuple(StoryboardSegment(
        segment_id=f"segment-{index + 1}", text=part,
        duration_ms=per_segment + (1 if index < remainder else 0),
        asset=assets[index % len(assets)],
    ) for index, part in enumerate(text_parts))
    return Storyboard(approved_copy, segments, width, height, fps)


class _RenderError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _local_file(path: str | Path, extensions: set[str], asset_root: Path | None = None) -> Path:
    if str(path).startswith(("\\\\", "//")):
        raise _RenderError("invalid_asset", "Network share paths are unsupported; supply a local file")
    supplied = Path(path).expanduser()
    if asset_root is not None and not supplied.is_absolute():
        supplied = asset_root / supplied
    try:
        resolved = supplied.resolve(strict=True)
        if str(resolved).startswith(("\\\\", "//")):
            raise _RenderError("invalid_asset", "Network share paths are unsupported; supply a local file")
        if asset_root is not None and not resolved.is_relative_to(asset_root):
            raise _RenderError("asset_outside_root", "A media file resolves outside the allowed asset directory")
        if not resolved.is_file() or resolved.suffix.lower() not in extensions:
            raise _RenderError("invalid_asset", "A media file has an unsupported type or is not a regular file")
        if not 0 < resolved.stat().st_size <= _MAX_FILE_BYTES:
            raise _RenderError("invalid_asset", "Media files must be nonempty and no larger than 200 MiB each")
        return resolved
    except _RenderError:
        raise
    except (OSError, RuntimeError) as exc:
        raise _RenderError("asset_unavailable", "A local media file cannot be read") from exc


def find_ffmpeg(ffmpeg: str | Path | None = None) -> str:
    """Locate FFmpeg without installation; explicit broken settings are errors.

    With no explicit argument/environment setting, prefer PATH, then the binary
    bundled by an already installed optional imageio-ffmpeg distribution.
    """
    configured = ffmpeg is not None or "GROWTH_FFMPEG" in os.environ
    supplied = str(ffmpeg) if ffmpeg is not None else os.getenv("GROWTH_FFMPEG", "ffmpeg")
    if not supplied.strip() or "\0" in supplied or Path(supplied).suffix.lower() in {".bat", ".cmd"}:
        raise _RenderError("ffmpeg_unavailable", "Configure an FFmpeg binary, not a shell command or batch file")
    found = shutil.which(supplied)
    if found is not None:
        return str(Path(found).resolve())
    if not configured:
        try:
            import imageio_ffmpeg

            bundled = imageio_ffmpeg.get_ffmpeg_exe()
            if isinstance(bundled, str) and Path(bundled).suffix.lower() not in {".bat", ".cmd"}:
                found = shutil.which(bundled)
                if found is not None:
                    return str(Path(found).resolve())
        except (ImportError, RuntimeError, OSError):
            pass
    raise _RenderError(
        "ffmpeg_unavailable",
        "FFmpeg is unavailable; configure GROWTH_FFMPEG/PATH or install the optional media extra",
    )


def _executable(ffmpeg: str | Path | None) -> str:
    return find_ffmpeg(ffmpeg)


def _font(font_path: str | Path | None, text: str) -> Path:
    explicit = font_path if font_path is not None else os.getenv("GROWTH_FONT_PATH")
    if explicit:
        return _local_file(explicit, {".ttf", ".otf", ".ttc"})
    windows = Path(os.getenv("WINDIR", "C:/Windows")) / "Fonts"
    cjk = any(unicodedata.east_asian_width(character) in {"W", "F"} for character in text)
    candidates = [
        windows / "msyh.ttc", windows / "msjh.ttc",
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"),
    ]
    if not cjk:
        candidates += [
            windows / "segoeui.ttf",
            Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
            Path("/System/Library/Fonts/Supplemental/Arial.ttf"),
        ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    raise _RenderError("font_unavailable", "Set GROWTH_FONT_PATH to a local font supporting the caption language")


def _caption(text: str, max_units: int = 34) -> str:
    """Conservative Unicode line wrapping; preserve words where practical."""
    lines: list[str] = []
    line = ""
    units = 0
    for character in text:
        if character == "\n":
            lines.append(line)
            line, units = "", 0
            continue
        width = 0 if unicodedata.combining(character) else (2 if unicodedata.east_asian_width(character) in {"W", "F"} else 1)
        if units + width > max_units:
            split = line.rfind(" ")
            if split >= max_units // 3:
                lines.append(line[:split])
                line = line[split + 1:]
                units = sum(2 if unicodedata.east_asian_width(item) in {"W", "F"} else 1 for item in line)
            else:
                lines.append(line)
                line, units = "", 0
        line += character
        units += width
    if line or not lines:
        lines.append(line)
    if len(lines) > 8:
        raise _RenderError("caption_too_long", "A segment needs more than eight caption lines; shorten and reconfirm the copy")
    return "\n".join(lines)


def _timestamp(milliseconds: int) -> str:
    seconds, millis = divmod(milliseconds, 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02},{millis:03}"


def _frame_count(segment: StoryboardSegment, fps: int) -> int:
    return max(1, round(segment.duration_ms * fps / 1000))


def _write_srt(storyboard: Storyboard, run_dir: Path) -> None:
    frames = 0
    entries = []
    for index, segment in enumerate(storyboard.segments, 1):
        start = round(frames * 1000 / storyboard.fps)
        frames += _frame_count(segment, storyboard.fps)
        end = round(frames * 1000 / storyboard.fps)
        entries.append(f"{index}\n{_timestamp(start)} --> {_timestamp(end)}\n{segment.text.strip()}\n")
    (run_dir / "captions.srt").write_text("\n".join(entries), encoding="utf-8")


def _invoke(binary: str, args: list[str], stage: str, run_dir: Path, deadline: float, trace: list[dict[str, Any]]) -> None:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise _RenderError("render_timeout", "The total FFmpeg execution time budget was exhausted")
    started = time.monotonic()
    creation_flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    with (run_dir / "ffmpeg.log").open("a", encoding="utf-8") as log:
        log.write(f"\n[{stage}]\n")
        log.flush()
        try:
            result = subprocess.run(
                [binary, "-hide_banner", "-nostdin", "-loglevel", "error", *args],
                cwd=run_dir, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                shell=False, timeout=remaining, check=False, creationflags=creation_flags,
            )
        except subprocess.TimeoutExpired as exc:
            trace.append({"stage": stage, "duration_ms": round((time.monotonic() - started) * 1000), "error": "timeout"})
            raise _RenderError("render_timeout", "FFmpeg exceeded the total rendering time budget") from exc
        except OSError as exc:
            raise _RenderError("ffmpeg_unavailable", "FFmpeg could not be started") from exc
    trace.append({"stage": stage, "duration_ms": round((time.monotonic() - started) * 1000), "returncode": result.returncode})
    if result.returncode != 0:
        raise _RenderError("ffmpeg_failed", f"FFmpeg failed during {stage}; inspect the saved ffmpeg.log")


def render_video(
    storyboard: Storyboard,
    *,
    output_root: Path,
    asset_root: Path | None = None,
    audio_path: str | Path | None = None,
    font_path: str | Path | None = None,
    ffmpeg: str | Path | None = None,
    timeout_seconds: float = 120,
) -> tuple[dict[str, Any], Path]:
    """Render one unique run, persisting a failed record on dependency/media errors.

    Asset paths are relative to asset_root when provided, and resolved symlinks
    must remain below that root. The caller must perform its own user/session
    access checks. Source clip audio is discarded; optional audio is user-supplied
    and trimmed or silence-padded to the video duration. There is no TTS.
    """
    if not isinstance(storyboard, Storyboard):
        raise ValueError("storyboard must be a validated Storyboard")
    if type(timeout_seconds) not in {int, float} or not math.isfinite(timeout_seconds) or not 1 <= timeout_seconds <= 600:
        raise ValueError("timeout_seconds must be a finite number from 1 to 600")
    output_root = Path(output_root).resolve()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    run_id = f"{stamp}-media-{uuid.uuid4().hex[:12]}"
    run_dir = output_root / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    frames = sum(_frame_count(segment, storyboard.fps) for segment in storyboard.segments)
    duration = frames / storyboard.fps
    bundle: dict[str, Any] = {
        "run_id": run_id, "status": "rendering", "planning_mode": "template",
        "source_audit_run_id": storyboard.approved_copy.run_id,
        "approved_copy_version": storyboard.approved_copy.copy_version,
        "reviewer": storyboard.approved_copy.reviewer,
        "audio_mode": "user_supplied" if audio_path is not None else "silent",
        "tts_generated": False, "published": False,
        "requested_output": {"width": storyboard.width, "height": storyboard.height, "fps": storyboard.fps,
                             "frames": frames, "duration_seconds": duration, "video_codec": "h264"},
        "artifacts": {"storyboard": "storyboard.json", "captions": "captions.srt", "log": "ffmpeg.log"},
        "trace": [],
    }
    (run_dir / "storyboard.json").write_text(json.dumps(storyboard.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (run_dir / "ffmpeg.log").write_text("No FFmpeg process output yet.\n", encoding="utf-8")
    _write_srt(storyboard, run_dir)
    started = time.monotonic()
    deadline = started + timeout_seconds
    try:
        try:
            allowed_root = Path(asset_root).resolve(strict=True) if asset_root is not None else None
        except (OSError, RuntimeError) as exc:
            raise _RenderError("invalid_asset_root", "The allowed asset root must be an existing directory") from exc
        if allowed_root is not None and not allowed_root.is_dir():
            raise _RenderError("invalid_asset_root", "The allowed asset root must be an existing directory")
        resolved_sources = [
            _local_file(segment.asset.path, _IMAGE_EXTENSIONS if segment.asset.kind == "image" else _VIDEO_EXTENSIONS, allowed_root)
            for segment in storyboard.segments
        ]
        audio = _local_file(audio_path, _AUDIO_EXTENSIONS, allowed_root) if audio_path is not None else None
        captions = [_caption(segment.text) for segment in storyboard.segments]
        binary = _executable(ffmpeg)
        font = _font(font_path, storyboard.approved_copy.text)
        safe_font = "caption-font" + font.suffix.lower()
        shutil.copyfile(font, run_dir / safe_font)
        _invoke(binary, ["-version"], "version", run_dir, deadline, bundle["trace"])
        version_lines = (run_dir / "ffmpeg.log").read_text(encoding="utf-8", errors="replace").splitlines()
        bundle["ffmpeg_version"] = next((line[:300] for line in version_lines if line.startswith("ffmpeg version")), "unreported")
        asset_details: dict[str, Any] = {}
        for index, segment in enumerate(storyboard.segments, 1):
            source = resolved_sources[index - 1]
            asset_details[segment.asset.asset_id] = {"name": source.name, "kind": segment.asset.kind, "bytes": source.stat().st_size}
            caption_file = f"caption-{index}.txt"
            (run_dir / caption_file).write_text(captions[index - 1], encoding="utf-8")
            font_size = max(10, storyboard.width // 22)
            margin = max(16, storyboard.height // 20)
            filters = (
                f"scale={storyboard.width}:{storyboard.height}:force_original_aspect_ratio=decrease,"
                f"pad={storyboard.width}:{storyboard.height}:(ow-iw)/2:(oh-ih)/2:color=black,"
                f"setsar=1,fps={storyboard.fps},"
                f"drawtext=fontfile={safe_font}:textfile={caption_file}:expansion=none:"
                f"fontsize={font_size}:fontcolor=white:line_spacing={max(3, font_size // 4)}:"
                f"box=1:boxcolor=black@0.65:boxborderw={max(6, font_size // 2)}:"
                f"x=(w-text_w)/2:y=h-text_h-{margin}"
            )
            input_args = (["-loop", "1", "-framerate", str(storyboard.fps)] if segment.asset.kind == "image" else ["-stream_loop", "-1"])
            _invoke(binary, [
                *input_args, "-protocol_whitelist", "file,pipe", "-i", str(source),
                "-map", "0:v:0", "-an", "-vf", filters,
                "-frames:v", str(_frame_count(segment, storyboard.fps)), "-c:v", "libx264",
                "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
                "-threads", "2", "-movflags", "+faststart", "-n", f"segment-{index}.mp4",
            ], f"segment-{index}", run_dir, deadline, bundle["trace"])
        bundle["source_assets"] = list(asset_details.values())
        (run_dir / "concat.txt").write_text("".join(f"file 'segment-{index}.mp4'\n" for index in range(1, 4)), encoding="ascii")
        output_args = ["-f", "concat", "-safe", "1", "-protocol_whitelist", "file,pipe", "-i", "concat.txt"]
        if audio is not None:
            bundle["source_audio"] = {"name": audio.name, "bytes": audio.stat().st_size, "alignment": "start_at_zero_trim_or_silence_pad"}
            output_args += ["-protocol_whitelist", "file,pipe", "-i", str(audio), "-map", "0:v:0", "-map", "1:a:0",
                            "-c:v", "copy", "-c:a", "aac", "-b:a", "128k", "-af", "apad", "-t", f"{duration:.6f}"]
        else:
            output_args += ["-map", "0:v:0", "-an", "-c:v", "copy"]
        output_args += ["-movflags", "+faststart", "-n", "video.partial.mp4"]
        _invoke(binary, output_args, "concat", run_dir, deadline, bundle["trace"])
        provisional = run_dir / "video.partial.mp4"
        if not provisional.is_file() or provisional.stat().st_size == 0:
            raise _RenderError("output_missing", "FFmpeg did not create a nonempty MP4")
        verify_args = ["-xerror", "-protocol_whitelist", "file,pipe", "-i", "video.partial.mp4", "-map", "0:v:0"]
        verify_args += ["-map", "0:a:0"] if audio_path is not None else ["-an"]
        _invoke(binary, [*verify_args, "-f", "null", "-"], "verify_decode", run_dir, deadline, bundle["trace"])
        provisional.rename(run_dir / "video.mp4")
        bundle["status"] = "completed"
        bundle["artifacts"]["video"] = "video.mp4"
        bundle["verification"] = {"full_decode_passed": True, "output_bytes": (run_dir / "video.mp4").stat().st_size,
                                  "dimensions_independently_probed": False}
    except _RenderError as exc:
        bundle["status"] = "failed"
        bundle["error"] = {"code": exc.code, "message": str(exc)}
    except (OSError, RuntimeError) as exc:
        bundle["status"] = "failed"
        bundle["error"] = {"code": "filesystem_error", "message": "A local file or directory operation failed", "type": type(exc).__name__}
    bundle["duration_ms"] = round((time.monotonic() - started) * 1000)
    bundle["at_utc"] = datetime.now(timezone.utc).isoformat()
    (run_dir / "media_bundle.json").write_text(json.dumps(bundle, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return bundle, run_dir

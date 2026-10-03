"""Media boundaries use mocked FFmpeg; a real decode test runs when installed."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import types
import unittest
import wave
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from growth_agent.media import (
    ApprovedCopy, MediaAsset, Storyboard, StoryboardSegment,
    _font, build_template_storyboard, copy_digest, find_ffmpeg, render_video,
)


def _approved(text: str = "Show your product. Explain its features. Review before sharing.") -> ApprovedCopy:
    return ApprovedCopy("audit-test", copy_digest(text), text, "Test editor", True)


def _ppm(path: Path) -> Path:
    # An original, tiny test image needs neither Pillow nor downloaded assets.
    path.write_bytes(b"P6\n16 16\n255\n" + bytes((20, 110, 160)) * 16 * 16)
    return path


class MediaSchemaTests(unittest.TestCase):
    def test_confirmation_requires_literal_true_and_exact_version(self) -> None:
        for value in (False, 1, "true", None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                replace(_approved(), confirmed=value)
        with self.assertRaisesRegex(ValueError, "digest"):
            replace(_approved(), text="Different approved wording.")
        with self.assertRaises(ValueError):
            copy_digest("invalid surrogate: \ud800")

    def test_schema_rejects_unknown_fields_and_type_coercion(self) -> None:
        with self.assertRaises(ValueError):
            ApprovedCopy.from_dict({**_approved().to_dict(), "publish": True})
        with self.assertRaises(ValueError):
            MediaAsset.from_dict({"asset_id": "a", "path": "photo.png", "kind": {}})
        asset = MediaAsset("image-1", "photo.png", "image")
        storyboard = build_template_storyboard(_approved(), [asset])
        record = storyboard.to_dict()
        record["segments"][0]["duration_ms"] = True
        with self.assertRaises(ValueError):
            Storyboard.from_dict(record)
        with self.assertRaises(ValueError):
            replace(storyboard, width=721)
        with self.assertRaises(ValueError):
            replace(storyboard, planning_mode="llm")

    def test_template_preserves_approved_text_and_is_roundtrip_serializable(self) -> None:
        for text in (
            "Show your product. Explain its features. Review before sharing.",
            "展示商品资料。查看并核对产品功能。人工确认文案后再导出视频。",
            "First sentence.\nSecond sentence. Third sentence.",
            "abc", "A B C",
        ):
            with self.subTest(text=text):
                storyboard = build_template_storyboard(_approved(text), [MediaAsset("a", "example.png", "image")])
                self.assertEqual("".join(segment.text for segment in storyboard.segments), text)
                self.assertEqual(sum(segment.duration_ms for segment in storyboard.segments), 15000)
                self.assertEqual(Storyboard.from_dict(storyboard.to_dict()), storyboard)
                self.assertEqual(storyboard.planning_mode, "template")

    def test_modified_segment_and_conflicting_asset_identity_are_rejected(self) -> None:
        asset = MediaAsset("a", "example.png", "image")
        storyboard = build_template_storyboard(_approved(), [asset])
        with self.assertRaisesRegex(ValueError, "exact approved copy"):
            replace(storyboard, segments=(replace(storyboard.segments[0], text="Added guarantee."), *storyboard.segments[1:]))
        with self.assertRaisesRegex(ValueError, "different source files"):
            replace(storyboard, segments=(storyboard.segments[0], replace(
                storyboard.segments[1], asset=replace(asset, path="other.png")
            ), storyboard.segments[2]))

    def test_budget_and_asset_count_are_bounded(self) -> None:
        asset = MediaAsset("a", "example.png", "image")
        for seconds in (True, float("nan"), float("inf"), 0, 31, "15"):
            with self.subTest(seconds=seconds), self.assertRaises(ValueError):
                build_template_storyboard(_approved(), [asset], duration_seconds=seconds)
        for assets in ([], [asset] * 4, [asset, asset]):
            with self.subTest(assets=assets), self.assertRaises(ValueError):
                build_template_storyboard(_approved(), assets)
        with self.assertRaises(ValueError):
            build_template_storyboard(_approved("Hi"), [asset])

    def test_optional_bundled_binary_is_used_only_after_unconfigured_path_failure(self) -> None:
        fake_package = types.SimpleNamespace(get_ffmpeg_exe=lambda: "bundled-ffmpeg.exe")
        environment = {key: value for key, value in os.environ.items() if key != "GROWTH_FFMPEG"}
        with patch.dict(os.environ, environment, clear=True), patch.dict("sys.modules", {"imageio_ffmpeg": fake_package}), patch(
            "growth_agent.media.shutil.which", side_effect=lambda value: None if value == "ffmpeg" else "/tmp/bundled-ffmpeg.exe",
        ):
            self.assertEqual(Path(find_ffmpeg()).name, "bundled-ffmpeg.exe")
        with patch.dict("sys.modules", {"imageio_ffmpeg": fake_package}), patch("growth_agent.media.shutil.which", return_value=None):
            with self.assertRaises(Exception):
                find_ffmpeg("explicit-missing.exe")
            with patch.dict(os.environ, {"GROWTH_FFMPEG": "explicit-missing.exe"}):
                with self.assertRaises(Exception):
                    find_ffmpeg()


class MediaRenderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.assets = self.root / "素材 with spaces"
        self.assets.mkdir()
        self.image = _ppm(self.assets / "商品 image.ppm")
        self.font = self.root / "font.ttf"
        self.font.write_bytes(b"mock font; never passed to a real process")
        self.storyboard = build_template_storyboard(_approved(), [MediaAsset("a", self.image.name, "image")])

    @staticmethod
    def _fake_process(command: list[str], **kwargs) -> subprocess.CompletedProcess:
        if "-version" in command:
            kwargs["stdout"].write("ffmpeg version mock-test-build\n")
            kwargs["stdout"].flush()
        if command[-1].endswith(".mp4"):
            (Path(kwargs["cwd"]) / command[-1]).write_bytes(b"mock-MP4-test-data")
        return subprocess.CompletedProcess(command, 0)

    def _render(self, storyboard: Storyboard | None = None, **kwargs):
        return render_video(
            storyboard or self.storyboard, output_root=self.root / "runs",
            asset_root=self.assets, font_path=self.font, ffmpeg="ffmpeg", **kwargs,
        )

    def test_success_uses_argument_lists_literal_captions_and_unique_output_dirs(self) -> None:
        text = "Use literal %{pts} wording. Check $() and quotes ' safely. Export approved text."
        storyboard = build_template_storyboard(_approved(text), [MediaAsset("a", self.image.name, "image")])
        with patch("growth_agent.media.shutil.which", return_value=str(self.root / "ffmpeg.exe")), patch(
            "growth_agent.media.subprocess.run", side_effect=self._fake_process,
        ) as process:
            bundle, run_dir = self._render(storyboard)
            second, second_dir = self._render(storyboard)
        self.assertEqual(bundle["status"], "completed")
        self.assertEqual(second["status"], "completed")
        self.assertNotEqual(run_dir, second_dir)
        self.assertTrue((run_dir / "video.mp4").is_file())
        self.assertEqual(bundle["audio_mode"], "silent")
        self.assertFalse(bundle["tts_generated"])
        self.assertTrue(bundle["verification"]["full_decode_passed"])
        self.assertFalse(bundle["verification"]["dimensions_independently_probed"])
        self.assertEqual(bundle["approved_copy_version"], copy_digest(text))
        self.assertIn("%{pts}", (run_dir / "caption-1.txt").read_text(encoding="utf-8"))
        self.assertEqual(json.loads((run_dir / "media_bundle.json").read_text(encoding="utf-8"))["status"], "completed")
        for call in process.call_args_list:
            command = call.args[0]
            self.assertIsInstance(command, list)
            self.assertFalse(call.kwargs["shell"])
            self.assertGreater(call.kwargs["timeout"], 0)
            self.assertIn("-nostdin", command)
            if "-vf" in command:
                self.assertIn("expansion=none", command[command.index("-vf") + 1])
                self.assertIn(str(self.image.resolve()), command)
                self.assertNotIn(text, command)
                self.assertIn("file,pipe", command)
        self.assertEqual(len(process.call_args_list), 12)  # Version, 3 parts, concat, decode; twice.

    def test_user_audio_is_mapped_and_silence_padded_with_no_tts_claim(self) -> None:
        audio = self.assets / "voice.wav"
        audio.write_bytes(b"placeholder for mocked process")
        with patch("growth_agent.media.shutil.which", return_value="ffmpeg.exe"), patch(
            "growth_agent.media.subprocess.run", side_effect=self._fake_process,
        ) as process:
            bundle, _ = self._render(audio_path=audio.name)
        self.assertEqual(bundle["status"], "completed")
        self.assertEqual(bundle["audio_mode"], "user_supplied")
        self.assertFalse(bundle["tts_generated"])
        concat = process.call_args_list[4].args[0]
        self.assertIn("1:a:0", concat)
        self.assertIn("apad", concat)
        self.assertIn("-t", concat)

    def test_existing_video_asset_is_looped_and_original_audio_discarded(self) -> None:
        clip = self.assets / "clip.mp4"
        clip.write_bytes(b"placeholder for mocked process")
        storyboard = build_template_storyboard(_approved(), [MediaAsset("clip", clip.name, "video")])
        with patch("growth_agent.media.shutil.which", return_value="ffmpeg.exe"), patch(
            "growth_agent.media.subprocess.run", side_effect=self._fake_process,
        ) as process:
            bundle, _ = self._render(storyboard)
        self.assertEqual(bundle["status"], "completed")
        segment = process.call_args_list[1].args[0]
        self.assertIn("-stream_loop", segment)
        self.assertIn("-an", segment)

    def test_relative_path_escape_rejected_before_starting_ffmpeg(self) -> None:
        outside = _ppm(self.root / "outside.ppm")
        storyboard = build_template_storyboard(_approved(), [MediaAsset("a", "../outside.ppm", "image")])
        with patch("growth_agent.media.subprocess.run") as process:
            bundle, run_dir = self._render(storyboard)
        self.assertTrue(outside.exists())
        self.assertEqual(bundle["error"]["code"], "asset_outside_root")
        self.assertFalse(process.called)
        self.assertTrue((run_dir / "media_bundle.json").is_file())

    def test_symlink_escape_rejected_when_platform_supports_symlinks(self) -> None:
        outside = _ppm(self.root / "outside.ppm")
        link = self.assets / "link.ppm"
        try:
            link.symlink_to(outside)
        except OSError:
            self.skipTest("Platform does not allow creating a test symlink")
        storyboard = build_template_storyboard(_approved(), [MediaAsset("a", link.name, "image")])
        with patch("growth_agent.media.subprocess.run") as process:
            bundle, _ = self._render(storyboard)
        self.assertEqual(bundle["error"]["code"], "asset_outside_root")
        self.assertFalse(process.called)

    def test_asset_type_missing_file_and_audio_escape_fail_before_rendering(self) -> None:
        unsupported = self.assets / "secret.txt"
        unsupported.write_text("not a media asset", encoding="utf-8")
        cases = [
            ("secret.txt", {}, "invalid_asset"),
            ("missing.png", {}, "asset_unavailable"),
            (self.image.name, {"audio_path": "../outside.wav"}, "asset_outside_root"),
        ]
        (self.root / "outside.wav").write_bytes(b"test audio")
        for source, kwargs, expected in cases:
            with self.subTest(source=source, kwargs=kwargs), patch("growth_agent.media.subprocess.run") as process:
                storyboard = build_template_storyboard(_approved(), [MediaAsset("a", source, "image")])
                bundle, _ = self._render(storyboard, **kwargs)
                self.assertEqual(bundle["error"]["code"], expected)
                self.assertFalse(process.called)

    def test_missing_ffmpeg_and_batch_command_report_a_saved_failure(self) -> None:
        with patch("growth_agent.media.shutil.which", return_value=None):
            bundle, run_dir = self._render()
        self.assertEqual(bundle["status"], "failed")
        self.assertEqual(bundle["error"]["code"], "ffmpeg_unavailable")
        self.assertFalse((run_dir / "video.mp4").exists())
        self.assertTrue(all((run_dir / artifact).is_file() for artifact in bundle["artifacts"].values()))
        with patch.dict(os.environ, {"GROWTH_FFMPEG": "unsafe.cmd"}):
            failed, _ = render_video(self.storyboard, output_root=self.root / "runs", asset_root=self.assets)
        self.assertEqual(failed["error"]["code"], "ffmpeg_unavailable")

    def test_timeout_and_nonzero_exit_preserve_error_artifacts(self) -> None:
        for outcome, expected in (
            (subprocess.TimeoutExpired("ffmpeg", 1), "render_timeout"),
            (subprocess.CompletedProcess("ffmpeg", 2), "ffmpeg_failed"),
        ):
            with self.subTest(expected=expected), patch("growth_agent.media.shutil.which", return_value="ffmpeg.exe"), patch(
                "growth_agent.media.subprocess.run", side_effect=outcome if isinstance(outcome, Exception) else None,
                return_value=outcome if not isinstance(outcome, Exception) else None,
            ):
                bundle, run_dir = self._render()
            self.assertEqual(bundle["status"], "failed")
            self.assertEqual(bundle["error"]["code"], expected)
            self.assertNotIn("video", bundle["artifacts"])
            self.assertTrue((run_dir / "storyboard.json").is_file())
            self.assertTrue((run_dir / "ffmpeg.log").is_file())

    def test_no_output_or_failed_decode_never_exposes_completed_video(self) -> None:
        def no_output(command, **kwargs):
            return subprocess.CompletedProcess(command, 0)

        def invalid_decode(command, **kwargs):
            result = self._fake_process(command, **kwargs)
            return subprocess.CompletedProcess(command, 1 if "null" in command else result.returncode)

        for fake, expected in ((no_output, "output_missing"), (invalid_decode, "ffmpeg_failed")):
            with self.subTest(expected=expected), patch("growth_agent.media.shutil.which", return_value="ffmpeg.exe"), patch(
                "growth_agent.media.subprocess.run", side_effect=fake,
            ):
                bundle, run_dir = self._render()
            self.assertEqual(bundle["error"]["code"], expected)
            self.assertFalse((run_dir / "video.mp4").exists())

    def test_overlong_captions_stop_before_model_or_process_work(self) -> None:
        storyboard = build_template_storyboard(_approved("A long caption fragment. " * 90), [MediaAsset("a", self.image.name, "image")])
        with patch("growth_agent.media.subprocess.run") as process:
            bundle, _ = self._render(storyboard)
        self.assertEqual(bundle["error"]["code"], "caption_too_long")
        self.assertFalse(process.called)

    def test_frame_rounded_srt_matches_rendered_duration(self) -> None:
        storyboard = build_template_storyboard(_approved(), [MediaAsset("a", self.image.name, "image")], duration_seconds=5.1)
        with patch("growth_agent.media.shutil.which", return_value=None):
            bundle, run_dir = self._render(storyboard)
        captions = (run_dir / "captions.srt").read_text(encoding="utf-8")
        frames = sum(round(segment.duration_ms * storyboard.fps / 1000) for segment in storyboard.segments)
        self.assertEqual(bundle["requested_output"]["duration_seconds"], frames / storyboard.fps)
        self.assertIn("00:00:00,000 -->", captions)
        self.assertEqual(captions.count(" --> "), 3)


class RealFFmpegMediaTests(unittest.TestCase):
    def test_real_images_and_user_audio_render_to_a_decodable_mp4(self) -> None:
        try:
            binary = find_ffmpeg()
        except Exception:
            self.skipTest("FFmpeg is not installed/configured")
        try:
            font = _font(None, "First. Second. Third.")
        except Exception:
            self.skipTest("A supported font is not available")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            image = _ppm(root / "original image.ppm")
            audio = root / "short silence.wav"
            with wave.open(str(audio), "wb") as file:
                file.setnchannels(1)
                file.setsampwidth(2)
                file.setframerate(8000)
                file.writeframes(b"\0\0" * 4000)
            storyboard = build_template_storyboard(
                _approved("First. Second. Third."), [MediaAsset("a", image, "image")],
                duration_seconds=3, width=360, height=640,
            )
            bundle, run_dir = render_video(storyboard, output_root=root / "runs", asset_root=root, font_path=font, audio_path=audio, ffmpeg=binary)
            self.assertEqual(bundle["status"], "completed", bundle.get("error"))
            self.assertGreater((run_dir / "video.mp4").stat().st_size, 100)
            self.assertTrue(bundle["verification"]["full_decode_passed"])
            self.assertEqual(bundle["audio_mode"], "user_supplied")


if __name__ == "__main__":
    unittest.main()

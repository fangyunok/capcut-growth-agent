"""Create labeled, code-generated placeholder media for reproducibility checks."""

from __future__ import annotations

import json
import uuid
from dataclasses import replace
from pathlib import Path

from .media import ApprovedCopy, MediaAsset, build_template_storyboard, copy_digest, render_video


def media_demo(output_root: Path, *, ffmpeg: str | None = None) -> tuple[dict, Path]:
    assets_dir = output_root / ("demo-assets-" + uuid.uuid4().hex[:12])
    assets_dir.mkdir(parents=True, exist_ok=False)
    assets = []
    for index, color in enumerate(((38, 65, 100), (36, 107, 112), (121, 70, 122)), 1):
        target = assets_dir / f"placeholder-{index}.ppm"
        width, height = 180, 320
        pixels = bytearray()
        for row in range(height):
            for column in range(width):
                inside = 35 < column < 145 and 55 < row < 195
                pixels.extend((min(255, channel + 55) if inside else channel) for channel in color)
        target.write_bytes(f"P6\n{width} {height}\n255\n".encode("ascii") + pixels)
        assets.append(MediaAsset(f"placeholder-{index}", target.name, "image"))
    text = "This is a template media demo. Three placeholder images illustrate the workflow. No model-generated copy or voice is used."
    approved = ApprovedCopy("template-demo", copy_digest(text), text, "demo-fixture", True)
    storyboard = replace(build_template_storyboard(approved, assets, duration_seconds=3), width=360, height=640)
    (assets_dir / "assets.json").write_text(json.dumps([asset.to_dict() for asset in assets], indent=2), encoding="utf-8")
    metadata, run_dir = render_video(storyboard, output_root=output_root, asset_root=assets_dir, ffmpeg=ffmpeg)
    (run_dir / "demo_notice.json").write_text(json.dumps({
        "scope": "Template media integration example with code-generated placeholders",
        "model_used": False, "tts_used": False, "actual_product_claims": False,
        "asset_directory": str(assets_dir),
    }, indent=2), encoding="utf-8")
    return metadata, run_dir

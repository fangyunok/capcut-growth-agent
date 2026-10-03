# 确认文案到竖屏视频

媒体扩展使用本地图片或已有短片，把**人工确认的具体文案版本**分成三段、加上字幕并用 FFmpeg 导出 9:16 MP4。分镜是明确标注的规则模板，逐段文字拼接后必须与确认文案完全一致。它没有调用模型规划分镜，没有生成图像，也没有自动发布。

默认输出是 **720 × 1280、24 fps、15 秒、无声视频**。可提供自己的配音或背景音文件；程序从音频开头开始，截断或补静音到视频长度。该行为不表示逐字配音对齐；输出记录为 `audio_mode=user_supplied`、`tts_generated=false`。没有音频时记录为 `audio_mode=silent`，原短片的声音也会被移除。

## 运行条件

- Python 3.11+；渲染主流程只用标准库，可选依赖仅用于发现所附 FFmpeg 二进制。
- 可运行的 FFmpeg，需要 `libx264` 编码器和 `drawtext` 滤镜。可设置 `GROWTH_FFMPEG` 为可执行文件完整路径，不能写成一整段 shell 命令。没有显式配置时先在 PATH 查找，再使用已经安装的可选 `imageio-ffmpeg==0.6.0` 所附二进制；可通过 `pip install -e ".[media]"` 安装该项目可选依赖。程序运行时不会下载或安装 FFmpeg，显式配置错误时也不会偷偷换用另一份程序。
- 本地字幕字体，可通过 `GROWTH_FONT_PATH` 或函数参数 `font_path` 指定 `.ttf`、`.otf`、`.ttc`。默认尝试 Windows 微软雅黑、Noto CJK；英文另尝试 Segoe UI、DejaVu Sans、Arial。中文需要包含中文字符的字体，最终字幕仍应目视核对。
- 自己拥有使用权的本地素材。图片支持 PNG/JPEG/WebP/BMP/PPM；已有短片支持 MP4/MOV/MKV/WebM；音频支持 WAV/MP3/M4A/AAC/OGG/FLAC。后缀检查不能代替媒体解码，实际格式错误由 FFmpeg 失败记录说明。

系统字体由运行者提供，不会提交到 GitHub。运行目录含本机文件路径、审核者、字幕和渲染日志；发布仓库时应保持 `runs/` 不进入 Git。

## Python 接口

`ApprovedCopy` 的 `run_id` 对应原审校运行；`copy_version` 是确认文字精确 UTF-8 编码的 SHA256；`confirmed` 必须是布尔值 `True`。改动文字后旧摘要将失效。调用者必须读取真实人工确认记录并检查用户权限；仅构造一个对象不等于已经做过事实审查。

```python
from pathlib import Path
from growth_agent.media import (
    ApprovedCopy, MediaAsset, build_template_storyboard, copy_digest, render_video,
)

# 这段文字应从已经保存的人工确认记录读取。
text = "展示产品资料。核对功能与来源。确认文案后导出视频。"
approved = ApprovedCopy(
    run_id="your-audit-run-id",
    copy_version=copy_digest(text),
    text=text,
    reviewer="your-editor-name",
    confirmed=True,
)
asset_dir = Path("my-assets").resolve()
storyboard = build_template_storyboard(
    approved,
    [MediaAsset("image-1", "product.png", "image")],
    duration_seconds=15,
)
bundle, run_dir = render_video(
    storyboard,
    output_root=Path("runs"),
    asset_root=asset_dir,
    # audio_path="recorded-voice.wav",  # 相对 asset_root；可选。
    # font_path=Path("C:/Windows/Fonts/msyh.ttc"),
    timeout_seconds=120,
)
print(bundle["status"], run_dir)
if bundle["status"] == "failed":
    print(bundle["error"]["code"], bundle["error"]["message"])
```

以上是调用方式示例，不是已经确认的产品功能样例。审校入口与媒体入口由 CLI / 网页读取保存的批准记录串联。网页若使用该函数，应在工作线程或独立 worker 执行，避免同步 FFmpeg 阻塞异步请求处理。

输入 1–3 个素材时，按顺序重复素材补足三段；不额外添加新文案或 CTA。短片从头开始，太短则循环，太长则取到该段结束。画面等比例缩小并居中补黑边，字幕位于下方带背景的区域。

`find_ffmpeg(ffmpeg=None)` 暴露同一解析规则，供环境检查使用；返回找到的可执行文件完整路径，找不到则抛出带 `code=ffmpeg_unavailable` 的错误。

可使用 `Storyboard.from_dict(record)` 加载严格 JSON 结构：顶层必需 `approved_copy` 与 `segments`；可选 `width`、`height`、`fps`、`planning_mode`。每段含 `segment_id`、`text`、`duration_ms`、`asset`，素材含 `asset_id`、`path`、`kind`。未知字段、字符串数字、布尔时长、非三段分镜、被改过的确认文案均拒绝。

## 产物与错误

每次调用创建唯一目录，保留 `storyboard.json`、`captions.srt`、`media_bundle.json`、`ffmpeg.log`。FFmpeg 尚未启动时日志明确说明没有进程输出。完成时暴露 `video.mp4`；失败时没有完成视频产物。`video.partial.mp4` 和三段中间 MP4 可能留下，便于查看失败阶段。日志可能包含本机文件名，公开演示时先检查内容。

状态为 `completed` 或 `failed`，主要错误码为 `ffmpeg_unavailable`、`font_unavailable`、`asset_unavailable`、`asset_outside_root`、`invalid_asset`、`invalid_asset_root`、`caption_too_long`、`render_timeout`、`ffmpeg_failed`、`output_missing`、`filesystem_error`。输入对象或超时参数不符合 schema 时直接抛出 `ValueError`；输出目录不可写时可能抛出 `OSError`。调用者应展示这类输入 / 环境错误。

提供 `asset_root` 时，相对素材路径以该目录解析，绝对路径也必须处于其下；解析符号链接后仍要符合此限制。图片、短片、音频均执行相同检查。字体是单独配置的本机资源。单个素材必须非空且不超过 200 MiB，不支持网络共享路径。

总时长 3–30 秒，24/25/30 fps，偶数像素的 9:16 画幅，最大 1080 × 1920。字幕按照字符宽度保守换行，任何一段超过八行就要求缩短文案并重新确认。各段时长按完整帧取整，SRT 使用同一帧时间轴；实际目标总时长可能与用户毫秒输入略有差别。

所有 FFmpeg 进程使用参数数组、`shell=False`、关闭 stdin、总时间预算、限定本地文件协议；文案写入独立 UTF-8 文件并关闭 `drawtext` 表达式展开。成功前对完整输出执行 FFmpeg 解码检查。元数据 `requested_output` 记录编码参数和帧数；`verification.full_decode_passed` 记录解码验证。当前没有独立使用 ffprobe 验证画幅和时长，因此 `dimensions_independently_probed=false`，不能把配置值写成另一次测量结果。

实现参考：[FFmpeg drawtext、scale、pad、fps 与 apad 官方文档](https://ffmpeg.org/ffmpeg-filters.html)、[concat demuxer 官方文档](https://ffmpeg.org/ffmpeg-formats.html#concat)。字幕支持取决于本机 FFmpeg 的构建能力。

## 验证范围

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_media.py -v
```

模拟进程测试检查确认摘要、文本一致性、严格字段、路径越界、可创建符号链接时的越界、无覆盖输出、传入音频、丢弃原短片音频、超时、非零退出码与完整解码失败。模拟进程产生的文件只存在于临时测试目录，不能当成真实视频。

检测到可执行 FFmpeg 和可用字体时，另运行一个真实三段图片视频＋用户音频测试，使用标准库创建的 PPM 与 WAV 测试素材，实际导出并解码 MP4。环境缺少 FFmpeg 时明确跳过，不伪造通过。投递演示还应检查中文字体、字幕遮挡、静音 / 音频标识与具体确认版本。

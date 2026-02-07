import logging
import subprocess
from dataclasses import dataclass

from core.config import settings

logger = logging.getLogger(__name__)


@dataclass
class Segment:
    start: float
    end: float

    @property
    def duration(self) -> float:
        return self.end - self.start


@dataclass
class VideoOptions:
    """Processing options passed from the API request."""
    zoom_level: int = 1400
    fade_duration: float = 1.0
    width: int = 1080
    height: int = 1920
    mirror: bool = False


# ---------------------------------------------------------------------------
# Phase 1: TRIM & PREP
# ---------------------------------------------------------------------------

def _build_trim_filters(segments: list[Segment], fps: int) -> str:
    """
    For each segment i, trim video+audio, reset timestamps, and normalize
    format so xfade receives consistent inputs (fps, pixel format, sample rate).

    Produces: [v0], [a0], [v1], [a1], ...
    """
    filters: list[str] = []
    for i, seg in enumerate(segments):
        filters.append(
            f"[0:v]trim=start={seg.start:.3f}:end={seg.end:.3f},"
            f"setpts=PTS-STARTPTS,setsar=1,"
            f"fps={fps},format=yuv420p[v{i}]"
        )
        filters.append(
            f"[0:a]atrim=start={seg.start:.3f}:end={seg.end:.3f},"
            f"asetpts=PTS-STARTPTS,"
            f"aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo[a{i}]"
        )
    return ";\n".join(filters)


# ---------------------------------------------------------------------------
# Phase 2: CHAIN FADE (xfade + acrossfade)
# ---------------------------------------------------------------------------

def _build_fade_filters(
    segments: list[Segment],
    fade_duration: float,
) -> tuple[str, str, str]:
    """
    Chain xfade (video) and acrossfade (audio) between trimmed segments.

    Offset formula: offset_k = sum(durations[0..k]) - (k+1) * fade_duration

    Returns (filter_string, final_video_label, final_audio_label).
    If only 1 segment, returns empty filters with labels [v0], [a0].
    """
    n = len(segments)
    if n == 1:
        return "", "v0", "a0"

    filters: list[str] = []
    cumulative_duration = 0.0
    last_video = "v0"
    last_audio = "a0"

    for k in range(n - 1):
        cumulative_duration += segments[k].duration
        offset = cumulative_duration - (k + 1) * fade_duration
        offset = max(0, offset)

        next_video = f"vfade{k}"
        next_audio = f"afade{k}"

        # Video crossfade
        filters.append(
            f"[{last_video}][v{k + 1}]xfade=transition=fade:"
            f"duration={fade_duration:.3f}:offset={offset:.3f}"
            f"[{next_video}]"
        )

        # Audio crossfade
        filters.append(
            f"[{last_audio}][a{k + 1}]acrossfade=d={fade_duration:.3f}:"
            f"c1=tri:c2=tri"
            f"[{next_audio}]"
        )

        last_video = next_video
        last_audio = next_audio

    return ";\n".join(filters), last_video, last_audio


# ---------------------------------------------------------------------------
# Phase 3: VISUAL STYLE (Mobile 9:16 - blur bg + zoom fg + overlay)
# ---------------------------------------------------------------------------

def _build_visual_style_filter(
    video_label: str,
    opts: VideoOptions,
) -> tuple[str, str]:
    """
    Apply the 9:16 mobile visual style:
      1. (optional) hflip for mirror
      2. split into bg_src and fg_src
      3. Background: scale small -> boxblur -> scale large (CPU trick)
      4. Foreground: scale zoom -> crop center
      5. Overlay foreground on background

    Returns (filter_string, final_video_label).
    """
    out_w = opts.width
    out_h = opts.height
    bg_small_w = out_w // 4
    bg_small_h = out_h // 4

    parts: list[str] = []

    # Mirror (hflip) if requested — applied before split
    if opts.mirror:
        mirror_label = "vmirror"
        parts.append(f"[{video_label}]hflip[{mirror_label}]")
        video_label = mirror_label

    # Split the stream
    parts.append(f"[{video_label}]split[bg_src][fg_src]")

    # Background: scale down -> blur -> scale up
    parts.append(
        f"[bg_src]scale={bg_small_w}:{bg_small_h}:"
        f"force_original_aspect_ratio=increase,"
        f"crop={bg_small_w}:{bg_small_h},"
        f"boxblur=luma_radius=20:luma_power=2:"
        f"chroma_radius=20:chroma_power=2,"
        f"scale={out_w}:{out_h}[bg]"
    )

    # Foreground: zoom scale -> crop center
    parts.append(
        f"[fg_src]scale={opts.zoom_level}:-2,"
        f"crop={out_w}:ih:(iw-{out_w})/2:0[fg]"
    )

    # Overlay: foreground centered on blurred background
    parts.append(
        f"[bg][fg]overlay=x=0:y=(H-h)/2,"
        f"scale={out_w}:{out_h},"
        f"setsar=1[vout]"
    )

    full = ";\n".join(parts)
    return full, "vout"


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------

def build_filter_complex(
    segments: list[Segment],
    opts: VideoOptions,
    fps: int,
) -> tuple[str, str, str]:
    """
    Build the complete filter_complex string.

    Returns (filter_complex, final_video_label, final_audio_label).
    """
    trim = _build_trim_filters(segments, fps)
    fade, video_label, audio_label = _build_fade_filters(segments, opts.fade_duration)
    style, final_video = _build_visual_style_filter(video_label, opts)

    parts = [trim]
    if fade:
        parts.append(fade)
    parts.append(style)

    return ";\n".join(parts), final_video, audio_label


def process_video(
    input_path: str,
    output_path: str,
    segments: list[Segment],
    opts: VideoOptions | None = None,
) -> str:
    """
    Execute FFmpeg to cut, crossfade, and style the video.

    Returns output_path on success.
    Raises RuntimeError if FFmpeg fails.
    """
    if not segments:
        raise ValueError("No segments provided")

    if opts is None:
        opts = VideoOptions()

    fps = settings.output_fps

    filter_complex, video_label, audio_label = build_filter_complex(segments, opts, fps)

    cmd = [
        settings.ffmpeg_path,
        "-y",
        "-i", input_path,
        "-filter_complex", filter_complex,
        "-map", f"[{video_label}]",
        "-map", f"[{audio_label}]",
        "-c:v", "libx264",
        "-preset", "medium",
        "-crf", "23",
        "-b:v", settings.video_bitrate,
        "-c:a", "aac",
        "-b:a", settings.audio_bitrate,
        "-r", str(fps),
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        output_path,
    ]

    logger.info(f"FFmpeg command: {' '.join(cmd)}")
    logger.debug(f"Filter complex:\n{filter_complex}")

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=600,
    )

    if result.returncode != 0:
        logger.error(f"FFmpeg stderr:\n{result.stderr}")
        raise RuntimeError(
            f"FFmpeg failed (code {result.returncode}): "
            f"{result.stderr[-1000:]}"
        )

    logger.info(f"FFmpeg processing complete: {output_path}")
    return output_path

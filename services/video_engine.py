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
    layout: str = "blur_zoom"  # blur_zoom | vertical | horizontal | blur
    zoom_level: int = 1400
    fade_duration: float = 1.0
    width: int = 1080
    height: int = 1920
    mirror: bool = False
    speed: float = 1.0          # 1.0 = normal, 1.05 = 5% faster (copyright avoidance)
    color_filter: bool = False  # subtle color grading to alter visual fingerprint
    pitch_shift: float = 1.0    # 1.0 = normal, 1.03 = 3% higher pitch (no speed change)
    background_noise: float = 0.0  # 0.0 = off, 0.03 = 3% pink noise volume
    ghost_effect: bool = False  # periodic brightness pulse to break temporal fingerprint
    dynamic_zoom: bool = False  # subtle oscillating zoom (0-2%)


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
# Phase 2.5: SPEED — alter playback speed (copyright avoidance)
# ---------------------------------------------------------------------------

def _apply_speed(
    video_label: str,
    audio_label: str,
    speed: float,
) -> tuple[str, str, str]:
    """
    Apply speed change to video and audio streams.
    Returns (filter_string, new_video_label, new_audio_label).
    If speed is 1.0, returns empty string and original labels.
    """
    if speed == 1.0:
        return "", video_label, audio_label

    vout = "vspeed"
    aout = "aspeed"
    filters = [
        f"[{video_label}]setpts=PTS/{speed:.4f}[{vout}]",
        f"[{audio_label}]atempo={speed:.4f}[{aout}]",
    ]
    return ";\n".join(filters), vout, aout


# ---------------------------------------------------------------------------
# Phase 2.6: PITCH SHIFT — change audio pitch without speed change
# ---------------------------------------------------------------------------

def _apply_pitch_shift(
    audio_label: str,
    pitch_shift: float,
) -> tuple[str, str]:
    """
    Shift audio pitch without changing playback speed.
    Uses asetrate (relabel sample rate) + atempo (compensate speed) + aresample.
    Returns (filter_string, new_audio_label).
    If pitch_shift is 1.0, returns empty string and original label.
    """
    if pitch_shift == 1.0:
        return "", audio_label

    aout = "apitch"
    sample_rate = 44100
    new_rate = int(sample_rate * pitch_shift)
    atempo_factor = 1.0 / pitch_shift
    filter_str = (
        f"[{audio_label}]asetrate={new_rate},"
        f"atempo={atempo_factor:.6f},"
        f"aresample={sample_rate}[{aout}]"
    )
    return filter_str, aout


# ---------------------------------------------------------------------------
# Phase 5: DYNAMIC ZOOM — subtle oscillating zoom (copyright avoidance)
# ---------------------------------------------------------------------------

def _apply_dynamic_zoom(
    video_label: str,
    opts: VideoOptions,
    fps: int,
) -> tuple[str, str]:
    """
    Apply subtle pulsing zoom (0-2%) with 5-second sine wave cycle.
    Returns (filter_string, new_video_label).
    Skipped for horizontal layout (unknown output dimensions).
    """
    if not opts.dynamic_zoom:
        return "", video_label
    if opts.layout == "horizontal":
        logger.info("Dynamic zoom skipped for horizontal layout")
        return "", video_label

    vout = "vzoom"
    w = opts.width
    h = opts.height
    filter_str = (
        f"[{video_label}]zoompan="
        f"z='1.02+0.01*sin(2*3.14159*t/5)':"
        f"x='int(iw/2-(iw/zoom/2))':"
        f"y='int(ih/2-(ih/zoom/2))':"
        f"d=1:s={w}x{h}:fps={fps},"
        f"format=yuv420p[{vout}]"
    )
    return filter_str, vout


# ---------------------------------------------------------------------------
# Phase 6: GHOST EFFECT — periodic brightness pulse (copyright avoidance)
# ---------------------------------------------------------------------------

def _apply_ghost_effect(
    video_label: str,
    ghost_effect: bool,
) -> tuple[str, str]:
    """
    Apply periodic subtle brightness pulse to break temporal fingerprint.
    +6% brightness for ~67ms (2 frames at 30fps) every 11 seconds.
    Returns (filter_string, new_video_label).
    """
    if not ghost_effect:
        return "", video_label

    vout = "vghost"
    filter_str = (
        f"[{video_label}]eq=brightness=0.06:"
        f"enable=lt(mod(t\\,11)\\,0.067)[{vout}]"
    )
    return filter_str, vout


# ---------------------------------------------------------------------------
# Phase 7: BACKGROUND NOISE — pink noise layer (copyright avoidance)
# ---------------------------------------------------------------------------

def _apply_background_noise(
    audio_label: str,
    noise_level: float,
) -> tuple[str, str]:
    """
    Mix subtle pink noise into audio to create unique sonic fingerprint.
    Uses anoisesrc (source filter) + amix within filter_complex.
    Returns (filter_string, new_audio_label).
    """
    if noise_level <= 0:
        return "", audio_label

    aout = "anoise"
    # Scale noise amplitude directly, mix with defaults, then boost to compensate
    # amix default normalization divides by number of inputs (2), so volume=2 restores level
    filter_str = (
        f"anoisesrc=color=pink:r=44100:a={noise_level:.4f}:d=600,"
        f"aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo[bg_noise];\n"
        f"[{audio_label}][bg_noise]amix=inputs=2:duration=first,"
        f"volume=2.0[{aout}]"
    )
    return filter_str, aout


# ---------------------------------------------------------------------------
# Phase 3: VISUAL STYLE — layout-based dispatch
# ---------------------------------------------------------------------------

def _apply_mirror(video_label: str, parts: list[str], mirror: bool) -> str:
    """Apply hflip if mirror is requested. Returns the (possibly new) label."""
    if mirror:
        mirror_label = "vmirror"
        parts.append(f"[{video_label}]hflip[{mirror_label}]")
        return mirror_label
    return video_label


def _build_style_blur_zoom(video_label: str, opts: VideoOptions) -> tuple[str, str]:
    """
    Default layout: blur background + zoomed foreground + overlay (9:16).
    """
    out_w = opts.width
    out_h = opts.height
    bg_small_w = out_w // 4
    bg_small_h = out_h // 4

    parts: list[str] = []
    video_label = _apply_mirror(video_label, parts, opts.mirror)

    parts.append(f"[{video_label}]split[bg_src][fg_src]")

    parts.append(
        f"[bg_src]scale={bg_small_w}:{bg_small_h}:"
        f"force_original_aspect_ratio=increase,"
        f"crop={bg_small_w}:{bg_small_h},"
        f"boxblur=luma_radius=20:luma_power=2:"
        f"chroma_radius=20:chroma_power=2,"
        f"scale={out_w}:{out_h}[bg]"
    )

    parts.append(
        f"[fg_src]scale={opts.zoom_level}:-2,"
        f"crop={out_w}:ih:(iw-{out_w})/2:0[fg]"
    )

    parts.append(
        f"[bg][fg]overlay=x=0:y=(H-h)/2,"
        f"scale={out_w}:{out_h},"
        f"setsar=1[vout]"
    )

    return ";\n".join(parts), "vout"


def _build_style_vertical(video_label: str, opts: VideoOptions) -> tuple[str, str]:
    """
    Simple vertical crop from center — no blur background.
    Scales to fill height, then crops width centered.
    """
    out_w = opts.width
    out_h = opts.height

    parts: list[str] = []
    video_label = _apply_mirror(video_label, parts, opts.mirror)

    parts.append(
        f"[{video_label}]scale=-2:{out_h}:"
        f"force_original_aspect_ratio=increase,"
        f"crop={out_w}:{out_h},"
        f"setsar=1[vout]"
    )

    return ";\n".join(parts), "vout"


def _build_style_horizontal(video_label: str, opts: VideoOptions) -> tuple[str, str]:
    """
    Keep original aspect ratio and resolution — no visual transformation.
    """
    parts: list[str] = []
    video_label = _apply_mirror(video_label, parts, opts.mirror)

    parts.append(f"[{video_label}]setsar=1[vout]")

    return ";\n".join(parts), "vout"


def _build_style_blur(video_label: str, opts: VideoOptions) -> tuple[str, str]:
    """
    Blur background + original video (no zoom) centered.
    Like blur_zoom but foreground scales to fit width without extra zoom.
    """
    out_w = opts.width
    out_h = opts.height
    bg_small_w = out_w // 4
    bg_small_h = out_h // 4

    parts: list[str] = []
    video_label = _apply_mirror(video_label, parts, opts.mirror)

    parts.append(f"[{video_label}]split[bg_src][fg_src]")

    parts.append(
        f"[bg_src]scale={bg_small_w}:{bg_small_h}:"
        f"force_original_aspect_ratio=increase,"
        f"crop={bg_small_w}:{bg_small_h},"
        f"boxblur=luma_radius=20:luma_power=2:"
        f"chroma_radius=20:chroma_power=2,"
        f"scale={out_w}:{out_h}[bg]"
    )

    parts.append(
        f"[fg_src]scale={out_w}:-2[fg]"
    )

    parts.append(
        f"[bg][fg]overlay=x=0:y=(H-h)/2,"
        f"scale={out_w}:{out_h},"
        f"setsar=1[vout]"
    )

    return ";\n".join(parts), "vout"


def _build_visual_style_filter(
    video_label: str,
    opts: VideoOptions,
) -> tuple[str, str]:
    """Dispatch to the correct visual style builder based on layout, then apply color filter."""
    builders = {
        "blur_zoom": _build_style_blur_zoom,
        "vertical": _build_style_vertical,
        "horizontal": _build_style_horizontal,
        "blur": _build_style_blur,
    }
    builder = builders.get(opts.layout, _build_style_blur_zoom)
    style_str, final_label = builder(video_label, opts)

    # Apply color grading if enabled (copyright avoidance)
    if opts.color_filter:
        color_label = "vcolor"
        color_filter = (
            f"[{final_label}]eq=brightness=0.04:contrast=1.06:saturation=1.12"
            f"[{color_label}]"
        )
        style_str = style_str + ";\n" + color_filter
        final_label = color_label

    return style_str, final_label


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

    Chain: Trim → Fade → Speed → Pitch Shift → [Mirror → Layout → Color]
           → Dynamic Zoom → Ghost Effect → [vout]
           → Background Noise → [aout]

    Returns (filter_complex, final_video_label, final_audio_label).
    """
    trim = _build_trim_filters(segments, fps)
    fade, video_label, audio_label = _build_fade_filters(segments, opts.fade_duration)

    # Speed change (applied after fade, before visual style)
    speed_str, video_label, audio_label = _apply_speed(
        video_label, audio_label, opts.speed
    )

    # Pitch shift (audio only — after speed)
    pitch_str, audio_label = _apply_pitch_shift(audio_label, opts.pitch_shift)

    # Visual style (mirror + layout + color filter)
    style, video_label = _build_visual_style_filter(video_label, opts)

    # Dynamic zoom (after visual style, before ghost effect)
    zoom_str, video_label = _apply_dynamic_zoom(video_label, opts, fps)

    # Ghost effect (last video step)
    ghost_str, video_label = _apply_ghost_effect(video_label, opts.ghost_effect)

    # Background noise (last audio step)
    noise_str, audio_label = _apply_background_noise(audio_label, opts.background_noise)

    # Assemble all filter parts
    parts = [trim]
    for p in [fade, speed_str, pitch_str, style, zoom_str, ghost_str, noise_str]:
        if p:
            parts.append(p)

    return ";\n".join(parts), video_label, audio_label


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


def burn_captions(
    video_path: str,
    ass_path: str,
    output_path: str,
) -> str:
    """
    Burn ASS subtitles into video (second-pass FFmpeg).

    Re-encodes video with subtitle overlay; audio is copied without re-encoding.
    """
    # Escape special characters in path for FFmpeg filter syntax
    escaped_ass = ass_path.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")

    cmd = [
        settings.ffmpeg_path,
        "-y",
        "-i", video_path,
        "-vf", f"ass='{escaped_ass}'",
        "-c:v", "libx264",
        "-preset", "medium",
        "-crf", "23",
        "-b:v", settings.video_bitrate,
        "-c:a", "copy",
        "-movflags", "+faststart",
        output_path,
    ]

    logger.info(f"Burning captions: {ass_path} -> {output_path}")

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=300,
    )

    if result.returncode != 0:
        logger.error(f"FFmpeg caption burn stderr:\n{result.stderr}")
        raise RuntimeError(
            f"FFmpeg caption burn failed (code {result.returncode}): "
            f"{result.stderr[-1000:]}"
        )

    logger.info(f"Captions burned successfully: {output_path}")
    return output_path

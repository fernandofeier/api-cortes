
import sys
import logging
from dataclasses import dataclass
from typing import List, Tuple

# Mocking the structures from video_engine.py

@dataclass
class Segment:
    start: float
    end: float

    @property
    def duration(self) -> float:
        return self.end - self.start

@dataclass
class VideoOptions:
    layout: str = "blur_zoom"
    zoom_level: int = 1400
    fade_duration: float = 1.0
    width: int = 1080
    height: int = 1920
    mirror: bool = False
    speed: float = 1.0
    color_filter: bool = False
    pitch_shift: float = 1.0
    background_noise: float = 0.0
    ghost_effect: bool = False
    dynamic_zoom: bool = False
    captions: bool = True
    caption_style: str = "box"
    max_clips: int = 1

def _build_trim_filters(segments: List[Segment], fps: int) -> str:
    filters: List[str] = []
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

def _build_fade_filters(
    segments: List[Segment],
    fade_duration: float,
) -> Tuple[str, str, str]:
    n = len(segments)
    if n == 1:
        return "", "v0", "a0"

    filters: List[str] = []
    cumulative_duration = 0.0
    last_video = "v0"
    last_audio = "a0"

    for k in range(n - 1):
        cumulative_duration += segments[k].duration
        offset = cumulative_duration - (k + 1) * fade_duration
        offset = max(0, offset)

        next_video = f"vfade{k}"
        next_audio = f"afade{k}"

        filters.append(
            f"[{last_video}][v{k + 1}]xfade=transition=fade:"
            f"duration={fade_duration:.3f}:offset={offset:.3f}"
            f"[{next_video}]"
        )

        filters.append(
            f"[{last_audio}][a{k + 1}]acrossfade=d={fade_duration:.3f}:"
            f"c1=tri:c2=tri"
            f"[{next_audio}]"
        )

        last_video = next_video
        last_audio = next_audio

    return ";\n".join(filters), last_video, last_audio

def _apply_speed(video_label: str, audio_label: str, speed: float) -> Tuple[str, str, str]:
    if speed == 1.0:
        return "", video_label, audio_label

    vout = "vspeed"
    aout = "aspeed"
    filters = [
        f"[{video_label}]setpts=PTS/{speed:.4f}[{vout}]",
        f"[{audio_label}]atempo={speed:.4f}[{aout}]",
    ]
    return ";\n".join(filters), vout, aout

def _apply_pitch_shift(audio_label: str, pitch_shift: float) -> Tuple[str, str]:
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

def _apply_dynamic_zoom(video_label: str, opts: VideoOptions, fps: int) -> Tuple[str, str]:
    if not opts.dynamic_zoom:
        return "", video_label
    if opts.layout == "horizontal":
        return "", video_label

    vout = "vzoom"
    w = opts.width
    h = opts.height
    filter_str = (
        f"[{video_label}]zoompan="
        f"z='1.01+0.01*sin(2*3.14159*t/5)':"
        f"x='iw/2-(iw/zoom/2)':"
        f"y='ih/2-(ih/zoom/2)':"
        f"d=1:s={w}x{h}:fps={fps},"
        f"format=yuv420p[{vout}]"
    )
    return filter_str, vout

def _apply_ghost_effect(video_label: str, ghost_effect: bool) -> Tuple[str, str]:
    if not ghost_effect:
        return "", video_label

    vout = "vghost"
    filter_str = (
        f"[{video_label}]eq=brightness=0.06:"
        f"enable=lt(mod(t\\,11)\\,0.067)[{vout}]"
    )
    return filter_str, vout

def _apply_background_noise(audio_label: str, noise_level: float) -> Tuple[str, str]:
    if noise_level <= 0:
        return "", audio_label

    aout = "anoise"
    filter_str = (
        f"anoisesrc=type=pink:r=44100:a={noise_level:.4f}:d=600,"
        f"aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo[bg_noise];\n"
        f"[{audio_label}][bg_noise]amix=inputs=2:duration=first,"
        f"volume=2.0[{aout}]"
    )
    return filter_str, aout

def _apply_mirror(video_label: str, parts: List[str], mirror: bool) -> str:
    if mirror:
        mirror_label = "vmirror"
        parts.append(f"[{video_label}]hflip[{mirror_label}]")
        return mirror_label
    return video_label

def _build_style_blur_zoom(video_label: str, opts: VideoOptions) -> Tuple[str, str]:
    out_w = opts.width
    out_h = opts.height
    bg_small_w = out_w // 4
    bg_small_h = out_h // 4

    parts: List[str] = []
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

def _build_visual_style_filter(video_label: str, opts: VideoOptions) -> Tuple[str, str]:
    builders = {
        "blur_zoom": _build_style_blur_zoom,
    }
    builder = builders.get(opts.layout, _build_style_blur_zoom)
    style_str, final_label = builder(video_label, opts)

    if opts.color_filter:
        color_label = "vcolor"
        color_filter = (
            f"[{final_label}]eq=brightness=0.04:contrast=1.06:saturation=1.12"
            f"[{color_label}]"
        )
        style_str = style_str + ";\n" + color_filter
        final_label = color_label

    return style_str, final_label

def build_filter_complex(
    segments: List[Segment],
    opts: VideoOptions,
    fps: int,
) -> Tuple[str, str, str]:
    trim = _build_trim_filters(segments, fps)
    
    # We call fade but since we only mock Segments list of length 1, fade returns empty, as expected
    fade, video_label, audio_label = _build_fade_filters(segments, opts.fade_duration)

    # Note: _build_fade_filters returns video_label="v0", audio_label="a0" for 1 segment.
    # But trim produces [v0], [a0] so that's correct linkage.

    speed_str, video_label, audio_label = _apply_speed(
        video_label, audio_label, opts.speed
    )

    pitch_str, audio_label = _apply_pitch_shift(audio_label, opts.pitch_shift)

    style, video_label = _build_visual_style_filter(video_label, opts)

    zoom_str, video_label = _apply_dynamic_zoom(video_label, opts, fps)

    ghost_str, video_label = _apply_ghost_effect(video_label, opts.ghost_effect)

    noise_str, audio_label = _apply_background_noise(audio_label, opts.background_noise)

    parts = [trim]
    for p in [fade, speed_str, pitch_str, style, zoom_str, ghost_str, noise_str]:
        if p:
            parts.append(p)

    return ";\n".join(parts), video_label, audio_label

if __name__ == "__main__":
    segments = [Segment(0.0, 10.0)]
    opts = VideoOptions(
        captions=True,
        caption_style="box",
        layout="blur_zoom",
        max_clips=1,
        mirror=True,
        zoom_level=1400,
        speed=1.07,
        color_filter=True,
        pitch_shift=1.03,
        background_noise=0.03,
        ghost_effect=True,
        dynamic_zoom=True
    )
    fps = 30
    fc, v, a = build_filter_complex(segments, opts, fps)
    print(fc)

"""
Face tracking service for dynamic crop positioning in vertical video cuts.

Pipeline:
  1. Probe video (ffprobe) → get dimensions and fps
  2. Extract sampled frames (FFmpeg pipe, downscaled to 320px) per segment
  3. Detect faces (MediaPipe) → select dominant (largest bbox)
  4. Smooth trajectory (EMA + gap fill + speed clamp)
  5. Map to output timeline (trim/fade/speed accounting)
  6. Simplify keyframes (Ramer-Douglas-Peucker)
  7. Build FFmpeg crop x expression (piecewise linear interpolation)

Non-fatal: returns None on any failure → caller uses center crop.
"""

import asyncio
import json
import logging
import math
import subprocess
from dataclasses import dataclass

from core.config import settings

logger = logging.getLogger(__name__)

# Lazy imports — face tracking is optional (mediapipe may not be installed)
try:
    import mediapipe as mp
    import numpy as np

    _AVAILABLE = True
except ImportError:
    _AVAILABLE = False


# ------------------------------------------------------------------
# Data structures
# ------------------------------------------------------------------

@dataclass
class CropKeyframe:
    time: float    # seconds on output timeline
    x_norm: float  # face center x, normalized 0.0–1.0


@dataclass
class CropTrajectory:
    keyframes: list[CropKeyframe]
    crop_x_expr: str = ""  # ready-to-use FFmpeg expression for crop x


# ------------------------------------------------------------------
# 1. Probe video
# ------------------------------------------------------------------

def _probe_video(path: str) -> tuple[int, int, float]:
    """Return (width, height, fps) via ffprobe."""
    cmd = [
        "ffprobe", "-v", "quiet",
        "-print_format", "json",
        "-show_streams", "-select_streams", "v:0",
        path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {result.stderr[:500]}")

    stream = json.loads(result.stdout)["streams"][0]
    w = int(stream["width"])
    h = int(stream["height"])
    num, den = stream.get("r_frame_rate", "30/1").split("/")
    fps = float(num) / float(den) if float(den) != 0 else 30.0
    return w, h, fps


# ------------------------------------------------------------------
# 2. Extract frames via FFmpeg pipe
# ------------------------------------------------------------------

def _extract_frames_for_segment(
    path: str,
    start: float,
    end: float,
    src_w: int,
    src_h: int,
    sample_fps: float,
) -> list:
    """
    Extract downscaled RGB frames from [start, end) via FFmpeg stdout pipe.
    Returns list of numpy arrays (H, W, 3) uint8.
    """
    duration = end - start
    if duration < 0.1:
        return []

    # Downscale to 320px wide — face detection is resolution-independent
    out_w = 320
    out_h = max(2, round(src_h * out_w / src_w / 2) * 2)  # ensure even
    frame_size = out_w * out_h * 3

    cmd = [
        settings.ffmpeg_path,
        "-ss", f"{start:.3f}",
        "-i", path,
        "-t", f"{duration:.3f}",
        "-vf", f"fps={sample_fps},scale={out_w}:{out_h},format=rgb24",
        "-f", "rawvideo", "-pix_fmt", "rgb24",
        "-v", "quiet",
        "pipe:1",
    ]

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    frames = []
    while True:
        data = proc.stdout.read(frame_size)
        if len(data) < frame_size:
            break
        frame = np.frombuffer(data, dtype=np.uint8).reshape((out_h, out_w, 3))
        frames.append(frame)

    proc.stdout.close()
    proc.wait()
    return frames


# ------------------------------------------------------------------
# 3. Detect faces (MediaPipe)
# ------------------------------------------------------------------

def _detect_faces(frames: list, confidence: float) -> list[float | None]:
    """
    Run MediaPipe face detection on each frame.
    Returns per-frame face center x (0.0–1.0 normalized) or None.
    Selects largest face when multiple are detected.
    """
    mp_face = mp.solutions.face_detection
    positions: list[float | None] = []

    with mp_face.FaceDetection(
        model_selection=1,  # full-range model (up to ~5 m)
        min_detection_confidence=confidence,
    ) as detector:
        for frame in frames:
            result = detector.process(frame)
            if not result.detections:
                positions.append(None)
                continue

            best_area = 0.0
            best_x = 0.5
            for det in result.detections:
                bb = det.location_data.relative_bounding_box
                area = max(0, bb.width) * max(0, bb.height)
                if area > best_area:
                    best_area = area
                    best_x = bb.xmin + bb.width / 2.0

            positions.append(max(0.0, min(1.0, best_x)))

    return positions


# ------------------------------------------------------------------
# 4. Smooth trajectory (EMA + gap fill + speed clamp)
# ------------------------------------------------------------------

def _smooth_trajectory(
    positions: list[float | None],
    alpha: float,
    max_speed: float = 0.05,
) -> list[float]:
    """
    EMA smoothing with gap filling and speed clamping.
    alpha: EMA weight for new value (lower → smoother). Default 0.15.
    max_speed: max change per frame (fraction of frame width).
    Returns list of same length with no Nones.
    """
    if not positions:
        return []

    filled: list[float | None] = list(positions)

    # Forward-fill gaps
    last: float | None = None
    for i in range(len(filled)):
        if filled[i] is not None:
            last = filled[i]
        elif last is not None:
            filled[i] = last

    # Backward-fill leading gaps
    last = None
    for i in range(len(filled) - 1, -1, -1):
        if filled[i] is not None:
            last = filled[i]
        elif last is not None:
            filled[i] = last

    # If truly all None (shouldn't happen), default to center
    if all(v is None for v in filled):
        return [0.5] * len(positions)

    # EMA + speed clamp
    smoothed = [filled[0]]
    for i in range(1, len(filled)):
        prev = smoothed[-1]
        target = filled[i]
        new = prev + alpha * (target - prev)
        delta = new - prev
        if abs(delta) > max_speed:
            new = prev + max_speed * (1.0 if delta > 0 else -1.0)
        smoothed.append(new)

    return smoothed


# ------------------------------------------------------------------
# 5. RDP simplification
# ------------------------------------------------------------------

def _point_line_dist(
    px: float, py: float,
    ax: float, ay: float,
    bx: float, by: float,
) -> float:
    """Perpendicular distance from point to line segment a→b."""
    dx, dy = bx - ax, by - ay
    len_sq = dx * dx + dy * dy
    if len_sq < 1e-12:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / len_sq))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _rdp(
    points: list[tuple[float, float]],
    epsilon: float,
) -> list[tuple[float, float]]:
    """Ramer-Douglas-Peucker line simplification."""
    if len(points) <= 2:
        return list(points)

    ax, ay = points[0]
    bx, by = points[-1]
    max_dist = 0.0
    max_idx = 0

    for i in range(1, len(points) - 1):
        d = _point_line_dist(points[i][0], points[i][1], ax, ay, bx, by)
        if d > max_dist:
            max_dist = d
            max_idx = i

    if max_dist > epsilon:
        left = _rdp(points[: max_idx + 1], epsilon)
        right = _rdp(points[max_idx:], epsilon)
        return left[:-1] + right

    return [points[0], points[-1]]


# ------------------------------------------------------------------
# 6. Map to output timeline
# ------------------------------------------------------------------

def _map_to_output_timeline(
    segment_data: list[list[tuple[float, float]]],
    segments,
    fade_duration: float,
    speed: float,
) -> list[CropKeyframe]:
    """
    Convert (source_time, x_norm) pairs to output timeline keyframes.
    Accounts for trim offsets, crossfade overlaps, and speed change.
    """
    keyframes: list[CropKeyframe] = []

    for i, data in enumerate(segment_data):
        # Cumulative duration of all preceding segments
        cum_dur = sum(segments[j].duration for j in range(i))
        # Subtract crossfade overlaps
        offset = max(0.0, cum_dur - i * fade_duration)

        for src_t, x_norm in data:
            local_t = src_t - segments[i].start
            out_t = (offset + local_t) / speed
            keyframes.append(CropKeyframe(time=round(out_t, 4), x_norm=x_norm))

    keyframes.sort(key=lambda k: k.time)
    return keyframes


# ------------------------------------------------------------------
# 7. Build FFmpeg crop x expression
# ------------------------------------------------------------------

def _build_crop_x_expr(keyframes: list[CropKeyframe], crop_w: int) -> str:
    """
    Build FFmpeg crop x expression with piecewise linear interpolation.

    Computes: trunc(clamp(face_x * iw - crop_w/2, 0, iw - crop_w) / 2) * 2
    where face_x is linearly interpolated between keyframes.

    Commas inside FFmpeg expression functions are escaped as \\\\,
    (which becomes \\, in the actual string passed to FFmpeg).
    """
    half_cw = crop_w / 2.0

    if not keyframes:
        return f"(iw-{crop_w})/2"

    if len(keyframes) == 1:
        f = keyframes[0].x_norm
        return (
            f"trunc(min(max({f:.4f}*iw-{half_cw:.0f}\\,0)"
            f"\\,iw-{crop_w})/2)*2"
        )

    # Build per-segment interpolation expressions
    segs: list[tuple[float, str]] = []
    for i in range(len(keyframes) - 1):
        t0, f0 = keyframes[i].time, keyframes[i].x_norm
        t1, f1 = keyframes[i + 1].time, keyframes[i + 1].x_norm
        dt = t1 - t0
        if dt < 0.001:
            continue
        df = f1 - f0
        lerp = f"{f0:.4f}+{df:.6f}*(t-{t0:.3f})/{dt:.3f}"
        expr = f"({lerp})*iw-{half_cw:.0f}"
        segs.append((t1, expr))

    if not segs:
        f = keyframes[0].x_norm
        return (
            f"trunc(min(max({f:.4f}*iw-{half_cw:.0f}\\,0)"
            f"\\,iw-{crop_w})/2)*2"
        )

    # Nested if(): last segment holds final value
    last_f = keyframes[-1].x_norm
    result = f"{last_f:.4f}*iw-{half_cw:.0f}"

    for t_end, expr in reversed(segs):
        result = f"if(lt(t\\,{t_end:.3f})\\,{expr}\\,{result})"

    # Clamp to [0, iw-cw] and ensure even alignment (yuv420p)
    return f"trunc(min(max({result}\\,0)\\,iw-{crop_w})/2)*2"


# ------------------------------------------------------------------
# Sync pipeline orchestrator
# ------------------------------------------------------------------

def _analyze_sync(
    path: str,
    segments,
    crop_w: int,
    crop_h: int,
    fade_duration: float,
    speed: float,
) -> CropTrajectory | None:
    """Run the complete face analysis pipeline (blocking)."""

    sample_fps = settings.face_tracking_sample_fps
    alpha = settings.face_tracking_smoothing
    confidence = settings.face_tracking_confidence

    # 1. Probe source video
    src_w, src_h, _ = _probe_video(path)

    # Check there's enough horizontal room after scaling to fill crop_h
    scaled_w = src_w * crop_h / src_h
    if scaled_w < crop_w * 1.1:
        logger.info(
            f"Face tracking: source too narrow after scale "
            f"({scaled_w:.0f}px vs {crop_w}px crop), skipping"
        )
        return None

    total_dur = sum(s.duration for s in segments)
    if total_dur < 3.0:
        logger.info("Face tracking: clip too short (<3s), skipping")
        return None

    # 2–3. Extract frames + detect faces per segment
    all_segment_data: list[list[tuple[float, float]]] = []
    total_frames = 0
    detected_frames = 0

    for seg in segments:
        frames = _extract_frames_for_segment(
            path, seg.start, seg.end, src_w, src_h, sample_fps,
        )
        if not frames:
            all_segment_data.append([])
            continue

        total_frames += len(frames)
        raw_positions = _detect_faces(frames, confidence)
        detected_frames += sum(1 for p in raw_positions if p is not None)

        # 4. Smooth within this segment
        smoothed = _smooth_trajectory(raw_positions, alpha)

        # Build (source_time, x_norm) pairs
        interval = 1.0 / sample_fps
        seg_data: list[tuple[float, float]] = []
        for j, x in enumerate(smoothed):
            t = seg.start + j * interval
            if t > seg.end:
                break
            seg_data.append((t, x))

        all_segment_data.append(seg_data)

    if total_frames == 0:
        logger.info("Face tracking: no frames extracted, skipping")
        return None

    ratio = detected_frames / total_frames
    if ratio < 0.3:
        logger.info(
            f"Face tracking: faces in only {ratio:.0%} of frames (<30%), skipping"
        )
        return None

    logger.info(
        f"Face tracking: faces detected in {detected_frames}/{total_frames} "
        f"frames ({ratio:.0%})"
    )

    # 5. Map to output timeline
    keyframes = _map_to_output_timeline(
        all_segment_data, segments, fade_duration, speed,
    )

    if len(keyframes) < 2:
        logger.info("Face tracking: too few keyframes after mapping, skipping")
        return None

    # 6. RDP simplification
    # Normalize time to [0,1] so both dimensions weigh equally
    points = [(kf.time, kf.x_norm) for kf in keyframes]
    t_max = max(p[0] for p in points) or 1.0
    normalized = [(t / t_max, x) for t, x in points]
    simplified = _rdp(normalized, epsilon=0.008)
    simplified = [(t * t_max, x) for t, x in simplified]
    keyframes = [CropKeyframe(time=t, x_norm=x) for t, x in simplified]

    logger.info(
        f"Face tracking: {len(points)} → {len(keyframes)} keyframes after RDP"
    )

    # 7. Build FFmpeg expression
    expr = _build_crop_x_expr(keyframes, crop_w)
    logger.debug(f"Face tracking crop_x expression ({len(expr)} chars): {expr}")

    return CropTrajectory(keyframes=keyframes, crop_x_expr=expr)


# ------------------------------------------------------------------
# Public async entry point
# ------------------------------------------------------------------

async def analyze_face_positions(
    video_path: str,
    segments,
    crop_w: int,
    crop_h: int,
    fade_duration: float = 1.0,
    speed: float = 1.0,
) -> CropTrajectory | None:
    """
    Analyze face positions in video segments for dynamic crop positioning.

    Returns CropTrajectory with FFmpeg expression, or None if:
    - mediapipe/numpy not installed
    - faces detected in <30% of frames
    - source too narrow for meaningful tracking
    - clip shorter than 3 seconds
    - any error occurs (non-fatal)
    """
    if not _AVAILABLE:
        logger.warning(
            "Face tracking unavailable: install mediapipe and "
            "opencv-python-headless (pip install mediapipe opencv-python-headless)"
        )
        return None

    try:
        return await asyncio.to_thread(
            _analyze_sync,
            video_path, segments, crop_w, crop_h, fade_duration, speed,
        )
    except Exception as e:
        logger.warning(f"Face tracking failed (non-fatal, using center crop): {e}")
        return None

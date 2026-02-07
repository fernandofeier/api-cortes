import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Optional

from google import genai

from core.config import settings

logger = logging.getLogger(__name__)

GEMINI_POLL_TIMEOUT = 600  # max seconds waiting for Gemini to process the file


@dataclass
class VideoSegment:
    start: float
    end: float
    description: str


@dataclass
class Corte:
    corte_number: int
    title: str
    segments: list[VideoSegment] = field(default_factory=list)


SINGLE_CLIP_PROMPT = """\
You are a viral video editor AI. Analyze this video and identify the most \
engaging, viral-worthy segments for a SINGLE short-form video (TikTok, Reels, Shorts).

CRITICAL RULES:
- Return exactly 1 corte (clip compilation).
- The corte must contain 2 to 4 segments that make sense together as a coherent short video.
- The SUM of all segment durations MUST NOT exceed 80 seconds. Hard limit.
- Each individual segment: 10 to 40 seconds.
- The FIRST segment MUST start with a hook — something visually or emotionally striking \
that grabs attention in the first 3 seconds. This is critical for retention.
- Segments must NOT overlap.
- Segments should be from DIFFERENT parts of the video to create variety.
- Do NOT include intro/outro, filler, or low-energy content.
- Timestamps must be precise to 0.1 seconds.

Return ONLY valid JSON, no markdown, no code fences. Format:

[
  {
    "corte_number": 1,
    "title": "Short catchy title for this clip",
    "segments": [
      {"start": 12.5, "end": 38.0, "description": "Hook: dramatic reveal"},
      {"start": 78.0, "end": 105.5, "description": "Emotional payoff moment"}
    ]
  }
]
"""

MULTI_CLIP_PROMPT = """\
You are a viral video editor AI. Analyze this video and identify segments for \
{max_clips} SEPARATE short-form videos (TikTok, Reels, Shorts). Each one must be \
an independent viral clip.

CRITICAL RULES:
- Return up to {max_clips} cortes (clip compilations). Each corte becomes a separate video.
- Each corte must contain 2 to 4 segments that make sense together as a coherent short video.
- The SUM of segment durations within EACH corte MUST NOT exceed 80 seconds. Hard limit.
- Each individual segment: 10 to 40 seconds.
- The FIRST segment of EACH corte MUST start with a hook — something visually or \
emotionally striking that grabs attention in the first 3 seconds. Critical for retention.
- Segments MUST NOT repeat or overlap between cortes. Each corte uses unique moments.
- Segments within a corte should be from DIFFERENT parts of the video to create variety.
- Do NOT include intro/outro, filler, or low-energy content.
- Timestamps must be precise to 0.1 seconds.
- Each corte should have a different theme/angle to maximize variety.

Return ONLY valid JSON, no markdown, no code fences. Format:

[
  {{
    "corte_number": 1,
    "title": "Short catchy title for this clip",
    "segments": [
      {{"start": 12.5, "end": 38.0, "description": "Hook: dramatic moment"}},
      {{"start": 78.0, "end": 105.5, "description": "Emotional payoff"}}
    ]
  }},
  {{
    "corte_number": 2,
    "title": "Another angle on the content",
    "segments": [
      {{"start": 200.0, "end": 225.0, "description": "Hook: surprising reveal"}},
      {{"start": 310.0, "end": 340.0, "description": "Key insight moment"}}
    ]
  }}
]
"""


def _build_prompt(num_clips: int, custom_instruction: Optional[str] = None) -> str:
    if num_clips <= 1:
        prompt = SINGLE_CLIP_PROMPT
    else:
        prompt = MULTI_CLIP_PROMPT.format(max_clips=num_clips)

    if custom_instruction:
        prompt += f"\n\nAdditional instruction from the user:\n{custom_instruction}"

    return prompt


def _upload_and_wait(client: genai.Client, video_path: str):
    """Upload video to Gemini File API and wait until processing is complete."""
    logger.info(f"Uploading video to Gemini File API: {video_path}")
    uploaded_file = client.files.upload(file=video_path)
    logger.info(f"Upload complete. File name: {uploaded_file.name}")

    start_time = time.time()
    while uploaded_file.state.name == "PROCESSING":
        elapsed = time.time() - start_time
        if elapsed > GEMINI_POLL_TIMEOUT:
            # Cleanup the stuck file before raising
            try:
                client.files.delete(name=uploaded_file.name)
            except Exception:
                pass
            raise RuntimeError(
                f"Gemini file processing timed out after {GEMINI_POLL_TIMEOUT}s "
                f"for {uploaded_file.name}"
            )
        logger.info(f"Waiting for Gemini to process video... ({elapsed:.0f}s)")
        time.sleep(5)
        uploaded_file = client.files.get(name=uploaded_file.name)

    if uploaded_file.state.name == "FAILED":
        # Cleanup the failed file
        try:
            client.files.delete(name=uploaded_file.name)
        except Exception:
            pass
        raise RuntimeError(f"Gemini file processing failed for {uploaded_file.name}")

    logger.info(f"File ready. State: {uploaded_file.state.name}")
    return uploaded_file


def _generate_analysis(client: genai.Client, uploaded_file, prompt: str) -> str:
    """Call Gemini generate_content and return raw text response."""
    logger.info(f"Sending analysis request to model: {settings.gemini_model}")
    response = client.models.generate_content(
        model=settings.gemini_model,
        contents=[uploaded_file, prompt],
    )
    return response.text.strip()


def _strip_code_fences(text: str) -> str:
    """Remove markdown code fences if present."""
    if text.startswith("```"):
        lines = text.split("\n")
        lines = [line for line in lines if not line.strip().startswith("```")]
        return "\n".join(lines)
    return text


def _validate_segment(seg: VideoSegment) -> bool:
    if seg.end <= seg.start:
        logger.warning(f"Skipping invalid segment (end <= start): {seg}")
        return False
    if (seg.end - seg.start) < 1.0:
        logger.warning(f"Skipping too-short segment (<1s): {seg}")
        return False
    return True


def _parse_cortes(raw_text: str) -> list[Corte]:
    """Parse Gemini JSON response into a list of Corte objects."""
    logger.info(f"Gemini raw response (first 500 chars): {raw_text[:500]}")

    text = _strip_code_fences(raw_text)
    data = json.loads(text)

    cortes: list[Corte] = []
    for item in data:
        segments = []
        for seg_data in item.get("segments", []):
            seg = VideoSegment(
                start=float(seg_data["start"]),
                end=float(seg_data["end"]),
                description=seg_data.get("description", ""),
            )
            if _validate_segment(seg):
                segments.append(seg)

        if not segments:
            continue

        # Sort segments within each corte by start time
        segments.sort(key=lambda s: s.start)

        cortes.append(Corte(
            corte_number=item.get("corte_number", len(cortes) + 1),
            title=item.get("title", f"Corte {len(cortes) + 1}"),
            segments=segments,
        ))

    return cortes


def _get_video_duration(video_path: str) -> float:
    """Get video duration using ffprobe."""
    import subprocess
    result = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", video_path],
        capture_output=True, text=True,
    )
    if result.returncode == 0 and result.stdout.strip():
        return float(result.stdout.strip())
    return 0.0


async def analyze_video(
    video_path: str,
    custom_instruction: Optional[str] = None,
    max_clips: int = 1,
) -> list[Corte]:
    """
    Upload video to Gemini, analyze for viral segments, return cortes.

    If max_clips > 1 and video > 10 min: returns up to max_clips cortes.
    Otherwise: returns 1 corte.

    IMPORTANT: Uses try/finally to always clean up uploaded Gemini files,
    preventing orphaned files from consuming storage quota.
    """
    # Pre-validate file size to avoid wasting Gemini credits on huge files
    file_size_mb = os.path.getsize(video_path) / (1024 * 1024)
    max_size = settings.max_upload_size_mb
    if file_size_mb > max_size:
        raise RuntimeError(
            f"Video file is too large ({file_size_mb:.0f} MB). "
            f"Maximum allowed: {max_size} MB. "
            f"Change MAX_UPLOAD_SIZE_MB in .env to increase (Gemini supports up to 2 GB)."
        )

    client = genai.Client(api_key=settings.gemini_api_key)

    # Determine how many clips to generate
    num_clips = 1
    if max_clips > 1:
        duration = await asyncio.to_thread(_get_video_duration, video_path)
        logger.info(f"Video duration: {duration:.1f}s")
        if duration >= settings.multi_clip_min_video_duration:
            num_clips = max_clips
            logger.info(f"Video is {duration:.0f}s (>= {settings.multi_clip_min_video_duration}s), generating up to {num_clips} cortes")
        else:
            logger.info(f"Video is {duration:.0f}s (< {settings.multi_clip_min_video_duration}s), generating 1 corte despite max_clips={max_clips}")

    # Upload and wait
    uploaded_file = await asyncio.to_thread(_upload_and_wait, client, video_path)

    # CRITICAL: Always cleanup the uploaded file, even if generation/parsing fails.
    # This prevents orphaned files from consuming Gemini storage quota.
    try:
        # Build prompt and generate
        prompt = _build_prompt(num_clips, custom_instruction)
        raw_text = await asyncio.to_thread(_generate_analysis, client, uploaded_file, prompt)

        # Parse
        cortes = _parse_cortes(raw_text)
    finally:
        # Always cleanup, regardless of success or failure
        try:
            await asyncio.to_thread(client.files.delete, name=uploaded_file.name)
            logger.info(f"Cleaned up Gemini file: {uploaded_file.name}")
        except Exception as e:
            logger.warning(f"Failed to delete Gemini file {uploaded_file.name}: {e}")

    logger.info(f"Identified {len(cortes)} corte(s)")
    for c in cortes:
        total = sum(s.end - s.start for s in c.segments)
        logger.info(f"  Corte {c.corte_number} '{c.title}': {len(c.segments)} segments, {total:.0f}s total")

    return cortes

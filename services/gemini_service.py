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
class UsageInfo:
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    model: str = ""
    estimated_cost_usd: float = 0.0
    estimated_cost_brl: float = 0.0

    def to_dict(self) -> dict:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "model": self.model,
            "estimated_cost_usd": round(self.estimated_cost_usd, 6),
            "estimated_cost_brl": round(self.estimated_cost_brl, 4),
        }


# Pricing per million tokens (USD) — Gemini 3 Flash Preview
# Source: https://ai.google.dev/gemini-api/docs/pricing
_PRICING = {
    "gemini-3-flash-preview": {"input": 0.50, "output": 3.00},
}
# Fallback: use Gemini 3 Flash Preview pricing for unknown models
_DEFAULT_PRICING = {"input": 0.50, "output": 3.00}

# USD → BRL approximate rate (updated periodically)
_USD_TO_BRL = 5.80


def _estimate_cost(input_tokens: int, output_tokens: int, model: str) -> UsageInfo:
    """Estimate cost based on token usage and model pricing."""
    pricing = _PRICING.get(model, _DEFAULT_PRICING)
    input_cost = (input_tokens / 1_000_000) * pricing["input"]
    output_cost = (output_tokens / 1_000_000) * pricing["output"]
    total_usd = input_cost + output_cost
    return UsageInfo(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
        model=model,
        estimated_cost_usd=total_usd,
        estimated_cost_brl=total_usd * _USD_TO_BRL,
    )


@dataclass
class AnalysisResult:
    cortes: list["Corte"] = field(default_factory=list)
    usage: UsageInfo = field(default_factory=UsageInfo)


@dataclass
class Corte:
    corte_number: int
    title: str
    platform: str = "universal"
    segments: list[VideoSegment] = field(default_factory=list)


SINGLE_CLIP_PROMPT = """\
You are a viral video editor AI. Analyze this video and identify the most \
engaging, viral-worthy segments for a SINGLE short-form video (TikTok, Reels, Shorts).

CRITICAL RULES:
- Return exactly 1 corte (clip compilation).
- The corte must contain 2 to 4 segments that make sense together as a coherent short video.
- The SUM of all segment durations MUST be between 50 and 70 seconds. Aim for 60-70s. \
Do NOT make clips shorter than 50 seconds — short clips feel incomplete and lose viewers.
- Each individual segment: 10 to 40 seconds.
- The FIRST segment MUST start with a hook — something visually or emotionally striking \
that grabs attention in the first 3 seconds. This is critical for retention.
- Segments must NOT overlap.
- Segments should be from DIFFERENT parts of the video to create variety.
- Each segment must tell a complete thought or moment — never cut mid-sentence or mid-action.
- Do NOT include intro/outro, filler, or low-energy content.
- Timestamps must be precise to 0.1 seconds.
- The "platform" field must be "universal" (this clip works on all platforms).

LANGUAGE RULE:
- The "title" and each segment's "description" MUST be written in the SAME language \
spoken in the video. If the video is in Portuguese, write in Portuguese. If in English, \
write in English. Match the video's language exactly.

Return ONLY valid JSON, no markdown, no code fences. Format:

[
  {
    "corte_number": 1,
    "title": "Short catchy title in the video's language",
    "platform": "universal",
    "segments": [
      {"start": 12.5, "end": 38.0, "description": "Hook: dramatic reveal"},
      {"start": 78.0, "end": 105.5, "description": "Emotional payoff moment"}
    ]
  }
]
"""

MULTI_CLIP_PROMPT = """\
You are a viral video editor AI. Analyze this video and identify segments for \
{max_clips} SEPARATE short-form videos optimized for different social media platforms.

PLATFORM RULES:
- Corte 1 MUST be for YouTube Shorts: "platform": "youtube_shorts"
  - Total duration: 50 to 70 seconds (sum of all segments). Aim for 60-70s. \
YouTube Shorts has a strict 60s limit so we keep it under 70s to allow for transitions.
  - Each individual segment: 10 to 40 seconds.
- Cortes 2 and beyond MUST be for TikTok/Instagram: "platform": "tiktok_instagram"
  - Total duration: MINIMUM 70 seconds, MAXIMUM 160 seconds (2min 40s). Aim for 90-140s. \
These platforms allow longer content, so USE that time to tell a more complete story. \
Do NOT make these clips short — a 30s or 40s TikTok/Instagram clip feels incomplete. \
Include more context, build-up, and payoff.
  - Each individual segment: 15 to 60 seconds. Use longer segments to capture full moments.

CRITICAL RULES:
- Return up to {max_clips} cortes (clip compilations). Each corte becomes a separate video.
- Each corte must contain 2 to 4 segments that make sense together as a coherent short video.
- The FIRST segment of EACH corte MUST start with a hook — something visually or \
emotionally striking that grabs attention in the first 3 seconds. Critical for retention.
- Each segment must tell a complete thought or moment — never cut mid-sentence or mid-action.
- Segments MUST NOT repeat or overlap between cortes. Each corte uses unique moments.
- Segments within a corte should be from DIFFERENT parts of the video to create variety.
- Do NOT include intro/outro, filler, or low-energy content.
- Timestamps must be precise to 0.1 seconds.
- Each corte should have a different theme/angle to maximize variety.
- Every corte MUST have the "platform" field.

LANGUAGE RULE:
- The "title" and each segment's "description" MUST be written in the SAME language \
spoken in the video. If the video is in Portuguese, write in Portuguese. If in English, \
write in English. Match the video's language exactly.

Return ONLY valid JSON, no markdown, no code fences. Format:

[
  {{
    "corte_number": 1,
    "title": "Titulo curto e chamativo no idioma do video",
    "platform": "youtube_shorts",
    "segments": [
      {{"start": 12.5, "end": 38.0, "description": "Hook: momento dramatico"}},
      {{"start": 78.0, "end": 105.5, "description": "Desfecho emocional"}}
    ]
  }},
  {{
    "corte_number": 2,
    "title": "Outro angulo sobre o conteudo",
    "platform": "tiktok_instagram",
    "segments": [
      {{"start": 200.0, "end": 245.0, "description": "Hook: revelacao surpreendente"}},
      {{"start": 310.0, "end": 365.0, "description": "Momento chave com contexto completo"}},
      {{"start": 400.0, "end": 435.0, "description": "Conclusao impactante"}}
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


def _generate_analysis(client: genai.Client, uploaded_file, prompt: str):
    """Call Gemini generate_content and return the full response object."""
    logger.info(f"Sending analysis request to model: {settings.gemini_model}")
    response = client.models.generate_content(
        model=settings.gemini_model,
        contents=[uploaded_file, prompt],
    )
    return response


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


def _repair_json(text: str) -> str:
    """Attempt to repair common Gemini JSON issues."""
    import re
    text = text.strip()

    # Remove markdown code fences
    text = re.sub(r"^```\w*\s*\n?", "", text)
    text = re.sub(r"\n?```\s*$", "", text)
    text = text.strip()

    # Fix trailing commas before } or ]
    text = re.sub(r",\s*([}\]])", r"\1", text)

    # If truncated mid-string, close at last complete object
    if text and not text.endswith("]"):
        last_brace = text.rfind("}")
        if last_brace > 0:
            text = text[:last_brace + 1] + "]"
            logger.warning("JSON appeared truncated, attempted repair")

    return text


def _parse_cortes(raw_text: str) -> list[Corte]:
    """Parse Gemini JSON response into a list of Corte objects."""
    logger.info(f"Gemini raw response (first 500 chars): {raw_text[:500]}")

    text = _strip_code_fences(raw_text)

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        logger.warning("Initial JSON parse failed, attempting repair")
        text = _repair_json(raw_text)
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
            platform=item.get("platform", "universal"),
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
) -> AnalysisResult:
    """
    Upload video to Gemini, analyze for viral segments, return AnalysisResult.

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
    usage = UsageInfo()
    try:
        # Build prompt and generate
        prompt = _build_prompt(num_clips, custom_instruction)
        response = await asyncio.to_thread(_generate_analysis, client, uploaded_file, prompt)

        # Extract usage metadata
        meta = getattr(response, "usage_metadata", None)
        if meta:
            input_tokens = getattr(meta, "prompt_token_count", 0) or 0
            output_tokens = getattr(meta, "candidates_token_count", 0) or 0
            usage = _estimate_cost(input_tokens, output_tokens, settings.gemini_model)
            logger.info(
                f"Gemini usage: {usage.input_tokens} input + {usage.output_tokens} output "
                f"= {usage.total_tokens} tokens (${usage.estimated_cost_usd:.4f} USD)"
            )

        raw_text = response.text.strip()

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

    return AnalysisResult(cortes=cortes, usage=usage)

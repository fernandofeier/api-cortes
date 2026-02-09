import asyncio
import json
import logging
import os
import subprocess
import time

from google import genai

from core.config import settings
from services.video_engine import burn_captions

logger = logging.getLogger(__name__)

TRANSCRIPTION_PROMPT = """\
Transcribe the audio with word-level precise timestamps.
Return ONLY a JSON array, no markdown, no code fences. Format:
[{"start": 0.00, "end": 2.50, "text": "phrase here"}, ...]

Critical rules:
- Maximum 4 words per block — shorter blocks are better for sync
- Timestamps in seconds with 2 decimal places (e.g. 1.25, not 1.2)
- "start" must be the EXACT moment the first word begins being spoken
- "end" must be the EXACT moment the last word finishes being spoken
- Do NOT add padding before start or after end — timestamps must be tight to the speech
- If there is a pause between phrases, the next block starts when speech resumes, not before
- If there is no speech at all, return []
- Transcribe in the original language of the audio
"""

GEMINI_POLL_TIMEOUT = 300


def _extract_audio(video_path: str, output_path: str) -> str:
    """Extract audio track from video using FFmpeg."""
    cmd = [
        settings.ffmpeg_path, "-y",
        "-i", video_path,
        "-vn",
        "-acodec", "aac",
        "-b:a", "128k",
        output_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise RuntimeError(f"Audio extraction failed: {result.stderr[-500:]}")
    logger.info(f"Audio extracted: {output_path}")
    return output_path


def _upload_and_wait(client: genai.Client, file_path: str):
    """Upload file to Gemini File API and wait until processing is complete."""
    logger.info(f"Uploading audio to Gemini for transcription: {file_path}")
    uploaded_file = client.files.upload(file=file_path)

    start_time = time.time()
    while uploaded_file.state.name == "PROCESSING":
        elapsed = time.time() - start_time
        if elapsed > GEMINI_POLL_TIMEOUT:
            try:
                client.files.delete(name=uploaded_file.name)
            except Exception:
                pass
            raise RuntimeError(
                f"Gemini file processing timed out after {GEMINI_POLL_TIMEOUT}s"
            )
        time.sleep(3)
        uploaded_file = client.files.get(name=uploaded_file.name)

    if uploaded_file.state.name == "FAILED":
        try:
            client.files.delete(name=uploaded_file.name)
        except Exception:
            pass
        raise RuntimeError("Gemini file processing failed")

    return uploaded_file


def _strip_code_fences(text: str) -> str:
    """Remove markdown code fences if present."""
    if text.startswith("```"):
        lines = text.split("\n")
        lines = [line for line in lines if not line.strip().startswith("```")]
        return "\n".join(lines)
    return text


def _transcribe(client: genai.Client, uploaded_file) -> list[dict]:
    """Call Gemini to transcribe audio and return list of {start, end, text}."""
    response = client.models.generate_content(
        model=settings.caption_model,
        contents=[uploaded_file, TRANSCRIPTION_PROMPT],
    )

    raw = response.text.strip()
    logger.info(f"Transcription response (first 300 chars): {raw[:300]}")

    text = _strip_code_fences(raw)
    blocks = json.loads(text)

    if not isinstance(blocks, list):
        logger.warning("Transcription returned non-list, treating as empty")
        return []

    valid = []
    for b in blocks:
        if isinstance(b, dict) and "start" in b and "end" in b and "text" in b:
            start = float(b["start"])
            end = float(b["end"])
            if end > start and b["text"].strip():
                valid.append({"start": start, "end": end, "text": b["text"].strip()})

    logger.info(f"Transcription: {len(valid)} blocks")
    return valid


def _format_ass_time(seconds: float) -> str:
    """Convert seconds to ASS time format: H:MM:SS.CC"""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    cs = int((seconds % 1) * 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _generate_ass(transcription: list[dict], output_path: str, width: int = 1080, height: int = 1920) -> str:
    """Generate ASS subtitle file with viral-style formatting."""
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,48,&H00FFFFFF,&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,4,1,2,40,40,320,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = [header]
    for block in transcription:
        start = _format_ass_time(block["start"])
        end = _format_ass_time(block["end"])
        text = block["text"].replace("\n", "\\N")
        lines.append(f"Dialogue: 0,{start},{end},Default,,0,0,0,,{text}")

    content = "\n".join(lines) + "\n"

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(content)

    logger.info(f"ASS file generated: {output_path} ({len(transcription)} blocks)")
    return output_path


async def add_captions(video_path: str, work_dir: str) -> str:
    """
    Full caption pipeline:
    1. Extract audio from video
    2. Transcribe via Gemini Flash Lite
    3. Generate ASS subtitle file
    4. Burn captions into video

    Returns path to captioned video (or original if no speech detected).
    """
    audio_path = os.path.join(work_dir, "caption-audio.aac")
    ass_path = os.path.join(work_dir, "captions.ass")
    captioned_path = os.path.join(work_dir, "captioned-" + os.path.basename(video_path))

    # Step 1: Extract audio
    await asyncio.to_thread(_extract_audio, video_path, audio_path)

    # Step 2: Transcribe with Gemini
    client = genai.Client(api_key=settings.gemini_api_key)
    uploaded_file = await asyncio.to_thread(_upload_and_wait, client, audio_path)

    try:
        transcription = await asyncio.to_thread(_transcribe, client, uploaded_file)
    finally:
        try:
            await asyncio.to_thread(client.files.delete, name=uploaded_file.name)
            logger.info(f"Cleaned up Gemini file: {uploaded_file.name}")
        except Exception as e:
            logger.warning(f"Failed to delete Gemini file: {e}")

    # Step 3: Check if there's speech
    if not transcription:
        logger.info("No speech detected, skipping captions")
        return video_path

    # Step 4: Generate ASS file
    _generate_ass(transcription, ass_path)

    # Step 5: Burn captions into video
    await asyncio.to_thread(burn_captions, video_path, ass_path, captioned_path)

    return captioned_path

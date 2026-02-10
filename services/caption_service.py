import asyncio
import json
import logging
import os
import time

from google import genai

from core.config import settings
from services.video_engine import burn_captions

logger = logging.getLogger(__name__)

TRANSCRIPTION_PROMPT = """\
Watch this video and transcribe the spoken dialogue with precise timestamps.
Use the visual cues (lip movement, scene changes) to sync timestamps accurately.
Return ONLY a JSON array, no markdown, no code fences.
[{"start": 0.00, "end": 2.50, "text": "phrase here"}, ...]

Rules:
- Each block: 2 to 5 words maximum
- Timestamps in seconds with 2 decimal places (e.g. 1.25, 3.80)
- "start" = exact moment the person begins speaking (watch their lips)
- "end" = exact moment the person stops speaking that phrase
- Only transcribe when someone is visually speaking on screen
- During scene transitions or silence, do NOT generate any blocks
- Ignore background music, sound effects, and non-speech audio
- Pay attention to proper nouns and character names
- Transcribe in the original language of the audio
- If no speech, return []
"""

GEMINI_POLL_TIMEOUT = 300


def _upload_and_wait(client: genai.Client, file_path: str):
    """Upload file to Gemini File API and wait until processing is complete."""
    logger.info(f"Uploading to Gemini for transcription: {file_path}")
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


def _postprocess_transcription(blocks: list[dict]) -> list[dict]:
    """
    Clean up transcription blocks to fix common Gemini timing issues:
    - Remove very short blocks (likely hallucinations during silence)
    - Remove overlaps so only one subtitle shows at a time
    - Ensure sequential ordering
    """
    if not blocks:
        return blocks

    # Sort by start time
    blocks.sort(key=lambda b: b["start"])

    initial_count = len(blocks)

    # Filter out very short blocks (< 0.15s) — usually hallucinations
    blocks = [b for b in blocks if (b["end"] - b["start"]) >= 0.15]

    # Remove overlaps: each block's end must not exceed next block's start
    for i in range(len(blocks) - 1):
        next_start = blocks[i + 1]["start"]
        if blocks[i]["end"] > next_start:
            blocks[i]["end"] = next_start

    # After trimming, remove blocks that became too short
    blocks = [b for b in blocks if (b["end"] - b["start"]) >= 0.08]

    logger.info(f"Post-processing: {initial_count} -> {len(blocks)} blocks")
    return blocks


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
Collisions: Normal

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
    1. Upload video to Gemini (video gives visual context for better sync)
    2. Transcribe via Gemini Flash
    3. Post-process timestamps
    4. Generate ASS subtitle file
    5. Burn captions into video

    Returns path to captioned video (or original if no speech detected).
    """
    ass_path = os.path.join(work_dir, "captions.ass")
    captioned_path = os.path.join(work_dir, "captioned-" + os.path.basename(video_path))

    # Step 1: Upload video to Gemini (video = visual + audio context for precise sync)
    client = genai.Client(api_key=settings.gemini_api_key)
    uploaded_file = await asyncio.to_thread(_upload_and_wait, client, video_path)

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

    # Step 4: Post-process timestamps (remove overlaps, ghost blocks)
    transcription = _postprocess_transcription(transcription)
    if not transcription:
        logger.info("No valid blocks after post-processing, skipping captions")
        return video_path

    # Step 5: Generate ASS file
    _generate_ass(transcription, ass_path)

    # Step 6: Burn captions into video
    await asyncio.to_thread(burn_captions, video_path, ass_path, captioned_path)

    return captioned_path

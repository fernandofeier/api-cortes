import asyncio
import json
import logging
import os
import re
import subprocess
import time

from google import genai

from core.config import settings
from services.video_engine import burn_captions

logger = logging.getLogger(__name__)

GEMINI_POLL_TIMEOUT = 300

TRANSCRIPTION_PROMPT = """\
Transcribe the spoken dialogue in this audio with precise timestamps.
Return ONLY a JSON array, no markdown, no code fences.
[{"start": 0.00, "end": 2.50, "text": "phrase here"}, ...]

Rules:
- Each block: 2 to 5 words
- Timestamps in seconds with 2 decimal places
- "start" = when the first word begins being spoken
- "end" = when the last word finishes being spoken
- IMPORTANT: Transcribe in the language that is ACTUALLY SPOKEN in the audio
- If the audio is a dub (e.g. Portuguese dub of an English show), transcribe the PORTUGUESE words you hear
- Do NOT translate — write exactly what you hear
- Ignore music, sound effects, and background noise
- No blocks during silence
- Pay attention to proper nouns and character names
- If no speech, return []
"""


def _extract_audio(video_path: str, output_path: str) -> str:
    """Extract audio track from video using FFmpeg."""
    cmd = [
        settings.ffmpeg_path, "-y",
        "-i", video_path,
        "-vn", "-acodec", "aac", "-b:a", "128k",
        output_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise RuntimeError(f"Audio extraction failed: {result.stderr[-500:]}")
    logger.info(f"Audio extracted: {output_path}")
    return output_path


def _upload_and_wait(client: genai.Client, file_path: str):
    """Upload file to Gemini File API and wait until processing is complete."""
    logger.info(f"Uploading to Gemini for transcription: {file_path}")
    uploaded_file = client.files.upload(file=file_path)

    start_time = time.time()
    while uploaded_file.state.name == "PROCESSING":
        if time.time() - start_time > GEMINI_POLL_TIMEOUT:
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


def _clean_json_response(raw: str) -> str:
    """Clean Gemini response to extract valid JSON."""
    text = raw.strip()

    # Remove markdown code fences
    text = re.sub(r"^```\w*\s*\n?", "", text)
    text = re.sub(r"\n?```\s*$", "", text)
    text = text.strip()

    # If truncated mid-string, try to close at last complete object
    if text and not text.endswith("]"):
        last_brace = text.rfind("}")
        if last_brace > 0:
            text = text[:last_brace + 1] + "]"
            logger.warning("JSON response appeared truncated, attempted repair")

    return text


def _transcribe(client: genai.Client, uploaded_file) -> list[dict]:
    """Call Gemini to transcribe audio. Returns list of {start, end, text}."""
    for attempt in range(2):
        response = client.models.generate_content(
            model=settings.caption_model,
            contents=[uploaded_file, TRANSCRIPTION_PROMPT],
        )

        raw = response.text.strip()
        logger.info(f"Transcription attempt {attempt+1} (first 500 chars): {raw[:500]}")

        text = _clean_json_response(raw)

        try:
            blocks = json.loads(text)
        except json.JSONDecodeError as e:
            logger.warning(f"JSON parse failed (attempt {attempt+1}): {e}")
            logger.warning(f"Raw response: {raw[:1000]}")
            if attempt == 0:
                continue
            return []

        if not isinstance(blocks, list):
            logger.warning("Transcription returned non-list")
            return []

        valid = []
        for b in blocks:
            if isinstance(b, dict) and "start" in b and "end" in b and "text" in b:
                start = float(b["start"])
                end = float(b["end"])
                if end > start and b["text"].strip():
                    valid.append({"start": start, "end": end, "text": b["text"].strip()})

        logger.info(f"Transcription: {len(valid)} valid blocks")
        return valid

    return []


def _postprocess(blocks: list[dict]) -> list[dict]:
    """
    Clean up transcription blocks:
    - Sort by start time
    - Remove very short blocks
    - Remove overlaps so only one subtitle shows at a time
    """
    if not blocks:
        return blocks

    blocks.sort(key=lambda b: b["start"])

    initial = len(blocks)

    # Remove blocks shorter than 0.15s
    blocks = [b for b in blocks if (b["end"] - b["start"]) >= 0.15]

    # Remove overlaps: trim end of each block to not exceed next block's start
    for i in range(len(blocks) - 1):
        if blocks[i]["end"] > blocks[i + 1]["start"]:
            blocks[i]["end"] = blocks[i + 1]["start"]

    # Remove blocks that became too short after trimming
    blocks = [b for b in blocks if (b["end"] - b["start"]) >= 0.08]

    logger.info(f"Post-processing: {initial} -> {len(blocks)} blocks")
    return blocks


def _format_ass_time(seconds: float) -> str:
    """Convert seconds to ASS time format: H:MM:SS.CC"""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    cs = int((seconds % 1) * 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _generate_ass(blocks: list[dict], output_path: str, width: int = 1080, height: int = 1920) -> str:
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
    for block in blocks:
        start = _format_ass_time(block["start"])
        end = _format_ass_time(block["end"])
        text = block["text"].replace("\n", "\\N")
        lines.append(f"Dialogue: 0,{start},{end},Default,,0,0,0,,{text}")

    content = "\n".join(lines) + "\n"

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(content)

    logger.info(f"ASS file generated: {output_path} ({len(blocks)} blocks)")
    return output_path


async def add_captions(video_path: str, work_dir: str) -> str:
    """
    Caption pipeline:
    1. Extract audio from clip
    2. Send audio to Gemini for transcription with timestamps
    3. Post-process (remove overlaps, short blocks)
    4. Generate ASS and burn into video

    Non-fatal: if anything fails, returns original video without captions.
    """
    try:
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
            except Exception:
                pass

        if not transcription:
            logger.info("No speech detected, skipping captions")
            return video_path

        # Step 3: Post-process
        transcription = _postprocess(transcription)
        if not transcription:
            logger.info("No valid blocks after post-processing, skipping captions")
            return video_path

        # Step 4: Generate ASS and burn
        _generate_ass(transcription, ass_path)
        await asyncio.to_thread(burn_captions, video_path, ass_path, captioned_path)

        return captioned_path

    except Exception as e:
        logger.error(f"Caption generation failed, delivering video without captions: {e}")
        return video_path

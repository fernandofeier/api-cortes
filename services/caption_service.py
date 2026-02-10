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


# ---------------------------------------------------------------------------
# Step 1: FFmpeg — extract audio & detect speech regions (signal-level)
# ---------------------------------------------------------------------------

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


def _detect_speech_regions(audio_path: str) -> list[dict]:
    """
    Use FFmpeg silencedetect to find exactly when speech occurs.
    Returns list of {"start": float, "end": float} for non-silent periods.

    This gives signal-level precision — much better than LLM timestamp guessing.
    """
    cmd = [
        settings.ffmpeg_path,
        "-i", audio_path,
        "-af", "silencedetect=noise=-30dB:d=0.4",
        "-f", "null", "-",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    stderr = result.stderr

    # Parse silence boundaries from FFmpeg output
    silence_starts = [float(m) for m in re.findall(r"silence_start:\s*([\d.]+)", stderr)]
    silence_ends = [float(m) for m in re.findall(r"silence_end:\s*([\d.]+)", stderr)]

    # Get total audio duration
    total = 0.0
    dur_match = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", stderr)
    if dur_match:
        h, m, s = dur_match.groups()
        total = int(h) * 3600 + int(m) * 60 + float(s)

    # Pair silence_start/silence_end
    silences = list(zip(silence_starts, silence_ends[:len(silence_starts)]))
    # Handle trailing silence (start without matching end = silence until end of file)
    if len(silence_starts) > len(silence_ends) and total > 0:
        silences.append((silence_starts[-1], total))

    # Invert silence regions to get speech regions
    speech = []
    pos = 0.0
    for s_start, s_end in sorted(silences):
        if s_start > pos + 0.1:
            speech.append({"start": round(pos, 2), "end": round(s_start, 2)})
        pos = s_end
    # Trailing speech after last silence
    if total > 0 and pos < total - 0.1:
        speech.append({"start": round(pos, 2), "end": round(total, 2)})

    # If no silence detected at all, entire audio is speech
    if not silences and total > 0:
        speech = [{"start": 0.0, "end": round(total, 2)}]

    logger.info(f"Speech detection: {len(speech)} regions, {len(silences)} silence gaps")
    return speech


# ---------------------------------------------------------------------------
# Step 2: Gemini — transcribe TEXT only (no timestamp guessing)
# ---------------------------------------------------------------------------

def _build_prompt(speech_regions: list[dict]) -> str:
    """Build prompt that tells Gemini exactly when speech occurs."""
    regions_str = "\n".join(
        f"  Region {i+1}: {r['start']:.2f}s to {r['end']:.2f}s"
        for i, r in enumerate(speech_regions)
    )
    return f"""\
Transcribe the speech in this audio file.
Speech was detected at these exact moments:

{regions_str}

For each region, transcribe what is said.
Return ONLY a JSON array, no markdown, no code fences:
[{{"region": 1, "text": "full transcription here"}}, ...]

Rules:
- Transcribe in the EXACT language spoken in the audio (listen carefully)
- If the content is dubbed, transcribe the DUB language, not the original
- Pay close attention to proper nouns and character names
- If a region has no intelligible speech (just noise/music), use empty string ""
- Return ONLY the JSON array, nothing else
"""


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

    # Remove markdown code fences (```json ... ``` or ``` ... ```)
    text = re.sub(r"^```\w*\s*\n?", "", text)
    text = re.sub(r"\n?```\s*$", "", text)
    text = text.strip()

    # If response was truncated mid-string, try to close it
    # Find the last complete JSON object
    if text and not text.endswith("]"):
        # Try to find the last complete entry and close the array
        last_brace = text.rfind("}")
        if last_brace > 0:
            text = text[:last_brace + 1] + "]"
            logger.warning("JSON response appeared truncated, attempted repair")

    return text


def _transcribe_regions(client: genai.Client, uploaded_file, speech_regions: list[dict]) -> list[dict]:
    """
    Send audio + speech region info to Gemini.
    Returns list of {"region": int, "text": str}.
    Gemini only needs to tell us WHAT is said — FFmpeg already told us WHEN.
    """
    prompt = _build_prompt(speech_regions)

    for attempt in range(2):
        response = client.models.generate_content(
            model=settings.caption_model,
            contents=[uploaded_file, prompt],
        )

        raw = response.text.strip()
        logger.info(f"Transcription response attempt {attempt+1} (first 500 chars): {raw[:500]}")

        text = _clean_json_response(raw)

        try:
            result = json.loads(text)
        except json.JSONDecodeError as e:
            logger.warning(f"JSON parse failed (attempt {attempt+1}): {e}")
            logger.warning(f"Raw response: {raw[:1000]}")
            if attempt == 0:
                continue  # retry once
            return []

        if not isinstance(result, list):
            logger.warning("Transcription returned non-list")
            return []

        return result

    return []


# ---------------------------------------------------------------------------
# Step 3: Combine FFmpeg timing + Gemini text → subtitle blocks
# ---------------------------------------------------------------------------

def _regions_to_blocks(speech_regions: list[dict], transcriptions: list[dict], max_words: int = 4) -> list[dict]:
    """
    Map Gemini text to FFmpeg-detected speech regions.
    Distributes words proportionally within each region's exact boundaries.

    This guarantees:
    - Captions NEVER appear before speech starts
    - Captions NEVER appear during silence
    - Smooth transitions within speech regions
    """
    blocks = []

    for entry in transcriptions:
        region_idx = entry.get("region", 0) - 1  # 1-indexed → 0-indexed
        text = entry.get("text", "").strip()

        if not text or region_idx < 0 or region_idx >= len(speech_regions):
            continue

        region = speech_regions[region_idx]
        start = region["start"]
        end = region["end"]
        duration = end - start

        words = text.split()
        if not words:
            continue

        total_words = len(words)
        word_idx = 0

        while word_idx < total_words:
            chunk = words[word_idx:word_idx + max_words]
            chunk_end_idx = min(word_idx + max_words, total_words)

            # Proportional timestamp distribution
            chunk_start = start + (word_idx / total_words) * duration
            chunk_end = start + (chunk_end_idx / total_words) * duration

            # Ensure minimum display time of 0.3s
            if chunk_end - chunk_start < 0.3:
                chunk_end = min(chunk_start + 0.3, end)

            blocks.append({
                "start": round(chunk_start, 2),
                "end": round(chunk_end, 2),
                "text": " ".join(chunk),
            })
            word_idx += max_words

    logger.info(f"Generated {len(blocks)} subtitle blocks from {len(transcriptions)} regions")
    return blocks


# ---------------------------------------------------------------------------
# Step 4: ASS generation
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

async def add_captions(video_path: str, work_dir: str) -> str:
    """
    Hybrid caption pipeline:
    1. FFmpeg extracts audio & detects speech regions (precise signal-level timing)
    2. Gemini transcribes text per region (no timestamp guessing)
    3. Words distributed proportionally within FFmpeg-detected boundaries
    4. ASS subtitle file generated and burned into video

    Returns path to captioned video (or original if no speech detected).
    """
    audio_path = os.path.join(work_dir, "caption-audio.aac")
    ass_path = os.path.join(work_dir, "captions.ass")
    captioned_path = os.path.join(work_dir, "captioned-" + os.path.basename(video_path))

    # Step 1: Extract audio
    await asyncio.to_thread(_extract_audio, video_path, audio_path)

    # Step 2: Detect speech regions with FFmpeg (signal-level precision)
    speech_regions = await asyncio.to_thread(_detect_speech_regions, audio_path)
    if not speech_regions:
        logger.info("No speech regions detected, skipping captions")
        return video_path

    # Step 3: Upload audio to Gemini and transcribe text per region
    client = genai.Client(api_key=settings.gemini_api_key)
    uploaded_file = await asyncio.to_thread(_upload_and_wait, client, audio_path)

    try:
        transcriptions = await asyncio.to_thread(
            _transcribe_regions, client, uploaded_file, speech_regions
        )
    finally:
        try:
            await asyncio.to_thread(client.files.delete, name=uploaded_file.name)
            logger.info(f"Cleaned up Gemini file: {uploaded_file.name}")
        except Exception as e:
            logger.warning(f"Failed to delete Gemini file: {e}")

    if not transcriptions:
        logger.info("No transcription returned, skipping captions")
        return video_path

    # Step 4: Combine FFmpeg timing + Gemini text → subtitle blocks
    blocks = _regions_to_blocks(speech_regions, transcriptions)
    if not blocks:
        logger.info("No subtitle blocks generated, skipping captions")
        return video_path

    # Step 5: Generate ASS file
    _generate_ass(blocks, ass_path)

    # Step 6: Burn captions into video
    await asyncio.to_thread(burn_captions, video_path, ass_path, captioned_path)

    return captioned_path

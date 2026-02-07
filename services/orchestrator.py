import asyncio
import logging
import os
import shutil
import tempfile

import httpx

from core.config import settings
from core.job_store import JobStep, get_job
from services.drive_service import download_file, upload_file
from services.gemini_service import AnalysisResult, Corte, analyze_video
from services.video_engine import Segment, VideoOptions, process_video
from utils.webhook_sender import send_webhook

logger = logging.getLogger(__name__)


def _update_job(job_id: str, status: JobStep, message: str) -> None:
    job = get_job(job_id)
    if job:
        job.update(status, message)


async def process_video_pipeline(
    job_id: str,
    file_id: str,
    webhook_url: str,
    gemini_prompt_instruction: str | None,
    drive_folder_id: str | None = None,
    options: "ProcessingOptions | None" = None,
    http_client: httpx.AsyncClient = None,
) -> None:
    work_dir = None
    try:
        # --- Setup ---
        os.makedirs(settings.temp_dir, exist_ok=True)
        work_dir = tempfile.mkdtemp(
            prefix=f"job-{job_id[:8]}-",
            dir=settings.temp_dir,
        )
        logger.info(f"[{job_id}] Pipeline started. Work dir: {work_dir}")

        # Build VideoOptions from API ProcessingOptions
        video_opts = VideoOptions(
            layout=options.layout if options else "blur_zoom",
            zoom_level=options.zoom_level if options else 1400,
            fade_duration=options.fade_duration if options else 1.0,
            width=options.width if options else 1080,
            height=options.height if options else 1920,
            mirror=options.mirror if options else False,
        )
        max_clips = options.max_clips if options else 1

        source_path = os.path.join(work_dir, "source.mp4")

        # --- Step 1: Download from Google Drive ---
        _update_job(job_id, JobStep.DOWNLOADING, "Baixando video...")
        await asyncio.to_thread(download_file, file_id, source_path)

        if not os.path.exists(source_path) or os.path.getsize(source_path) == 0:
            raise RuntimeError("Downloaded file is empty or missing")

        size_mb = os.path.getsize(source_path) / (1024 * 1024)
        _update_job(job_id, JobStep.DOWNLOADING, f"Download concluido ({size_mb:.1f} MB)")

        # --- Step 2: Analyze with Gemini ---
        _update_job(job_id, JobStep.ANALYZING, "Analisando video...")
        analysis: AnalysisResult = await analyze_video(
            video_path=source_path,
            custom_instruction=gemini_prompt_instruction,
            max_clips=max_clips,
        )
        cortes = analysis.cortes
        usage = analysis.usage

        if not cortes:
            raise RuntimeError("Nenhum corte viavel encontrado no video")

        _update_job(
            job_id, JobStep.ANALYZING,
            f"Analise concluida — {len(cortes)} corte(s) identificado(s)",
        )

        # --- Step 3: Process each corte with FFmpeg ---
        generated_clips = []

        for i, corte in enumerate(cortes):
            corte_label = f"Corte {corte.corte_number}/{len(cortes)}"
            _update_job(
                job_id, JobStep.PROCESSING,
                f"Gerando {corte_label}: '{corte.title}'...",
            )

            output_name = f"viral-{job_id[:8]}-corte{corte.corte_number}.mp4"
            output_path = os.path.join(work_dir, output_name)

            engine_segments = [
                Segment(start=s.start, end=s.end) for s in corte.segments
            ]

            await asyncio.to_thread(process_video, source_path, output_path, engine_segments, video_opts)

            if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
                logger.error(f"[{job_id}] Empty output for {corte_label}")
                continue

            output_size_mb = os.path.getsize(output_path) / (1024 * 1024)
            logger.info(f"[{job_id}] {corte_label} complete: {output_size_mb:.1f} MB")

            # --- Step 4: Upload each corte ---
            _update_job(
                job_id, JobStep.UPLOADING,
                f"Enviando {corte_label} para o Drive...",
            )

            drive_result = await asyncio.to_thread(
                upload_file,
                output_path,
                output_name,
                drive_folder_id,
            )

            generated_clips.append({
                "corte_number": corte.corte_number,
                "title": corte.title,
                "file_id": drive_result["id"],
                "file_name": drive_result["name"],
                "web_view_link": drive_result.get("webViewLink"),
                "segments": [
                    {"start": s.start, "end": s.end, "description": s.description}
                    for s in corte.segments
                ],
                "output_size_mb": round(output_size_mb, 2),
            })

        if not generated_clips:
            raise RuntimeError("Nenhum corte foi processado com sucesso")

        # --- Step 5: Send single webhook with all results ---
        _update_job(job_id, JobStep.SENDING_WEBHOOK, "Enviando resultados...")

        result_payload = {
            "total_clips": len(generated_clips),
            "generated_clips": generated_clips,
            "usage": usage.to_dict(),
        }

        await send_webhook(
            http_client=http_client,
            url=webhook_url,
            payload={
                "job_id": job_id,
                "status": "completed",
                "original_file_id": file_id,
                "result": result_payload,
            },
        )

        # Mark completed
        job = get_job(job_id)
        if job:
            job.update(JobStep.COMPLETED, f"Concluido! {len(generated_clips)} corte(s) gerado(s)")
            job.result = result_payload

        logger.info(f"[{job_id}] Pipeline completed: {len(generated_clips)} corte(s)")

    except Exception as e:
        logger.exception(f"[{job_id}] Pipeline failed: {e}")

        error_data = {"message": str(e), "type": type(e).__name__}

        job = get_job(job_id)
        if job:
            job.update(JobStep.ERROR, str(e))
            job.error = error_data

        try:
            await send_webhook(
                http_client=http_client,
                url=webhook_url,
                payload={
                    "job_id": job_id,
                    "status": "error",
                    "original_file_id": file_id,
                    "error": error_data,
                },
            )
        except Exception as webhook_err:
            logger.error(f"[{job_id}] Failed to send error webhook: {webhook_err}")

    finally:
        if work_dir and os.path.exists(work_dir):
            try:
                shutil.rmtree(work_dir)
                logger.info(f"[{job_id}] Cleaned up work dir: {work_dir}")
            except Exception as cleanup_err:
                logger.warning(
                    f"[{job_id}] Failed to clean up {work_dir}: {cleanup_err}"
                )


async def manual_cut_pipeline(
    job_id: str,
    file_id: str,
    webhook_url: str,
    clips: list[dict],
    drive_folder_id: str | None = None,
    options: "ProcessingOptions | None" = None,
    http_client: httpx.AsyncClient = None,
) -> None:
    """Manual cut pipeline — no AI, user provides exact timestamps."""
    work_dir = None
    try:
        # --- Setup ---
        os.makedirs(settings.temp_dir, exist_ok=True)
        work_dir = tempfile.mkdtemp(
            prefix=f"job-{job_id[:8]}-",
            dir=settings.temp_dir,
        )
        logger.info(f"[{job_id}] Manual cut started. Work dir: {work_dir}")

        video_opts = VideoOptions(
            layout=options.layout if options else "blur_zoom",
            zoom_level=options.zoom_level if options else 1400,
            fade_duration=options.fade_duration if options else 1.0,
            width=options.width if options else 1080,
            height=options.height if options else 1920,
            mirror=options.mirror if options else False,
        )

        source_path = os.path.join(work_dir, "source.mp4")

        # --- Step 1: Download from Google Drive ---
        _update_job(job_id, JobStep.DOWNLOADING, "Baixando video...")
        await asyncio.to_thread(download_file, file_id, source_path)

        if not os.path.exists(source_path) or os.path.getsize(source_path) == 0:
            raise RuntimeError("Downloaded file is empty or missing")

        size_mb = os.path.getsize(source_path) / (1024 * 1024)
        _update_job(job_id, JobStep.DOWNLOADING, f"Download concluido ({size_mb:.1f} MB)")

        # --- Step 2: Process each clip with FFmpeg ---
        generated_clips = []

        for i, clip in enumerate(clips):
            clip_num = i + 1
            clip_title = clip.get("title") or f"Clip {clip_num}"
            clip_label = f"Clip {clip_num}/{len(clips)}"

            _update_job(
                job_id, JobStep.PROCESSING,
                f"Gerando {clip_label}: '{clip_title}'...",
            )

            output_name = f"clip-{job_id[:8]}-{clip_num}.mp4"
            output_path = os.path.join(work_dir, output_name)

            engine_segments = [Segment(start=clip["start"], end=clip["end"])]

            await asyncio.to_thread(process_video, source_path, output_path, engine_segments, video_opts)

            if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
                logger.error(f"[{job_id}] Empty output for {clip_label}")
                continue

            output_size_mb = os.path.getsize(output_path) / (1024 * 1024)
            logger.info(f"[{job_id}] {clip_label} complete: {output_size_mb:.1f} MB")

            # --- Step 3: Upload each clip ---
            _update_job(
                job_id, JobStep.UPLOADING,
                f"Enviando {clip_label} para o Drive...",
            )

            drive_result = await asyncio.to_thread(
                upload_file,
                output_path,
                output_name,
                drive_folder_id,
            )

            generated_clips.append({
                "clip_number": clip_num,
                "title": clip_title,
                "file_id": drive_result["id"],
                "file_name": drive_result["name"],
                "web_view_link": drive_result.get("webViewLink"),
                "start": clip["start"],
                "end": clip["end"],
                "output_size_mb": round(output_size_mb, 2),
            })

        if not generated_clips:
            raise RuntimeError("Nenhum clip foi processado com sucesso")

        # --- Step 4: Send webhook with all results ---
        _update_job(job_id, JobStep.SENDING_WEBHOOK, "Enviando resultados...")

        result_payload = {
            "total_clips": len(generated_clips),
            "generated_clips": generated_clips,
        }

        await send_webhook(
            http_client=http_client,
            url=webhook_url,
            payload={
                "job_id": job_id,
                "status": "completed",
                "original_file_id": file_id,
                "result": result_payload,
            },
        )

        job = get_job(job_id)
        if job:
            job.update(JobStep.COMPLETED, f"Concluido! {len(generated_clips)} clip(s) gerado(s)")
            job.result = result_payload

        logger.info(f"[{job_id}] Manual cut completed: {len(generated_clips)} clip(s)")

    except Exception as e:
        logger.exception(f"[{job_id}] Manual cut failed: {e}")

        error_data = {"message": str(e), "type": type(e).__name__}

        job = get_job(job_id)
        if job:
            job.update(JobStep.ERROR, str(e))
            job.error = error_data

        try:
            await send_webhook(
                http_client=http_client,
                url=webhook_url,
                payload={
                    "job_id": job_id,
                    "status": "error",
                    "original_file_id": file_id,
                    "error": error_data,
                },
            )
        except Exception as webhook_err:
            logger.error(f"[{job_id}] Failed to send error webhook: {webhook_err}")

    finally:
        if work_dir and os.path.exists(work_dir):
            try:
                shutil.rmtree(work_dir)
                logger.info(f"[{job_id}] Cleaned up work dir: {work_dir}")
            except Exception as cleanup_err:
                logger.warning(
                    f"[{job_id}] Failed to clean up {work_dir}: {cleanup_err}"
                )

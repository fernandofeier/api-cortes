import logging
import time
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger(__name__)

JOB_TTL_SECONDS = 3 * 24 * 60 * 60  # 3 days


class JobStep(str, Enum):
    QUEUED = "queued"
    DOWNLOADING = "downloading"
    ANALYZING = "analyzing"
    PROCESSING = "processing"
    UPLOADING = "uploading"
    SENDING_WEBHOOK = "finishing"
    COMPLETED = "completed"
    ERROR = "error"


@dataclass
class Job:
    job_id: str
    file_id: str
    webhook_url: str
    status: JobStep = JobStep.QUEUED
    progress_message: str = "Job queued"
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    result: dict | None = None
    error: dict | None = None

    def update(self, status: JobStep, message: str) -> None:
        self.status = status
        self.progress_message = message
        self.updated_at = time.time()

    def to_dict(self) -> dict:
        elapsed = round(time.time() - self.created_at, 1)
        data = {
            "job_id": self.job_id,
            "status": self.status.value,
            "progress_message": self.progress_message,
            "elapsed_seconds": elapsed,
        }
        if self.result:
            data["result"] = self.result
        if self.error:
            data["error"] = self.error
        return data


# In-memory store — resets on container restart
_jobs: dict[str, Job] = {}


def create_job(job_id: str, file_id: str, webhook_url: str) -> Job:
    job = Job(job_id=job_id, file_id=file_id, webhook_url=webhook_url)
    _jobs[job_id] = job
    return job


def get_job(job_id: str) -> Job | None:
    return _jobs.get(job_id)


def cleanup_old_jobs() -> int:
    """Remove jobs older than JOB_TTL_SECONDS. Returns number of removed jobs."""
    now = time.time()
    expired = [
        jid for jid, job in _jobs.items()
        if now - job.created_at > JOB_TTL_SECONDS
    ]
    for jid in expired:
        del _jobs[jid]
    if expired:
        logger.info(f"Cleaned up {len(expired)} expired job(s)")
    return len(expired)

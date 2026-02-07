import asyncio
import logging

import httpx

from core.config import settings

logger = logging.getLogger(__name__)


async def send_webhook(
    http_client: httpx.AsyncClient,
    url: str,
    payload: dict,
) -> None:
    """
    Send a webhook POST with exponential backoff retry.

    Retries on network errors, 5xx, and 429.
    Does NOT retry on 4xx (except 429) — those indicate config problems.
    Raises after all retries exhausted.
    """
    max_retries = settings.webhook_max_retries
    base_delay = settings.webhook_retry_base_delay
    last_exception: Exception | None = None

    for attempt in range(max_retries + 1):
        try:
            logger.info(
                f"Webhook POST to {url} (attempt {attempt + 1}/{max_retries + 1})"
            )
            response = await http_client.post(
                url,
                json=payload,
                timeout=settings.webhook_timeout,
            )

            if response.status_code < 300:
                logger.info(f"Webhook delivered: {response.status_code}")
                return

            if response.status_code == 429 or response.status_code >= 500:
                logger.warning(
                    f"Webhook returned {response.status_code}, will retry"
                )
                last_exception = RuntimeError(
                    f"Webhook status {response.status_code}: "
                    f"{response.text[:200]}"
                )
            else:
                # 4xx (not 429) — client config error, no retry
                logger.error(
                    f"Webhook client error {response.status_code}: "
                    f"{response.text[:200]}. Not retrying."
                )
                raise RuntimeError(
                    f"Webhook status {response.status_code}: "
                    f"{response.text[:200]}"
                )

        except httpx.RequestError as e:
            logger.warning(f"Webhook network error: {e}")
            last_exception = e

        # Exponential backoff: 2s, 4s, 8s, ...
        if attempt < max_retries:
            delay = base_delay * (2 ** attempt)
            logger.info(f"Retrying webhook in {delay:.1f}s...")
            await asyncio.sleep(delay)

    logger.error(f"Webhook delivery failed after {max_retries + 1} attempts")
    raise RuntimeError(
        f"Webhook delivery to {url} failed after {max_retries + 1} attempts. "
        f"Last error: {last_exception}"
    )

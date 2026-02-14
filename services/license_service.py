import logging
from datetime import datetime, timezone

import httpx

from core.config import settings

logger = logging.getLogger(__name__)


class LicenseStatus:
    def __init__(self):
        self.valid: bool = False
        self.user_name: str = ""
        self.expires_at: datetime | None = None
        self.last_check: datetime | None = None


# Global cache — validated on startup, re-checked every hour
_license_cache = LicenseStatus()


def _supabase_headers() -> dict[str, str]:
    return {
        "apikey": settings.supabase_anon_key,
        "Authorization": f"Bearer {settings.supabase_anon_key}",
        "Content-Type": "application/json",
    }


def is_configured() -> bool:
    """Check if license key is set."""
    return bool(settings.license_key)


async def validate_license(license_key: str) -> LicenseStatus:
    """Validate license_key against Supabase RPC function (SECURITY DEFINER)."""
    url = f"{settings.supabase_url}/rest/v1/rpc/validate_license"

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                url,
                headers=_supabase_headers(),
                json={"p_key": license_key, "p_api": settings.api_slug},
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        logger.warning(f"License validation request failed: {e}")
        # On network error, keep current cache (grace period)
        if _license_cache.last_check is not None:
            logger.info("Keeping cached license status (grace period)")
            return _license_cache
        _license_cache.valid = False
        return _license_cache

    # RPC returns null when no matching row found
    if not data:
        logger.warning("License key not found, inactive, or not authorized for this API")
        _license_cache.valid = False
        return _license_cache

    # Check expiration (double-check client-side, even though DB already filters)
    if data.get("expires_at"):
        exp = datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00"))
        if exp < datetime.now(timezone.utc):
            logger.warning(f"License expired at {exp.isoformat()}")
            _license_cache.valid = False
            return _license_cache
        _license_cache.expires_at = exp
    else:
        _license_cache.expires_at = None  # No expiration (lifetime)

    _license_cache.valid = True
    _license_cache.user_name = data.get("user_name", "")
    _license_cache.last_check = datetime.now(timezone.utc)

    # Record last validation timestamp (fire-and-forget)
    try:
        record_url = f"{settings.supabase_url}/rest/v1/rpc/record_validation"
        async with httpx.AsyncClient(timeout=5) as client:
            await client.post(
                record_url,
                headers=_supabase_headers(),
                json={"p_key": license_key},
            )
    except Exception:
        pass  # non-critical

    return _license_cache


def get_cached_license() -> LicenseStatus:
    """Return the cached license status (no network call)."""
    return _license_cache

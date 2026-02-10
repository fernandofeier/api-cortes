from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # --- Google Gemini ---
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3-flash-preview"

    # --- Google Drive (OAuth2) ---
    google_drive_token_json: str = "/app/credentials/token.json"
    google_drive_client_secret_json: str = "/app/credentials/client_secret.json"

    # --- FFmpeg ---
    ffmpeg_path: str = "ffmpeg"
    output_fps: int = 30
    video_bitrate: str = "5M"
    audio_bitrate: str = "192k"

    # --- Webhook ---
    webhook_timeout: float = 30.0
    webhook_max_retries: int = 3
    webhook_retry_base_delay: float = 2.0

    # --- Limits ---
    multi_clip_min_video_duration: int = 600  # 10 minutes in seconds
    max_upload_size_mb: int = 2000  # Gemini File API supports up to 2 GB

    # --- DeepInfra (optional — Whisper captions) ---
    deepinfra_api_key: str = ""

    # --- Telegram Bot (optional) ---
    telegram_bot_token: str = ""
    telegram_api_id: int = 0
    telegram_api_hash: str = ""
    telegram_allowed_users: str = ""
    telegram_default_drive_folder: str = ""
    telegram_default_webhook_url: str = ""

    # --- Auth ---
    api_key: str = "changeme-default-key-2024"

    # --- App ---
    app_title: str = "Viral Video Cutter API"
    app_version: str = "1.0.0"
    app_base_url: str = "http://localhost:8000"
    temp_dir: str = "/tmp/video-cutter"
    log_level: str = "INFO"


settings = Settings()

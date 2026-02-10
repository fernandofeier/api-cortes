import asyncio
import logging
import os
import shutil
import tempfile
from datetime import datetime, timezone

import httpx
from pyrogram import Client, filters
from pyrogram.types import Message

from core.config import settings
from services.drive_service import upload_file

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Per-user folder mapping (in-memory, resets on restart)
# ---------------------------------------------------------------------------
_user_folders: dict[int, str] = {}
_user_webhooks: dict[int, str] = {}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _get_allowed_users() -> set[int]:
    raw = settings.telegram_allowed_users.strip()
    if not raw:
        return set()
    return {int(uid.strip()) for uid in raw.split(",") if uid.strip().isdigit()}


def _is_authorized(user_id: int) -> bool:
    allowed = _get_allowed_users()
    if not allowed:
        return False
    return user_id in allowed


def _get_folder_for_user(user_id: int) -> str | None:
    return _user_folders.get(user_id) or settings.telegram_default_drive_folder or None


def _get_webhook_for_user(user_id: int) -> str | None:
    return _user_webhooks.get(user_id) or settings.telegram_default_webhook_url or None


def _format_size(size_bytes: int) -> str:
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    if size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"


# ---------------------------------------------------------------------------
# Bot client (module-level singleton)
# ---------------------------------------------------------------------------
_client: Client | None = None


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------
async def _handle_start(client: Client, message: Message):
    if not _is_authorized(message.from_user.id):
        await message.reply_text("Voce nao tem permissao para usar este bot.")
        return

    folder = _get_folder_for_user(message.from_user.id)
    folder_text = f"`{folder}`" if folder else "raiz do Drive"

    webhook = _get_webhook_for_user(message.from_user.id)
    webhook_text = f"`{webhook}`" if webhook else "nenhum"

    await message.reply_text(
        "Bem-vindo ao **Video Uploader Bot**!\n\n"
        "Envie um video ou documento e ele sera enviado "
        "automaticamente para o Google Drive.\n\n"
        "**Comandos:**\n"
        "/pasta `<folder_id>` — Define a pasta do Drive\n"
        "/pasta — Mostra a pasta atual\n"
        "/webhook `<url>` — Define URL de notificacao\n"
        "/webhook — Mostra o webhook atual\n"
        "/webhook off — Desativa o webhook\n\n"
        f"**Pasta atual:** {folder_text}\n"
        f"**Webhook:** {webhook_text}",
    )


async def _handle_pasta(client: Client, message: Message):
    if not _is_authorized(message.from_user.id):
        await message.reply_text("Voce nao tem permissao para usar este bot.")
        return

    user_id = message.from_user.id
    parts = message.text.strip().split(maxsplit=1)

    if len(parts) < 2:
        folder = _get_folder_for_user(user_id)
        if folder:
            await message.reply_text(f"Pasta atual: `{folder}`")
        else:
            await message.reply_text(
                "Nenhuma pasta configurada. Uploads vao para a raiz do Drive.\n"
                "Use /pasta <folder_id> para definir."
            )
        return

    folder_id = parts[1].strip()
    _user_folders[user_id] = folder_id
    await message.reply_text(f"Pasta definida: `{folder_id}`")
    logger.info(f"User {user_id} set Drive folder to {folder_id}")


async def _handle_webhook(client: Client, message: Message):
    if not _is_authorized(message.from_user.id):
        await message.reply_text("Voce nao tem permissao para usar este bot.")
        return

    user_id = message.from_user.id
    parts = message.text.strip().split(maxsplit=1)

    if len(parts) < 2:
        webhook = _get_webhook_for_user(user_id)
        if webhook:
            await message.reply_text(f"Webhook atual: `{webhook}`")
        else:
            await message.reply_text(
                "Nenhum webhook configurado.\n"
                "Use /webhook <url> para definir."
            )
        return

    value = parts[1].strip()

    if value.lower() == "off":
        _user_webhooks.pop(user_id, None)
        await message.reply_text("Webhook desativado.")
        logger.info(f"User {user_id} disabled webhook")
        return

    if not value.startswith(("http://", "https://")):
        await message.reply_text("URL invalida. Use uma URL que comece com http:// ou https://")
        return

    _user_webhooks[user_id] = value
    await message.reply_text(f"Webhook definido: `{value}`")
    logger.info(f"User {user_id} set webhook to {value}")


async def _send_webhook_notification(
    webhook_url: str,
    drive_result: dict,
    file_name: str,
    file_size: int,
    caption: str | None,
    user_id: int,
    message_id: int = 0,
    duration: int = 0,
) -> None:
    payload = {
        "event": "telegram_upload",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "user_id": user_id,
        "message_id": message_id,
        "file": {
            "name": file_name,
            "size_bytes": file_size,
            "size_human": _format_size(file_size),
            "duration": duration,
        },
        "drive": {
            "file_id": drive_result.get("id"),
            "file_name": drive_result.get("name"),
            "web_view_link": drive_result.get("webViewLink"),
        },
        "caption": caption,
    }

    try:
        async with httpx.AsyncClient(timeout=settings.webhook_timeout) as client:
            resp = await client.post(webhook_url, json=payload)
            logger.info(f"Webhook sent to {webhook_url} — status {resp.status_code}")
    except Exception as e:
        logger.warning(f"Webhook to {webhook_url} failed: {e}")


async def _handle_video(client: Client, message: Message):
    if not _is_authorized(message.from_user.id):
        await message.reply_text("Voce nao tem permissao para usar este bot.")
        return

    user_id = message.from_user.id
    media = message.video or message.document
    if not media:
        await message.reply_text("Envie um video ou arquivo para fazer upload ao Drive.")
        return

    file_name = media.file_name or f"telegram-video-{message.id}.mp4"
    file_size = media.file_size or 0
    size_text = _format_size(file_size) if file_size else "tamanho desconhecido"
    caption = message.caption

    logger.info(f"User {user_id} sent file: {file_name} ({size_text})")

    status_msg = await message.reply_text(f"Baixando `{file_name}` ({size_text})...")

    work_dir = None
    try:
        os.makedirs(settings.temp_dir, exist_ok=True)
        work_dir = tempfile.mkdtemp(prefix=f"tg-{user_id}-", dir=settings.temp_dir)
        local_path = os.path.join(work_dir, file_name)

        # Download from Telegram via MTProto
        await message.download(file_name=local_path)

        actual_size = os.path.getsize(local_path)
        logger.info(f"Downloaded {file_name}: {_format_size(actual_size)}")

        # Upload to Google Drive
        await status_msg.edit_text(f"Enviando `{file_name}` para o Drive...")

        folder_id = _get_folder_for_user(user_id)
        drive_result = await asyncio.to_thread(
            upload_file, local_path, file_name, folder_id,
        )

        link = drive_result.get("webViewLink", "link indisponivel")
        drive_name = drive_result.get("name", file_name)
        drive_id = drive_result.get("id", "?")

        webhook_url = _get_webhook_for_user(user_id)
        webhook_status = ""
        if webhook_url:
            duration = getattr(media, "duration", 0) or 0
            await _send_webhook_notification(
                webhook_url=webhook_url,
                drive_result=drive_result,
                file_name=file_name,
                file_size=actual_size,
                caption=caption,
                user_id=user_id,
                message_id=message.id,
                duration=duration,
            )
            webhook_status = "\n**Webhook:** enviado"

        await status_msg.edit_text(
            f"Concluido!\n\n"
            f"**Arquivo:** `{drive_name}`\n"
            f"**Tamanho:** {_format_size(actual_size)}\n"
            f"**Drive ID:** `{drive_id}`\n"
            f"**Link:** {link}{webhook_status}",
        )
        logger.info(f"Upload complete: {drive_name} -> {link}")

    except Exception as e:
        logger.exception(f"Telegram upload failed for user {user_id}: {e}")
        try:
            await status_msg.edit_text(f"Erro ao processar o arquivo:\n`{e}`")
        except Exception:
            pass

    finally:
        if work_dir and os.path.exists(work_dir):
            try:
                shutil.rmtree(work_dir)
            except Exception as cleanup_err:
                logger.warning(f"Failed to clean up {work_dir}: {cleanup_err}")


# ---------------------------------------------------------------------------
# Public API: start / stop
# ---------------------------------------------------------------------------
async def start_telegram_bot() -> bool:
    global _client

    if not settings.telegram_bot_token:
        logger.info("Telegram bot: TELEGRAM_BOT_TOKEN not set, skipping.")
        return False
    if not settings.telegram_api_id:
        logger.info("Telegram bot: TELEGRAM_API_ID not set, skipping.")
        return False
    if not settings.telegram_api_hash:
        logger.info("Telegram bot: TELEGRAM_API_HASH not set, skipping.")
        return False

    allowed = _get_allowed_users()
    if not allowed:
        logger.warning(
            "Telegram bot: TELEGRAM_ALLOWED_USERS is empty. "
            "Bot will start but reject all messages."
        )

    try:
        _client = Client(
            name="api_cortes_bot",
            api_id=settings.telegram_api_id,
            api_hash=settings.telegram_api_hash,
            bot_token=settings.telegram_bot_token,
            workdir=settings.temp_dir,
        )

        # Register handlers
        _client.on_message(filters.command("start") & filters.private)(_handle_start)
        _client.on_message(filters.command("pasta") & filters.private)(_handle_pasta)
        _client.on_message(filters.command("webhook") & filters.private)(_handle_webhook)
        _client.on_message(
            (filters.video | filters.document) & filters.private
        )(_handle_video)

        await _client.start()
        me = await _client.get_me()
        logger.info(
            f"Telegram bot started: @{me.username} (ID: {me.id}). "
            f"Allowed users: {allowed or 'NONE (all denied)'}"
        )
        return True

    except Exception as e:
        logger.error(f"Failed to start Telegram bot: {e}")
        _client = None
        return False


async def stop_telegram_bot() -> None:
    global _client
    if _client:
        try:
            await _client.stop()
            logger.info("Telegram bot stopped.")
        except Exception as e:
            logger.warning(f"Error stopping Telegram bot: {e}")
        finally:
            _client = None

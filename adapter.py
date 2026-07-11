from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import mimetypes
import os
import secrets
import tempfile
import textwrap
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
    cache_audio_from_bytes,
    cache_document_from_bytes,
    cache_image_from_bytes,
)
from gateway.platforms.helpers import MessageDeduplicator
from hermes_cli.config import get_hermes_home
from utils import atomic_json_write

from .crypto import _aes128_ecb_encrypt, _aes_padded_size, _cdn_upload_url
from .ilink_api import (
    AIOHTTP_AVAILABLE,
    API_TIMEOUT_MS,
    BACKOFF_DELAY_SECONDS,
    CONFIG_TIMEOUT_MS,
    ILINK_BASE_URL,
    ITEM_FILE,
    ITEM_IMAGE,
    ITEM_TEXT,
    ITEM_VIDEO,
    ITEM_VOICE,
    LONG_POLL_TIMEOUT_MS,
    MAX_CONSECUTIVE_FAILURES,
    MEDIA_FILE,
    MEDIA_IMAGE,
    MEDIA_VIDEO,
    MEDIA_VOICE,
    MESSAGE_DEDUP_TTL_SECONDS,
    MSG_STATE_FINISH,
    MSG_TYPE_BOT,
    RATE_LIMIT_ERRCODE,
    RETRY_DELAY_SECONDS,
    SESSION_EXPIRED_ERRCODE,
    TYPING_START,
    TYPING_STOP,
    WEIXIN_CDN_BASE_URL,
    _api_post,
    _download_and_decrypt_media,
    _get_config,
    _get_updates,
    _get_upload_url,
    _guess_chat_type,
    _is_stale_session_ret,
    _send_message,
    _send_typing,
    _should_split_short_chat_block_for_weixin,
    _upload_ciphertext,
    check_weixin_requirements,
)
from .token_store import ContextTokenStore

logger = logging.getLogger(__name__)

try:
    import aiohttp
except ImportError:  # pragma: no cover
    aiohttp = None  # type: ignore[assignment]

WEIXIN_COPY_LINE_WIDTH = 120
_FENCE = "```"


def _safe_id(value: Optional[str], keep: int = 8) -> str:
    raw = str(value or "").strip()
    if not raw:
        return "?"
    return raw if len(raw) <= keep else raw[:keep]


def _coerce_bool(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _normalize_markdown_blocks(content: str) -> str:
    lines = content.splitlines()
    result: List[str] = []
    in_code_block = False
    blank_run = 0
    for raw_line in lines:
        line = raw_line.rstrip()
        if line.strip().startswith(_FENCE):
            in_code_block = not in_code_block
            result.append(line)
            blank_run = 0
            continue
        if in_code_block:
            result.append(line)
            continue
        if not line.strip():
            blank_run += 1
            if blank_run <= 1:
                result.append("")
            continue
        blank_run = 0
        result.append(line)
    return "\n".join(result).strip()


def _wrap_copy_friendly_lines_for_weixin(content: str) -> str:
    if not content:
        return content
    wrapped: List[str] = []
    in_code_block = False
    for raw_line in content.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if stripped.startswith(_FENCE):
            in_code_block = not in_code_block
            wrapped.append(line)
            continue
        if in_code_block or len(line) <= WEIXIN_COPY_LINE_WIDTH or not stripped or stripped.startswith("|"):
            wrapped.append(line)
            continue
        wrapped.extend(
            textwrap.wrap(
                line,
                width=WEIXIN_COPY_LINE_WIDTH,
                break_long_words=False,
                break_on_hyphens=False,
                replace_whitespace=False,
                drop_whitespace=True,
            )
            or [line]
        )
    return "\n".join(wrapped).strip()


def _split_markdown_blocks(content: str) -> List[str]:
    if not content:
        return []
    blocks: List[str] = []
    current: List[str] = []
    in_code = False
    for raw_line in content.splitlines():
        line = raw_line.rstrip()
        if line.strip().startswith(_FENCE):
            if not in_code and current:
                blocks.append("\n".join(current).strip())
                current = []
            current.append(line)
            in_code = not in_code
            if not in_code:
                blocks.append("\n".join(current).strip())
                current = []
            continue
        if in_code:
            current.append(line)
            continue
        if not line.strip():
            if current:
                blocks.append("\n".join(current).strip())
                current = []
            continue
        current.append(line)
    if current:
        blocks.append("\n".join(current).strip())
    return [block for block in blocks if block]


def _pack_markdown_blocks_for_weixin(content: str, max_length: int) -> List[str]:
    if len(content) <= max_length:
        return [content]
    packed: List[str] = []
    current = ""
    for block in _split_markdown_blocks(content):
        candidate = block if not current else f"{current}\n\n{block}"
        if len(candidate) <= max_length:
            current = candidate
            continue
        if current:
            packed.append(current)
            current = ""
        if len(block) <= max_length:
            current = block
            continue
        packed.extend(BasePlatformAdapter.truncate_message(block, max_length))
    if current:
        packed.append(current)
    return packed


def _split_delivery_units_for_weixin(content: str) -> List[str]:
    units: List[str] = []
    for block in _split_markdown_blocks(content):
        if block.splitlines()[0].strip().startswith(_FENCE):
            units.append(block)
            continue
        current: List[str] = []
        for raw_line in block.splitlines():
            line = raw_line.rstrip()
            if not line.strip():
                if current:
                    units.append("\n".join(current).strip())
                    current = []
                continue
            is_continuation = bool(current) and raw_line.startswith((" ", "\t"))
            if is_continuation:
                current.append(line)
                continue
            if current:
                units.append("\n".join(current).strip())
            current = [line]
        if current:
            units.append("\n".join(current).strip())
    return [unit for unit in units if unit]


def _split_text_for_weixin_delivery(content: str, max_length: int, split_per_line: bool = False) -> List[str]:
    if not content:
        return []
    if split_per_line:
        if len(content) <= max_length and "\n" not in content:
            return [content]
        chunks: List[str] = []
        for unit in _split_delivery_units_for_weixin(content):
            if len(unit) <= max_length:
                chunks.append(unit)
            else:
                chunks.extend(_pack_markdown_blocks_for_weixin(unit, max_length))
        return [c for c in chunks if c] or [content]
    if len(content) <= max_length:
        return [u for u in _split_delivery_units_for_weixin(content) if u] if _should_split_short_chat_block_for_weixin(content) else [content]
    return _pack_markdown_blocks_for_weixin(content, max_length) or [content]


def _extract_text(item_list: List[Dict[str, Any]]) -> str:
    for item in item_list:
        if item.get("type") == ITEM_TEXT:
            text = str((item.get("text_item") or {}).get("text") or "")
            ref = item.get("ref_msg") or {}
            ref_item = ref.get("message_item") or {}
            ref_type = ref_item.get("type")
            if ref_type in {ITEM_IMAGE, ITEM_VIDEO, ITEM_FILE, ITEM_VOICE}:
                title = ref.get("title") or ""
                prefix = f"[quoted media: {title}]\n" if title else "[quoted media]\n"
                return f"{prefix}{text}".strip()
            return text
    for item in item_list:
        if item.get("type") == ITEM_VOICE:
            voice_text = str((item.get("voice_item") or {}).get("text") or "")
            if voice_text:
                return voice_text
    return ""


def _message_type_from_media(media_types: List[str], text: str) -> MessageType:
    if any(m.startswith("image/") for m in media_types):
        return MessageType.PHOTO
    if any(m.startswith("video/") for m in media_types):
        return MessageType.VIDEO
    if any(m.startswith("audio/") for m in media_types):
        return MessageType.VOICE
    if media_types:
        return MessageType.DOCUMENT
    if text.startswith("/"):
        return MessageType.COMMAND
    return MessageType.TEXT


@dataclass
class TypingTicketCache:
    ttl_seconds: float = 600.0

    def __post_init__(self) -> None:
        self._cache: Dict[str, Tuple[str, float]] = {}

    def get(self, user_id: str) -> Optional[str]:
        entry = self._cache.get(user_id)
        if not entry:
            return None
        if time.time() - entry[1] >= self.ttl_seconds:
            self._cache.pop(user_id, None)
            return None
        return entry[0]

    def set(self, user_id: str, ticket: str) -> None:
        self._cache[user_id] = (ticket, time.time())


def _make_ssl_connector() -> Optional["aiohttp.TCPConnector"]:
    try:
        import ssl
        import certifi
    except ImportError:
        return None
    if not AIOHTTP_AVAILABLE:
        return None
    return aiohttp.TCPConnector(ssl=ssl.create_default_context(cafile=certifi.where()))


class WeixinMultiAdapter(BasePlatformAdapter):
    supports_code_blocks = True
    MAX_MESSAGE_LENGTH = 2000
    SUPPORTS_MESSAGE_EDITING = False

    def __init__(self, config: PlatformConfig):
        platform_name = str((config.extra or {}).get("platform_name") or "weixin-multi")
        super().__init__(config, Platform(platform_name))
        extra = config.extra or {}
        self._platform_name = platform_name
        self._instance_suffix = platform_name[len("weixin-") :] if platform_name.startswith("weixin-") else platform_name
        self._env_prefix = f"WEIXIN_{self._instance_suffix.upper().replace('-', '_')}"
        self._hermes_home = Path(get_hermes_home())
        self._instance_root = self._hermes_home / "weixin-multi"
        self._instance_root.mkdir(parents=True, exist_ok=True)
        self._token_store = ContextTokenStore(self._instance_root, self._instance_suffix)
        self._typing_cache = TypingTicketCache()
        self._poll_session: Optional[aiohttp.ClientSession] = None
        self._send_session: Optional[aiohttp.ClientSession] = None
        self._poll_task: Optional[asyncio.Task] = None
        self._dedup = MessageDeduplicator(ttl_seconds=MESSAGE_DEDUP_TTL_SECONDS)

        self._account_id = self._get_config_value(extra, "account_id", default="").strip()
        self._token = str(config.token or self._get_config_value(extra, "token", default="")).strip()
        self._base_url = self._get_config_value(extra, "base_url", default=ILINK_BASE_URL).strip().rstrip("/")
        self._cdn_base_url = self._get_config_value(extra, "cdn_base_url", default=WEIXIN_CDN_BASE_URL).strip().rstrip("/")
        self._send_chunk_delay_seconds = float(self._get_config_value(extra, "send_chunk_delay_seconds", default="1.5"))
        self._send_chunk_retries = int(self._get_config_value(extra, "send_chunk_retries", default="4"))
        self._send_chunk_retry_delay_seconds = float(self._get_config_value(extra, "send_chunk_retry_delay_seconds", default="1.0"))
        self._send_text_gate = asyncio.Lock()
        self._dm_policy = str(self._get_config_value(extra, "dm_policy", default="open")).strip().lower()
        self._group_policy = str(self._get_config_value(extra, "group_policy", default="disabled")).strip().lower()
        allowed_users = extra.get("allowed_users", extra.get("allow_from"))
        if allowed_users is None:
            allowed_users = os.getenv(f"{self._env_prefix}_ALLOWED_USERS", "")
        self._allow_from = self._coerce_list(allowed_users)
        group_allow = extra.get("group_allowed_users", extra.get("group_allow_from"))
        self._group_allow_from = self._coerce_list(group_allow)
        self._split_multiline_messages = _coerce_bool(extra.get("split_multiline_messages"), default=False)
        self._text_batch_delay_seconds = self._coerce_float_extra("text_batch_delay_seconds", 3.0)
        self._text_batch_split_delay_seconds = self._coerce_float_extra("text_batch_split_delay_seconds", 5.0)
        self._pending_text_batches: Dict[str, MessageEvent] = {}
        self._pending_text_batch_tasks: Dict[str, asyncio.Task] = {}

    def _get_config_value(self, extra: Dict[str, Any], key: str, default: str) -> str:
        env_name = f"{self._env_prefix}_{key.upper()}"
        env_value = os.getenv(env_name)
        if env_value not in {None, ""}:
            return str(env_value)
        value = extra.get(key)
        if value in {None, ""}:
            return str(default)
        return str(value)

    def _coerce_float_extra(self, key: str, default: float) -> float:
        import math

        value = self.config.extra.get(key) if getattr(self.config, "extra", None) else None
        if value is None:
            return float(default)
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return float(default)
        if not math.isfinite(parsed) or parsed < 0:
            return float(default)
        return parsed

    @staticmethod
    def _coerce_list(value: Any) -> List[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        if isinstance(value, (list, tuple, set)):
            return [str(item).strip() for item in value if str(item).strip()]
        return [str(value).strip()] if str(value).strip() else []

    def _sync_buf_path(self) -> Path:
        return self._instance_root / f"{self._instance_suffix}.sync.json"

    def _load_sync_buf(self) -> str:
        path = self._sync_buf_path()
        if not path.exists():
            return ""
        try:
            return json.loads(path.read_text(encoding="utf-8")).get("get_updates_buf", "")
        except Exception:
            return ""

    def _save_sync_buf(self, sync_buf: str) -> None:
        atomic_json_write(self._sync_buf_path(), {"get_updates_buf": sync_buf})

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        # ``is_reconnect`` is part of Hermes' platform adapter contract. The
        # iLink sync cursor is persisted independently, so no special handling
        # is needed here.
        if not check_weixin_requirements():
            self._set_fatal_error("weixin_missing_dependency", "Weixin startup failed: aiohttp is required", retryable=False)
            return False
        if not self._token:
            self._set_fatal_error("weixin_missing_token", f"{self._platform_name} startup failed: token is required", retryable=False)
            return False
        if not self._account_id:
            self._set_fatal_error("weixin_missing_account", f"{self._platform_name} startup failed: account_id is required", retryable=False)
            return False
        try:
            if not self._acquire_platform_lock(f"weixin-bot-token-{self._instance_suffix}", self._token, "Weixin bot token"):
                return False
        except Exception as exc:
            logger.debug("[%s] token lock unavailable (non-fatal): %s", self.name, exc)
        self._poll_session = aiohttp.ClientSession(trust_env=True, connector=_make_ssl_connector())
        no_timeout = aiohttp.ClientTimeout(total=None, connect=None, sock_connect=None, sock_read=None)
        self._send_session = aiohttp.ClientSession(trust_env=True, connector=_make_ssl_connector(), timeout=no_timeout)
        self._token_store.restore()
        self._poll_task = asyncio.create_task(self._poll_loop(), name=f"{self._platform_name}-poll")
        self._mark_connected()
        if self._group_policy != "disabled":
            logger.warning("[%s] group_policy=%s is enabled, but ordinary WeChat group delivery may still be unavailable on iLink bot identities.", self.name, self._group_policy)
        return True

    async def disconnect(self) -> None:
        self._running = False
        for task in self._pending_text_batch_tasks.values():
            if not task.done():
                task.cancel()
        self._pending_text_batches.clear()
        self._pending_text_batch_tasks.clear()
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
        self._poll_task = None
        if self._poll_session and not self._poll_session.closed:
            await self._poll_session.close()
        if self._send_session and not self._send_session.closed:
            await self._send_session.close()
        self._poll_session = None
        self._send_session = None
        self._release_platform_lock()
        self._mark_disconnected()

    async def _poll_loop(self) -> None:
        assert self._poll_session is not None
        sync_buf = self._load_sync_buf()
        timeout_ms = LONG_POLL_TIMEOUT_MS
        consecutive_failures = 0
        logger.info("[%s] poll loop started, account=%s", self.name, _safe_id(self._account_id))
        while self._running:
            try:
                response = await _get_updates(self._poll_session, base_url=self._base_url, token=self._token, sync_buf=sync_buf, timeout_ms=timeout_ms)
                msgs = response.get("msgs") or []
                if msgs:
                    logger.info("[%s] poll returned %d message(s)", self.name, len(msgs))
                suggested_timeout = response.get("longpolling_timeout_ms")
                if isinstance(suggested_timeout, int) and suggested_timeout > 0:
                    timeout_ms = suggested_timeout
                ret = response.get("ret", 0)
                errcode = response.get("errcode", 0)
                if ret not in {0, None} or errcode not in {0, None}:
                    if ret == SESSION_EXPIRED_ERRCODE or errcode == SESSION_EXPIRED_ERRCODE or _is_stale_session_ret(ret, errcode, response.get("errmsg")):
                        logger.error("[%s] session expired; pausing for 10 minutes", self.name)
                        await asyncio.sleep(600)
                        consecutive_failures = 0
                        continue
                    consecutive_failures += 1
                    logger.warning("[%s] getUpdates failed ret=%s errcode=%s errmsg=%s (%d/%d)", self.name, ret, errcode, response.get("errmsg", ""), consecutive_failures, MAX_CONSECUTIVE_FAILURES)
                    await asyncio.sleep(BACKOFF_DELAY_SECONDS if consecutive_failures >= MAX_CONSECUTIVE_FAILURES else RETRY_DELAY_SECONDS)
                    if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                        consecutive_failures = 0
                    continue
                consecutive_failures = 0
                new_sync_buf = str(response.get("get_updates_buf") or "")
                if new_sync_buf:
                    sync_buf = new_sync_buf
                    self._save_sync_buf(sync_buf)
                for message in response.get("msgs") or []:
                    asyncio.create_task(self._process_message_safe(message))
            except asyncio.CancelledError:
                break
            except Exception as exc:
                consecutive_failures += 1
                logger.error("[%s] poll error (%d/%d): %s", self.name, consecutive_failures, MAX_CONSECUTIVE_FAILURES, exc)
                await asyncio.sleep(BACKOFF_DELAY_SECONDS if consecutive_failures >= MAX_CONSECUTIVE_FAILURES else RETRY_DELAY_SECONDS)
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    consecutive_failures = 0

    async def _process_message_safe(self, message: Dict[str, Any]) -> None:
        try:
            await self._process_message(message)
        except Exception as exc:
            logger.error("[%s] unhandled inbound error from=%s: %s", self.name, _safe_id(message.get("from_user_id")), exc, exc_info=True)

    async def _process_message(self, message: Dict[str, Any]) -> None:
        assert self._poll_session is not None
        sender_id = str(message.get("from_user_id") or "").strip()
        if not sender_id or sender_id == self._account_id:
            return
        message_id = str(message.get("message_id") or "").strip()
        if message_id and self._dedup.is_duplicate(message_id):
            return
        item_list = message.get("item_list") or []
        text = _extract_text(item_list)
        if text:
            content_key = f"content:{sender_id}:{hashlib.md5(text.encode()).hexdigest()}"
            if self._dedup.is_duplicate(content_key):
                return
        chat_type, effective_chat_id = _guess_chat_type(message, self._account_id)
        if chat_type == "group":
            if self._group_policy == "disabled":
                return
            if self._group_policy == "allowlist" and effective_chat_id not in self._group_allow_from:
                return
        elif not self._is_dm_allowed(sender_id):
            return
        context_token = str(message.get("context_token") or "").strip()
        if context_token:
            self._token_store.set(sender_id, context_token)
        asyncio.create_task(self._maybe_fetch_typing_ticket(sender_id, context_token or None))

        media_paths: List[str] = []
        media_types: List[str] = []
        for item in item_list:
            await self._collect_media(item, media_paths, media_types)

        if not text and not media_paths:
            return
        source = self.build_source(chat_id=effective_chat_id, chat_type=chat_type, user_id=sender_id, user_name=sender_id)
        event = MessageEvent(text=text, message_type=_message_type_from_media(media_types, text), source=source, raw_message=message, message_id=message_id or None, media_urls=media_paths, media_types=media_types, timestamp=datetime.now())
        if event.message_type == MessageType.TEXT:
            self._enqueue_text_event(event)
        else:
            await self.handle_message(event)

    def _is_dm_allowed(self, sender_id: str) -> bool:
        if self._dm_policy == "disabled":
            return False
        if self._dm_policy == "allowlist":
            return sender_id in self._allow_from
        return True

    @property
    def enforces_own_access_policy(self) -> bool:
        return True

    _SPLIT_THRESHOLD = 1800

    def _text_batch_key(self, event: MessageEvent) -> str:
        from gateway.session import build_session_key

        return build_session_key(
            event.source,
            group_sessions_per_user=self.config.extra.get("group_sessions_per_user", True),
            thread_sessions_per_user=self.config.extra.get("thread_sessions_per_user", False),
        )

    def _enqueue_text_event(self, event: MessageEvent) -> None:
        key = self._text_batch_key(event)
        existing = self._pending_text_batches.get(key)
        chunk_len = len(event.text or "")
        if existing is None:
            event._last_chunk_len = chunk_len  # type: ignore[attr-defined]
            self._pending_text_batches[key] = event
        else:
            if event.text:
                existing.text = f"{existing.text}\n{event.text}" if existing.text else event.text
            existing._last_chunk_len = chunk_len  # type: ignore[attr-defined]
        prior_task = self._pending_text_batch_tasks.get(key)
        if prior_task and not prior_task.done():
            prior_task.cancel()
        self._pending_text_batch_tasks[key] = asyncio.create_task(self._flush_text_batch(key))

    async def _flush_text_batch(self, key: str) -> None:
        current_task = asyncio.current_task()
        try:
            pending = self._pending_text_batches.get(key)
            last_len = getattr(pending, "_last_chunk_len", 0) if pending else 0
            delay = self._text_batch_split_delay_seconds if last_len >= self._SPLIT_THRESHOLD else self._text_batch_delay_seconds
            await asyncio.sleep(delay)
            if self._pending_text_batch_tasks.get(key) is not current_task:
                return
            event = self._pending_text_batches.pop(key, None)
            if event:
                await self.handle_message(event)
        finally:
            if self._pending_text_batch_tasks.get(key) is current_task:
                self._pending_text_batch_tasks.pop(key, None)

    async def _collect_media(self, item: Dict[str, Any], media_paths: List[str], media_types: List[str]) -> None:
        item_type = item.get("type")
        if item_type == ITEM_IMAGE:
            path = await self._download_image(item)
            if path:
                media_paths.append(path)
                media_types.append("image/*")
        elif item_type == ITEM_VIDEO:
            path = await self._download_video(item)
            if path:
                media_paths.append(path)
                media_types.append("video/*")
        elif item_type == ITEM_FILE:
            path, mime = await self._download_file(item)
            if path:
                media_paths.append(path)
                media_types.append(mime)
        elif item_type == ITEM_VOICE:
            path = await self._download_voice(item)
            if path:
                media_paths.append(path)
                media_types.append("audio/*")

    async def _download_image(self, item: Dict[str, Any]) -> Optional[str]:
        media = ((item.get("image_item") or {}).get("media") or {})
        try:
            data = await _download_and_decrypt_media(self._poll_session, cdn_base_url=self._cdn_base_url, encrypted_query_param=media.get("encrypt_query_param"), aes_key_b64=media.get("aes_key"), full_url=media.get("full_url"), timeout_seconds=60.0)
            return cache_image_from_bytes(data, ".jpg")
        except Exception as exc:
            logger.warning("[%s] image download failed: %s", self.name, exc)
            return None

    async def _download_video(self, item: Dict[str, Any]) -> Optional[str]:
        media = ((item.get("video_item") or {}).get("media") or {})
        try:
            data = await _download_and_decrypt_media(self._poll_session, cdn_base_url=self._cdn_base_url, encrypted_query_param=media.get("encrypt_query_param"), aes_key_b64=media.get("aes_key"), full_url=media.get("full_url"), timeout_seconds=120.0)
            return cache_document_from_bytes(data, "video.mp4")
        except Exception as exc:
            logger.warning("[%s] video download failed: %s", self.name, exc)
            return None

    async def _download_file(self, item: Dict[str, Any]) -> Tuple[Optional[str], str]:
        file_item = item.get("file_item") or {}
        media = file_item.get("media") or {}
        filename = str(file_item.get("file_name") or "document.bin")
        mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        try:
            data = await _download_and_decrypt_media(self._poll_session, cdn_base_url=self._cdn_base_url, encrypted_query_param=media.get("encrypt_query_param"), aes_key_b64=media.get("aes_key"), full_url=media.get("full_url"), timeout_seconds=60.0)
            return cache_document_from_bytes(data, filename), mime
        except Exception as exc:
            logger.warning("[%s] file download failed: %s", self.name, exc)
            return None, mime

    async def _download_voice(self, item: Dict[str, Any]) -> Optional[str]:
        voice_item = item.get("voice_item") or {}
        media = voice_item.get("media") or {}
        if voice_item.get("text"):
            return None
        try:
            data = await _download_and_decrypt_media(self._poll_session, cdn_base_url=self._cdn_base_url, encrypted_query_param=media.get("encrypt_query_param"), aes_key_b64=media.get("aes_key"), full_url=media.get("full_url"), timeout_seconds=60.0)
            return cache_audio_from_bytes(data, ".silk")
        except Exception as exc:
            logger.warning("[%s] voice download failed: %s", self.name, exc)
            return None

    async def _maybe_fetch_typing_ticket(self, user_id: str, context_token: Optional[str]) -> None:
        if not self._poll_session or not self._token or self._typing_cache.get(user_id):
            return
        try:
            response = await _get_config(self._poll_session, base_url=self._base_url, token=self._token, user_id=user_id, context_token=context_token)
            typing_ticket = str(response.get("typing_ticket") or "")
            if typing_ticket:
                self._typing_cache.set(user_id, typing_ticket)
        except Exception as exc:
            logger.debug("[%s] getConfig failed for %s: %s", self.name, _safe_id(user_id), exc)

    def _split_text(self, content: str) -> List[str]:
        return _split_text_for_weixin_delivery(content, self.MAX_MESSAGE_LENGTH, self._split_multiline_messages)

    async def _send_text_chunk(self, *, chat_id: str, chunk: str, context_token: Optional[str], client_id: str) -> None:
        async with self._send_text_gate:
            last_error: Optional[Exception] = None
            retried_without_token = False
            current_token = context_token
            for attempt in range(self._send_chunk_retries + 1):
                try:
                    resp = await _send_message(self._send_session, base_url=self._base_url, token=self._token, to=chat_id, text=chunk, context_token=current_token, client_id=client_id)
                    ret = resp.get("ret") if isinstance(resp, dict) else None
                    errcode = resp.get("errcode") if isinstance(resp, dict) else None
                    if (ret is not None and ret not in {0}) or (errcode is not None and errcode not in {0}):
                        is_session_expired = ret == SESSION_EXPIRED_ERRCODE or errcode == SESSION_EXPIRED_ERRCODE or _is_stale_session_ret(ret, errcode, resp.get("errmsg"))
                        if is_session_expired and not retried_without_token and current_token:
                            retried_without_token = True
                            current_token = None
                            self._token_store.delete(chat_id)
                            continue
                        if ret == RATE_LIMIT_ERRCODE or errcode == RATE_LIMIT_ERRCODE:
                            last_error = RuntimeError(f"iLink sendmessage rate limited: ret={ret} errcode={errcode} errmsg={resp.get('errmsg')}")
                            if attempt >= self._send_chunk_retries:
                                break
                            await asyncio.sleep(self._send_chunk_retry_delay_seconds * 3)
                            continue
                        raise RuntimeError(f"iLink sendmessage error: ret={ret} errcode={errcode} errmsg={resp.get('errmsg')}")
                    return
                except Exception as exc:
                    last_error = exc
                    if attempt >= self._send_chunk_retries:
                        break
                    await asyncio.sleep(self._send_chunk_retry_delay_seconds * (attempt + 1))
            if last_error is None:
                last_error = RuntimeError("unknown send failure")
            raise last_error

    async def send(self, chat_id: str, content: str, reply_to: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None) -> SendResult:
        del reply_to, metadata
        if not self._send_session or not self._token:
            return SendResult(success=False, error="Not connected")
        context_token = self._token_store.get(chat_id)
        last_message_id: Optional[str] = None
        media_files, cleaned_content = self.extract_media(content)
        media_files = self.filter_media_delivery_paths(media_files)
        _, image_cleaned = self.extract_images(cleaned_content)
        local_files, final_content = self.extract_local_files(image_cleaned)
        local_files = self.filter_local_delivery_paths(local_files)
        audio_exts = {".ogg", ".opus", ".mp3", ".wav", ".m4a", ".flac"}
        video_exts = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".3gp"}
        image_exts = {".jpg", ".jpeg", ".png", ".webp", ".gif"}

        async def _deliver_media(path: str, is_voice: bool = False) -> None:
            ext = Path(path).suffix.lower()
            if is_voice or ext in audio_exts:
                await self.send_voice(chat_id=chat_id, audio_path=path)
            elif ext in video_exts:
                await self.send_video(chat_id=chat_id, video_path=path)
            elif ext in image_exts:
                await self.send_image_file(chat_id=chat_id, image_path=path)
            else:
                await self.send_document(chat_id=chat_id, file_path=path)

        try:
            for media_path, is_voice in media_files:
                await _deliver_media(media_path, is_voice)
            for file_path in local_files:
                await _deliver_media(file_path, False)
            chunks = [c for c in self._split_text(self.format_message(final_content)) if c and c.strip()]
            for idx, chunk in enumerate(chunks):
                client_id = f"hermes-weixin-{uuid.uuid4().hex}"
                await self._send_text_chunk(chat_id=chat_id, chunk=chunk, context_token=context_token, client_id=client_id)
                last_message_id = client_id
                if idx < len(chunks) - 1 and self._send_chunk_delay_seconds > 0:
                    await asyncio.sleep(self._send_chunk_delay_seconds)
            return SendResult(success=True, message_id=last_message_id)
        except Exception as exc:
            logger.error("[%s] send failed to=%s: %s", self.name, _safe_id(chat_id), exc)
            return SendResult(success=False, error=str(exc))

    async def _ensure_typing_ticket(self, chat_id: str) -> Optional[str]:
        ticket = self._typing_cache.get(chat_id)
        if ticket:
            return ticket
        if not self._send_session or not self._token:
            return None
        context_token = self._token_store.get(chat_id)
        try:
            response = await _get_config(self._send_session, base_url=self._base_url, token=self._token, user_id=chat_id, context_token=context_token)
            typing_ticket = str(response.get("typing_ticket") or "")
            if typing_ticket:
                self._typing_cache.set(chat_id, typing_ticket)
                return typing_ticket
        except Exception as exc:
            logger.debug("[%s] typing ticket refresh failed for %s: %s", self.name, _safe_id(chat_id), exc)
        return None

    async def send_typing(self, chat_id: str, metadata: Optional[Dict[str, Any]] = None) -> None:
        del metadata
        if not self._send_session or not self._token:
            return
        typing_ticket = await self._ensure_typing_ticket(chat_id)
        if not typing_ticket:
            return
        try:
            await _send_typing(self._send_session, base_url=self._base_url, token=self._token, to_user_id=chat_id, typing_ticket=typing_ticket, status=TYPING_START)
        except Exception as exc:
            logger.debug("[%s] typing start failed for %s: %s", self.name, _safe_id(chat_id), exc)

    async def stop_typing(self, chat_id: str) -> None:
        if not self._send_session or not self._token:
            return
        typing_ticket = await self._ensure_typing_ticket(chat_id)
        if not typing_ticket:
            return
        try:
            await _send_typing(self._send_session, base_url=self._base_url, token=self._token, to_user_id=chat_id, typing_ticket=typing_ticket, status=TYPING_STOP)
        except Exception as exc:
            logger.debug("[%s] typing stop failed for %s: %s", self.name, _safe_id(chat_id), exc)

    async def send_image(self, chat_id: str, image_url: str, caption: str = "", reply_to: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None) -> SendResult:
        del reply_to, metadata
        if image_url.startswith(("http://", "https://")):
            file_path = await self._download_remote_media(image_url)
            cleanup = True
        else:
            file_path = image_url.replace("file://", "")
            if not os.path.isabs(file_path):
                file_path = os.path.abspath(file_path)
            cleanup = False
        try:
            return await self.send_document(chat_id=chat_id, file_path=file_path, caption=caption)
        finally:
            if cleanup and file_path and os.path.exists(file_path):
                try:
                    os.unlink(file_path)
                except OSError:
                    pass

    async def send_image_file(self, chat_id: str, image_path: str, caption: Optional[str] = None, reply_to: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None, **kwargs) -> SendResult:
        del reply_to, metadata, kwargs
        return await self.send_document(chat_id=chat_id, file_path=image_path, caption=caption)

    async def send_document(self, chat_id: str, file_path: str, caption: Optional[str] = None, file_name: Optional[str] = None, reply_to: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None, **kwargs) -> SendResult:
        del file_name, reply_to, metadata, kwargs
        if not self._send_session or not self._token:
            return SendResult(success=False, error="Not connected")
        try:
            message_id = await self._send_file(chat_id, file_path, caption or "")
            return SendResult(success=True, message_id=message_id)
        except Exception as exc:
            logger.error("[%s] send_document failed to=%s: %s", self.name, _safe_id(chat_id), exc)
            return SendResult(success=False, error=str(exc))

    async def send_video(self, chat_id: str, video_path: str, caption: Optional[str] = None, reply_to: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None) -> SendResult:
        del reply_to, metadata
        if not self._send_session or not self._token:
            return SendResult(success=False, error="Not connected")
        try:
            message_id = await self._send_file(chat_id, video_path, caption or "")
            return SendResult(success=True, message_id=message_id)
        except Exception as exc:
            logger.error("[%s] send_video failed to=%s: %s", self.name, _safe_id(chat_id), exc)
            return SendResult(success=False, error=str(exc))

    async def send_voice(self, chat_id: str, audio_path: str, caption: Optional[str] = None, reply_to: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None) -> SendResult:
        del reply_to, metadata
        if not self._send_session or not self._token:
            return SendResult(success=False, error="Not connected")
        try:
            message_id = await self._send_file(chat_id, audio_path, caption or "[voice message as attachment]", force_file_attachment=True)
            return SendResult(success=True, message_id=message_id)
        except Exception as exc:
            logger.error("[%s] send_voice failed to=%s: %s", self.name, _safe_id(chat_id), exc)
            return SendResult(success=False, error=str(exc))

    async def _download_remote_media(self, url: str) -> str:
        from tools.url_safety import is_safe_url

        if not is_safe_url(url):
            raise ValueError(f"Blocked unsafe URL (SSRF protection): {url}")
        assert self._send_session is not None
        async def _do_fetch() -> bytes:
            async with self._send_session.get(url) as response:
                response.raise_for_status()
                return await response.read()
        data = await asyncio.wait_for(_do_fetch(), timeout=30)
        suffix = Path(url.split("?", 1)[0]).suffix or ".bin"
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as handle:
            handle.write(data)
            return handle.name

    async def _send_file(self, chat_id: str, path: str, caption: str, force_file_attachment: bool = False) -> str:
        assert self._send_session is not None and self._token is not None
        plaintext = Path(path).read_bytes()
        media_type, item_builder = self._outbound_media_builder(path, force_file_attachment=force_file_attachment)
        filekey = secrets.token_hex(16)
        aes_key = secrets.token_bytes(16)
        rawsize = len(plaintext)
        rawfilemd5 = hashlib.md5(plaintext).hexdigest()
        upload_response = await _get_upload_url(self._send_session, base_url=self._base_url, token=self._token, to_user_id=chat_id, media_type=media_type, filekey=filekey, rawsize=rawsize, rawfilemd5=rawfilemd5, filesize=_aes_padded_size(rawsize), aeskey_hex=aes_key.hex())
        upload_param = str(upload_response.get("upload_param") or "")
        upload_full_url = str(upload_response.get("upload_full_url") or "")
        ciphertext = _aes128_ecb_encrypt(plaintext, aes_key)
        if upload_full_url:
            upload_url = upload_full_url
        elif upload_param:
            upload_url = _cdn_upload_url(self._cdn_base_url, upload_param, filekey)
        else:
            raise RuntimeError(f"getUploadUrl returned neither upload_param nor upload_full_url: {upload_response}")
        encrypted_query_param = await _upload_ciphertext(self._send_session, ciphertext=ciphertext, upload_url=upload_url)
        context_token = self._token_store.get(chat_id)
        aes_key_for_api = base64.b64encode(aes_key.hex().encode("ascii")).decode("ascii")
        item_kwargs = {"encrypt_query_param": encrypted_query_param, "aes_key_for_api": aes_key_for_api, "ciphertext_size": len(ciphertext), "plaintext_size": rawsize, "filename": Path(path).name, "rawfilemd5": rawfilemd5}
        if media_type == MEDIA_VOICE and path.endswith(".silk"):
            item_kwargs["encode_type"] = 6
            item_kwargs["sample_rate"] = 24000
            item_kwargs["bits_per_sample"] = 16
        media_item = item_builder(**item_kwargs)
        last_message_id = None
        if caption:
            last_message_id = f"hermes-weixin-{uuid.uuid4().hex}"
            await _send_message(self._send_session, base_url=self._base_url, token=self._token, to=chat_id, text=self.format_message(caption), context_token=context_token, client_id=last_message_id)
        last_message_id = f"hermes-weixin-{uuid.uuid4().hex}"
        await _api_post(self._send_session, base_url=self._base_url, endpoint="ilink/bot/sendmessage", payload={"msg": {"from_user_id": "", "to_user_id": chat_id, "client_id": last_message_id, "message_type": MSG_TYPE_BOT, "message_state": MSG_STATE_FINISH, "item_list": [media_item], **({"context_token": context_token} if context_token else {})}}, token=self._token, timeout_ms=API_TIMEOUT_MS)
        return last_message_id

    def _outbound_media_builder(self, path: str, force_file_attachment: bool = False):
        mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
        if mime.startswith("image/"):
            return MEDIA_IMAGE, lambda **kw: {"type": ITEM_IMAGE, "image_item": {"media": {"encrypt_query_param": kw["encrypt_query_param"], "aes_key": kw["aes_key_for_api"], "encrypt_type": 1}, "mid_size": kw["ciphertext_size"]}}
        if mime.startswith("video/"):
            return MEDIA_VIDEO, lambda **kw: {"type": ITEM_VIDEO, "video_item": {"media": {"encrypt_query_param": kw["encrypt_query_param"], "aes_key": kw["aes_key_for_api"], "encrypt_type": 1}, "video_size": kw["ciphertext_size"], "play_length": kw.get("play_length", 0), "video_md5": kw.get("rawfilemd5", "")}}
        if path.endswith(".silk") and not force_file_attachment:
            return MEDIA_VOICE, lambda **kw: {"type": ITEM_VOICE, "voice_item": {"media": {"encrypt_query_param": kw["encrypt_query_param"], "aes_key": kw["aes_key_for_api"], "encrypt_type": 1}, "encode_type": kw.get("encode_type"), "bits_per_sample": kw.get("bits_per_sample"), "sample_rate": kw.get("sample_rate"), "playtime": kw.get("playtime", 0)}}
        return MEDIA_FILE, lambda **kw: {"type": ITEM_FILE, "file_item": {"media": {"encrypt_query_param": kw["encrypt_query_param"], "aes_key": kw["aes_key_for_api"], "encrypt_type": 1}, "file_name": kw["filename"], "len": str(kw["plaintext_size"])}}

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        chat_type = "group" if chat_id.endswith("@chatroom") else "dm"
        return {"name": chat_id, "type": chat_type, "chat_id": chat_id}

    def format_message(self, content: Optional[str]) -> str:
        if content is None:
            return ""
        return _wrap_copy_friendly_lines_for_weixin(_normalize_markdown_blocks(content))


def _validate_config(cfg: PlatformConfig) -> bool:
    extra = cfg.extra or {}
    return bool((cfg.token or extra.get("token")) and extra.get("account_id"))


def _is_connected(cfg: PlatformConfig) -> bool:
    return bool(cfg.enabled and _validate_config(cfg))
def _make_instance_setup_fn(instance_name: str):
    """Return a setup_fn for a specific weixin instance."""

    def _setup_instance():
        from hermes_cli.setup import (
            print_header,
            print_info,
        )

        print_header(f"Weixin Multi-iLink ({instance_name})")
        print_info(f"Instance: {instance_name}")
        print_info("To add a NEW instance, run:")
        print_info(f"  python ~/.hermes/plugins/weixin-multi-ilink/setup_instance.py add <name>")
        print()
        print_info("To reconfigure an existing instance, edit ~/.hermes/config.yaml directly.")
        print_info("Then run: hermes gateway restart")

    return _setup_instance


def _load_plugin_instances() -> Dict[str, Dict[str, Any]]:
    config_path = Path(get_hermes_home()) / "config.yaml"
    if not config_path.exists():
        return {}
    try:
        import yaml

        data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        logger.warning("weixin-multi: failed to read %s: %s", config_path, exc)
        return {}
    platforms = data.get("platforms") or {}
    if not isinstance(platforms, dict):
        return {}
    return {name: cfg for name, cfg in platforms.items() if isinstance(name, str) and name.startswith("weixin-") and isinstance(cfg, dict)}


def register(ctx) -> None:
    instances = _load_plugin_instances()
    for platform_name, raw_cfg in sorted(instances.items()):
        extra = dict(raw_cfg.get("extra") or {})
        extra["platform_name"] = platform_name
        instance_suffix = platform_name[len("weixin-") :] if platform_name.startswith("weixin-") else platform_name
        env_prefix = f"WEIXIN_{instance_suffix.upper().replace('-', '_')}"

        def _factory(cfg: PlatformConfig, platform_name: str = platform_name) -> WeixinMultiAdapter:
            cfg.extra = dict(cfg.extra or {})
            cfg.extra.setdefault("platform_name", platform_name)
            return WeixinMultiAdapter(cfg)

        ctx.register_platform(
            name=platform_name,
            label=f"Weixin ({instance_suffix})",
            adapter_factory=_factory,
            check_fn=check_weixin_requirements,
            setup_fn=_make_instance_setup_fn(platform_name),
            validate_config=_validate_config,
            is_connected=_is_connected,
            required_env=[],
            install_hint="pip install aiohttp cryptography certifi",
            allowed_users_env=f"{env_prefix}_ALLOWED_USERS",
            allow_all_env=f"{env_prefix}_ALLOW_ALL_USERS",
            cron_deliver_env_var=f"{env_prefix}_HOME_CHANNEL",
            max_message_length=WeixinMultiAdapter.MAX_MESSAGE_LENGTH,
            emoji="💬",
            allow_update_command=True,
            pii_safe=False,
            platform_hint=(
                "You are on Weixin via the iLink bot API. Markdown code fences render, but long responses should stay compact. "
                "Message size limit is about 2000 characters per chunk. Media attachments are supported via local files."
            ),
        )


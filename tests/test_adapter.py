import sys
from pathlib import Path
import types
import importlib.util
from unittest.mock import MagicMock, AsyncMock, patch
import pytest
import asyncio

# Add project root to sys.path
root = Path(__file__).parent.parent
sys.path.insert(0, str(root))

# Mock gateway and base modules
mock_gateway_config = types.ModuleType("gateway.config")
mock_gateway_config.Platform = MagicMock(return_value="weixin-multi")
class PlatformConfig:
    def __init__(self, token=None, extra=None, enabled=True):
        self.token = token
        self.extra = extra or {}
        self.enabled = enabled
mock_gateway_config.PlatformConfig = PlatformConfig
sys.modules["gateway.config"] = mock_gateway_config

mock_gateway_platforms_base = types.ModuleType("gateway.platforms.base")
class BasePlatformAdapter:
    def __init__(self, config, platform):
        self.config = config
        self.platform = platform
        self.name = "weixin-test"
mock_gateway_platforms_base.BasePlatformAdapter = BasePlatformAdapter
mock_gateway_platforms_base.MessageEvent = MagicMock()
mock_gateway_platforms_base.MessageType = MagicMock()

class SendResult:
    def __init__(self, success, message_id=None, error=None):
        self.success = success
        self.message_id = message_id
        self.error = error
mock_gateway_platforms_base.SendResult = SendResult
mock_gateway_platforms_base.cache_audio_from_bytes = MagicMock(return_value="/tmp/audio.silk")
mock_gateway_platforms_base.cache_document_from_bytes = MagicMock(return_value="/tmp/doc.bin")
mock_gateway_platforms_base.cache_image_from_bytes = MagicMock(return_value="/tmp/img.png")
sys.modules["gateway.platforms.base"] = mock_gateway_platforms_base

mock_gateway_platforms_helpers = types.ModuleType("gateway.platforms.helpers")
mock_gateway_platforms_helpers.MessageDeduplicator = MagicMock()
sys.modules["gateway.platforms.helpers"] = mock_gateway_platforms_helpers

mock_hermes_cli_config = types.ModuleType("hermes_cli.config")
mock_hermes_cli_config.get_hermes_home = MagicMock(return_value="/tmp/.hermes")
sys.modules["hermes_cli.config"] = mock_hermes_cli_config

mock_utils = types.ModuleType("utils")
mock_utils.atomic_json_write = MagicMock()
sys.modules["utils"] = mock_utils

# Create mock plugin package
mock_package = types.ModuleType("mock_plugin")
mock_package.__path__ = [str(root)]
sys.modules["mock_plugin"] = mock_package

# Load relative modules
spec_crypto = importlib.util.spec_from_file_location("mock_plugin.crypto", str(root / "crypto.py"))
crypto = importlib.util.module_from_spec(spec_crypto)
sys.modules["mock_plugin.crypto"] = crypto
spec_crypto.loader.exec_module(crypto)

spec_api = importlib.util.spec_from_file_location("mock_plugin.ilink_api", str(root / "ilink_api.py"))
ilink_api = importlib.util.module_from_spec(spec_api)
sys.modules["mock_plugin.ilink_api"] = ilink_api
spec_api.loader.exec_module(ilink_api)

spec_token = importlib.util.spec_from_file_location("mock_plugin.token_store", str(root / "token_store.py"))
token_store = importlib.util.module_from_spec(spec_token)
sys.modules["mock_plugin.token_store"] = token_store
spec_token.loader.exec_module(token_store)

spec_adapter = importlib.util.spec_from_file_location("mock_plugin.adapter", str(root / "adapter.py"))
adapter = importlib.util.module_from_spec(spec_adapter)
sys.modules["mock_plugin.adapter"] = adapter
spec_adapter.loader.exec_module(adapter)

WeixinMultiAdapter = adapter.WeixinMultiAdapter


@pytest.mark.asyncio
async def test_send_text_chunk_session_expired_retry():
    """stale session 时清除 context_token 并重试"""
    config = PlatformConfig(token="bot_token", extra={"platform_name": "weixin-test", "account_id": "test_account"})
    instance = WeixinMultiAdapter(config)
    instance._send_session = MagicMock()
    instance._token_store = MagicMock()
    instance._token_store.get.return_value = "stale_token"

    mock_send = AsyncMock()
    mock_send.side_effect = [
        {"ret": -2, "errmsg": "unknown error"},
        {"ret": 0},
    ]

    with patch("mock_plugin.adapter._send_message", mock_send):
        await instance._send_text_chunk(chat_id="user123", chunk="hello", context_token="stale_token", client_id="msg123")

        instance._token_store.delete.assert_called_once_with("user123")
        assert mock_send.call_count == 2
        mock_send.assert_any_call(instance._send_session, base_url=instance._base_url, token=instance._token, to="user123", text="hello", context_token="stale_token", client_id="msg123")
        mock_send.assert_any_call(instance._send_session, base_url=instance._base_url, token=instance._token, to="user123", text="hello", context_token=None, client_id="msg123")


@pytest.mark.asyncio
async def test_send_text_chunk_session_expired_retry_without_context_token():
    """context_token 为 None 时遇到 stale session 也应重试（不调用 delete）"""
    config = PlatformConfig(token="bot_token", extra={"platform_name": "weixin-test", "account_id": "test_account"})
    instance = WeixinMultiAdapter(config)
    instance._send_session = MagicMock()
    instance._token_store = MagicMock()

    mock_send = AsyncMock()
    mock_send.side_effect = [
        {"ret": -2, "errmsg": None},  # stale session，context_token 本身就为 None
        {"ret": 0},
    ]

    with patch("mock_plugin.adapter._send_message", mock_send):
        await instance._send_text_chunk(chat_id="user123", chunk="hello", context_token=None, client_id="msg123")

        # context_token 本来就是 None，不需要调用 delete
        instance._token_store.delete.assert_not_called()
        # 但仍然应该重试一次
        assert mock_send.call_count == 2


@pytest.mark.asyncio
async def test_send_text_chunk_rate_limited_retry():
    """真正的限流不应清除 token"""
    config = PlatformConfig(token="bot_token", extra={"platform_name": "weixin-test", "account_id": "test_account"})
    instance = WeixinMultiAdapter(config)
    instance._send_session = MagicMock()
    instance._token_store = MagicMock()
    instance._send_chunk_retries = 1
    instance._send_chunk_retry_delay_seconds = 0.01

    mock_send = AsyncMock()
    mock_send.side_effect = [
        {"ret": -2, "errmsg": "freq limit"},
        {"ret": 0},
    ]

    with patch("mock_plugin.adapter._send_message", mock_send):
        await instance._send_text_chunk(chat_id="user123", chunk="hello", context_token="token123", client_id="msg123")

        instance._token_store.delete.assert_not_called()
        assert mock_send.call_count == 2


@pytest.mark.asyncio
async def test_send_file_caption_stale_session_retry():
    """发送文件的 caption 遇到 stale session 时清 token 重试"""
    config = PlatformConfig(token="bot_token", extra={"platform_name": "weixin-test", "account_id": "test_account"})
    instance = WeixinMultiAdapter(config)
    instance._send_session = MagicMock()
    instance._token_store = MagicMock()
    instance._token_store.get.return_value = "stale_token"

    mock_get_upload = AsyncMock(return_value={"upload_full_url": "http://cdn.com/upload"})
    mock_upload = AsyncMock(return_value="cdn_query_param")

    mock_send_msg = AsyncMock()
    mock_send_msg.side_effect = [
        {"ret": -2, "errmsg": None},  # stale session
        {"ret": 0},
    ]

    mock_api_post = AsyncMock(return_value={"ret": 0})

    temp_file = Path("/tmp/test_file.png")
    temp_file.write_bytes(b"dummy content")

    try:
        with patch("mock_plugin.adapter._get_upload_url", mock_get_upload), \
             patch("mock_plugin.adapter._upload_ciphertext", mock_upload), \
             patch("mock_plugin.adapter._send_message", mock_send_msg), \
             patch("mock_plugin.adapter._api_post", mock_api_post):

            await instance._send_file(chat_id="user123", path=str(temp_file), caption="hello", force_file_attachment=False)

            instance._token_store.delete.assert_called_once_with("user123")
            assert mock_send_msg.call_count == 2
            assert mock_api_post.call_count == 1
            # 两次 caption 调用应使用同一个 client_id（避免重复消息）
            first_client_id = mock_send_msg.call_args_list[0].kwargs.get("client_id") or mock_send_msg.call_args_list[0][1].get("client_id")
            second_client_id = mock_send_msg.call_args_list[1].kwargs.get("client_id") or mock_send_msg.call_args_list[1][1].get("client_id")
            assert first_client_id == second_client_id
    finally:
        if temp_file.exists():
            temp_file.unlink()


@pytest.mark.asyncio
async def test_send_file_media_stale_session_retry():
    """发送文件的 media 遇到 stale session 时清 token 重试，caption 不重发"""
    config = PlatformConfig(token="bot_token", extra={"platform_name": "weixin-test", "account_id": "test_account"})
    instance = WeixinMultiAdapter(config)
    instance._send_session = MagicMock()
    instance._token_store = MagicMock()
    instance._token_store.get.return_value = "stale_token"

    mock_get_upload = AsyncMock(return_value={"upload_full_url": "http://cdn.com/upload"})
    mock_upload = AsyncMock(return_value="cdn_query_param")

    mock_send_msg = AsyncMock(return_value={"ret": 0})

    mock_api_post = AsyncMock()
    mock_api_post.side_effect = [
        {"ret": -2, "errmsg": "unknown error"},
        {"ret": 0},
    ]

    temp_file = Path("/tmp/test_file.png")
    temp_file.write_bytes(b"dummy content")

    try:
        with patch("mock_plugin.adapter._get_upload_url", mock_get_upload), \
             patch("mock_plugin.adapter._upload_ciphertext", mock_upload), \
             patch("mock_plugin.adapter._send_message", mock_send_msg), \
             patch("mock_plugin.adapter._api_post", mock_api_post):

            await instance._send_file(chat_id="user123", path=str(temp_file), caption="hello", force_file_attachment=False)

            instance._token_store.delete.assert_called_once_with("user123")
            assert mock_api_post.call_count == 2
            # caption 不应重发
            assert mock_send_msg.call_count == 1
    finally:
        if temp_file.exists():
            temp_file.unlink()


@pytest.mark.asyncio
async def test_send_file_no_context_token_stale_session_retry():
    """context_token 为 None 时遇到 stale session 也应重试"""
    config = PlatformConfig(token="bot_token", extra={"platform_name": "weixin-test", "account_id": "test_account"})
    instance = WeixinMultiAdapter(config)
    instance._send_session = MagicMock()
    instance._token_store = MagicMock()
    instance._token_store.get.return_value = None  # 无 context_token

    mock_get_upload = AsyncMock(return_value={"upload_full_url": "http://cdn.com/upload"})
    mock_upload = AsyncMock(return_value="cdn_query_param")

    mock_api_post = AsyncMock()
    mock_api_post.side_effect = [
        {"ret": -2, "errmsg": None},  # stale session
        {"ret": 0},
    ]

    temp_file = Path("/tmp/test_file_notoken.png")
    temp_file.write_bytes(b"dummy content")

    try:
        with patch("mock_plugin.adapter._get_upload_url", mock_get_upload), \
             patch("mock_plugin.adapter._upload_ciphertext", mock_upload), \
             patch("mock_plugin.adapter._api_post", mock_api_post):

            await instance._send_file(chat_id="user123", path=str(temp_file), caption="", force_file_attachment=False)

            # context_token 本来就是 None，不需要调用 delete
            instance._token_store.delete.assert_not_called()
            # 但仍然应该重试
            assert mock_api_post.call_count == 2
    finally:
        if temp_file.exists():
            temp_file.unlink()

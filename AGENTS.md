# weixin-multi-ilink Plugin Development Plan

## Goal
Create a Hermes Agent gateway plugin that supports **multiple iLink WeChat instances** simultaneously. Each instance is an independent WeChat account connected via the iLink Bot API. Adding a new instance should only require adding a config.yaml entry — no code changes, no new directories.

## Architecture: Option A (Dynamic Multi-Platform Registration)

One plugin directory registers **multiple platform names** dynamically. Each `weixin-*` entry in config.yaml becomes an independent platform with its own adapter instance.

### Why Option A
- **Isolation**: One instance crashing doesn't affect others
- **Gateway-native routing**: Each instance gets its own chat_id namespace, auth config, cron delivery
- **Platform.missing() support**: Hermes auto-creates pseudo-members for registered plugin platforms (confirmed in gateway/config.py:167-209)
- **Simple adding**: Just add a `weixin-<name>` block to config.yaml

## Directory Structure

```
~/.hermes/plugins/weixin-multi-ilink/
├── AGENTS.md          # This file (development plan)
├── plugin.yaml        # Plugin metadata
├── __init__.py        # Entry point: register(ctx)
├── adapter.py         # WeixinMultiAdapter class + helpers
├── ilink_api.py       # iLink API client (extracted from weixin.py)
├── crypto.py          # AES-128-ECB encryption for CDN
├── token_store.py     # Context token persistence
└── .gitignore
```

## config.yaml Format

```yaml
platforms:
  weixin-main:
    enabled: true
    extra:
      account_id: "c40b597f@im.bot"
      token: "xxx"                    # or from env WEIXIN_MAIN_TOKEN
      base_url: "https://ilinkai.weixin.qq.com"
      cdn_base_url: "https://novac2c.cdn.weixin.qq.com/c2c"
      dm_policy: "pairing"            # open, allowlist, disabled, pairing
      group_policy: "disabled"
      allowed_users: []               # for allowlist mode
      home_channel: "user_id@im.wechat"

  weixin-work:
    enabled: true
    extra:
      account_id: "abc123@im.bot"
      token: "yyy"
      dm_policy: "open"
```

**Adding a new instance**: Just add a new `weixin-<name>` block. No code changes needed.

**Env var override**: Each instance can use `WEIXIN_<NAME>_TOKEN`, `WEIXIN_<NAME>_ACCOUNT_ID` etc. The naming convention is `WEIXIN_` + upper(instance name) + `_TOKEN`.

## Key Technical Details

### 1. Dynamic Platform Registration

In `__init__.py` → `register(ctx)`:
- Read `~/.hermes/config.yaml`
- Find all `weixin-*` keys under `platforms:`
- For each, call `ctx.register_platform(name=..., adapter_factory=...)`
- Gateway handles the rest (adapter creation, connection, message routing)

### 2. Core Logic to Fork from gateway/platforms/weixin.py

The following modules should be extracted into separate files for clarity:

**ilink_api.py** — Low-level iLink HTTP calls:
- `_api_post()` — generic POST to iLink
- `_get_updates()` — long-poll for messages
- `_send_message()` — send text
- `_send_typing()` — typing indicator
- `_get_config()` — get typing ticket
- `_get_upload_url()` / `_upload_ciphertext()` — CDN upload
- Constants: endpoints, error codes, timeouts

**crypto.py** — AES encryption:
- `_aes128_ecb_encrypt()` / `_aes128_ecb_decrypt()`
- `_pkcs7_pad()`
- CDN URL construction

**token_store.py** — Context token management:
- `ContextTokenStore` class (disk-backed, per account+peer)
- Save/load from `~/.hermes/weixin-multi/<instance_id>.context-tokens.json`

**adapter.py** — Main adapter class:
- `WeixinMultiAdapter(BasePlatformAdapter)` — one instance per platform
- `connect()` / `disconnect()` / `_poll_loop()`
- `_process_message()` — inbound handling
- `send()` / `send_image()` / `send_document()` / `send_video()` / `send_voice()`
- `_is_dm_allowed()` — access control
- Text batching / deduplication

### 3. Differences from Built-in weixin.py

| Aspect | Built-in weixin.py | Multi-ilink Plugin |
|--------|-------------------|-------------------|
| Platform name | `Platform.WEIXIN` (fixed) | Dynamic: `weixin-main`, `weixin-work`, etc. |
| Instance identity | Single `self._account_id` | Per-instance `account_id` from config extra |
| Config source | `.env` variables | `config.yaml` extra dict (with env fallback) |
| Token store path | `~/.hermes/weixin/accounts/` | `~/.hermes/weixin-multi/<instance>/` |
| Sync buf path | `~/.hermes/weixin/` | `~/.hermes/weixin-multi/<instance>/` |
| Group policy warning | Checks for iLink bot identity | Same check, per instance |

### 4. Lifecycle Management

- Each adapter runs its own `_poll_loop()` task
- Independent reconnection with exponential backoff
- Session expiry (errcode=-14): pause 10 minutes, then retry
- Gateway's connect/disconnect lifecycle applies per-platform

### 5. Authorization Integration

The plugin registers `allowed_users_env` and `allow_all_env` per platform:
```python
ctx.register_platform(
    name=instance_name,
    allowed_users_env=f"WEIXIN_{instance_name.upper()}_ALLOWED_USERS",
    allow_all_env=f"WEIXIN_{instance_name.upper()}_ALLOW_ALL_USERS",
    cron_deliver_env_var=f"WEIXIN_{instance_name.upper()}_HOME_CHANNEL",
    ...
)
```

Gateway's `_is_user_authorized()` checks these env vars automatically.

### 6. What NOT to Change

- Do NOT modify any file outside `~/.hermes/plugins/weixin-multi-ilink/`
- Do NOT touch `gateway/platforms/weixin.py` or any hermes-agent core file
- The built-in weixin platform should continue to work independently

## Implementation Order

1. Create `plugin.yaml` and `__init__.py` with dynamic registration
2. Extract `ilink_api.py` from weixin.py (API constants + HTTP functions)
3. Extract `crypto.py` (AES encryption helpers)
4. Extract `token_store.py` (context token persistence)
5. Create `adapter.py` (WeixinMultiAdapter class)
6. Test with a single instance first
7. Test with multiple instances

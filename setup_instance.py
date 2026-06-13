#!/usr/bin/env python3
"""
Interactive setup for weixin-multi-ilink instances.

Usage:
    python setup_instance.py add <instance_name>    # Add a new instance via QR login
    python setup_instance.py list                   # List configured instances
    python setup_instance.py remove <instance_name> # Remove an instance

Examples:
    python setup_instance.py add main
    python setup_instance.py add work
    python setup_instance.py list
"""

import asyncio
import sys
import os
from pathlib import Path

# Add parent to path for imports
sys.path.insert(0, str(Path(__file__).parent))


def _get_hermes_home() -> str:
    return os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))


def _get_config_path() -> Path:
    return Path(_get_hermes_home()) / "config.yaml"


def _load_config() -> dict:
    import yaml
    path = _get_config_path()
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _save_config(config: dict) -> None:
    import yaml
    path = _get_config_path()
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(config, f, default_flow_style=False, allow_unicode=True)
    print(f"  Config saved to {path}")


def _list_instances(config: dict) -> dict:
    platforms = config.get("platforms") or {}
    return {k: v for k, v in platforms.items() if isinstance(k, str) and k.startswith("weixin-")}


def cmd_list():
    config = _load_config()
    instances = _list_instances(config)
    if not instances:
        print("  No weixin-multi-ilink instances configured.")
        print("  Run: python setup_instance.py add <name>")
        return
    print(f"  Configured instances ({len(instances)}):")
    for name, cfg in sorted(instances.items()):
        extra = (cfg if isinstance(cfg, dict) else {}).get("extra") or {}
        account_id = extra.get("account_id", "?")
        dm_policy = extra.get("dm_policy", "open")
        enabled = (cfg if isinstance(cfg, dict) else {}).get("enabled", True)
        status = "enabled" if enabled else "disabled"
        print(f"    {name}: account={account_id} dm_policy={dm_policy} [{status}]")


def cmd_add(instance_name: str):
    if not instance_name.startswith("weixin-"):
        instance_name = f"weixin-{instance_name}"

    config = _load_config()
    platforms = config.setdefault("platforms", {})

    if instance_name in platforms:
        print(f"  Instance '{instance_name}' already exists.")
        resp = input("  Reconfigure? [y/N] ").strip().lower()
        if resp != "y":
            print("  Cancelled.")
            return

    print(f"\n  --- Adding instance: {instance_name} ---\n")
    print("  This will open iLink QR login.")
    print("  Use WeChat to scan and confirm the QR code.\n")

    # Import and run QR login
    try:
        from gateway.platforms.weixin import qr_login, check_weixin_requirements
    except ImportError:
        print("  Error: Cannot import weixin adapter.")
        print("  Make sure hermes-agent is installed.")
        sys.exit(1)

    if not check_weixin_requirements():
        print("  Missing dependencies: aiohttp and cryptography are required.")
        print("  Install: pip install aiohttp cryptography")
        sys.exit(1)

    try:
        credentials = asyncio.run(qr_login(_get_hermes_home()))
    except KeyboardInterrupt:
        print("\n  Cancelled.")
        return

    if not credentials:
        print("  QR login did not complete.")
        return

    account_id = credentials.get("account_id", "")
    token = credentials.get("token", "")
    base_url = credentials.get("base_url", "")
    user_id = credentials.get("user_id", "")

    print(f"\n  Connected! account_id={account_id}")

    # Configure DM policy
    print("\n  DM access policy:")
    print("    [0] Pairing (recommended) - users request access via code")
    print("    [1] Open - anyone can message")
    print("    [2] Allowlist - only listed user IDs")
    print("    [3] Disabled - no DMs")
    choice = input("  Choose [0-3, default=0]: ").strip() or "0"

    dm_policy_map = {"0": "pairing", "1": "open", "2": "allowlist", "3": "disabled"}
    dm_policy = dm_policy_map.get(choice, "pairing")

    allowed_users = []
    if dm_policy == "allowlist":
        users_input = input("  Allowed user IDs (comma-separated): ").strip()
        allowed_users = [u.strip() for u in users_input.split(",") if u.strip()]

    home_channel = input("  Home channel user ID (for cron/notifications, or empty): ").strip()

    # Build instance config
    instance_cfg = {
        "enabled": True,
        "extra": {
            "account_id": account_id,
            "token": token,
            "base_url": base_url or "https://ilinkai.weixin.qq.com",
            "cdn_base_url": "https://novac2c.cdn.weixin.qq.com/c2c",
            "dm_policy": dm_policy,
            "group_policy": "disabled",
        },
    }
    if allowed_users:
        instance_cfg["extra"]["allowed_users"] = allowed_users
    if home_channel:
        instance_cfg["extra"]["home_channel"] = home_channel

    platforms[instance_name] = instance_cfg
    _save_config(config)

    print(f"\n  Instance '{instance_name}' configured successfully!")
    print(f"  Restart gateway: hermes gateway restart")


def cmd_remove(instance_name: str):
    if not instance_name.startswith("weixin-"):
        instance_name = f"weixin-{instance_name}"

    config = _load_config()
    platforms = config.get("platforms") or {}

    if instance_name not in platforms:
        print(f"  Instance '{instance_name}' not found.")
        return

    resp = input(f"  Remove '{instance_name}'? [y/N] ").strip().lower()
    if resp != "y":
        print("  Cancelled.")
        return

    del platforms[instance_name]
    _save_config(config)
    print(f"  Instance '{instance_name}' removed.")
    print(f"  Restart gateway: hermes gateway restart")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return

    cmd = sys.argv[1]

    if cmd == "list":
        cmd_list()
    elif cmd == "add":
        if len(sys.argv) < 3:
            print("  Usage: python setup_instance.py add <instance_name>")
            print("  Example: python setup_instance.py add main")
            sys.exit(1)
        cmd_add(sys.argv[2])
    elif cmd == "remove":
        if len(sys.argv) < 3:
            print("  Usage: python setup_instance.py remove <instance_name>")
            sys.exit(1)
        cmd_remove(sys.argv[2])
    else:
        print(f"  Unknown command: {cmd}")
        print(__doc__)


if __name__ == "__main__":
    main()

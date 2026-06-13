from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Dict, Optional

from utils import atomic_json_write

logger = logging.getLogger(__name__)


class ContextTokenStore:
    """Disk-backed context_token cache keyed by peer for one plugin instance."""

    def __init__(self, root_dir: Path, instance_id: str):
        self._root = Path(root_dir)
        self._root.mkdir(parents=True, exist_ok=True)
        self._instance_id = instance_id
        self._cache: Dict[str, str] = {}

    @property
    def path(self) -> Path:
        return self._root / f"{self._instance_id}.context-tokens.json"

    def restore(self) -> None:
        path = self.path
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("weixin-multi: failed to restore context tokens for %s: %s", self._instance_id, exc)
            return
        restored = 0
        for user_id, token in data.items():
            if isinstance(user_id, str) and isinstance(token, str) and token:
                self._cache[user_id] = token
                restored += 1
        if restored:
            logger.info("weixin-multi: restored %d context token(s) for %s", restored, self._instance_id)

    def get(self, user_id: str) -> Optional[str]:
        return self._cache.get(user_id)

    def set(self, user_id: str, token: str) -> None:
        self._cache[user_id] = token
        self._persist()

    def delete(self, user_id: str) -> None:
        if self._cache.pop(user_id, None) is not None:
            self._persist()

    def _persist(self) -> None:
        try:
            atomic_json_write(self.path, dict(sorted(self._cache.items())))
            try:
                self.path.chmod(0o600)
            except OSError:
                pass
        except Exception as exc:
            logger.warning("weixin-multi: failed to persist context tokens for %s: %s", self._instance_id, exc)

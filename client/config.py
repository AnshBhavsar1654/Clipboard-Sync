"""Application configuration and persistent device identity."""

from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_WS_URL = "ws://127.0.0.1:8000/ws"
DEFAULT_RECONNECT_BASE_DELAY = 1.0
DEFAULT_RECONNECT_MAX_DELAY = 60.0
DEFAULT_CONFIG_DIR = Path.home() / ".clipboardsync"


@dataclass
class Config:
    """Runtime configuration for the clipboard sync client."""

    websocket_url: str = DEFAULT_WS_URL
    device_id: str = field(default="")
    reconnect_base_delay: float = DEFAULT_RECONNECT_BASE_DELAY
    reconnect_max_delay: float = DEFAULT_RECONNECT_MAX_DELAY
    config_dir: Path = field(default_factory=lambda: DEFAULT_CONFIG_DIR)

    def __post_init__(self) -> None:
        self.config_dir = Path(self.config_dir)
        self.config_dir.mkdir(parents=True, exist_ok=True)

        if not self.device_id:
            self.device_id = _load_or_create_device_id(self.config_dir)

        self.websocket_url = os.environ.get("CLIPBOARDSYNC_WS_URL", self.websocket_url)
        self.device_id = os.environ.get("CLIPBOARDSYNC_DEVICE_ID", self.device_id)

        log_level = os.environ.get("CLIPBOARDSYNC_LOG_LEVEL", "INFO").upper()
        logging.basicConfig(
            level=getattr(logging, log_level, logging.INFO),
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        )


def _load_or_create_device_id(config_dir: Path) -> str:
    """Load a persisted device ID or create and store a new one."""
    identity_file = config_dir / "device_id.json"

    if identity_file.exists():
        try:
            data = json.loads(identity_file.read_text(encoding="utf-8"))
            device_id = data.get("device_id", "")
            if device_id:
                logger.info("Loaded device ID from %s", identity_file)
                return device_id
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Could not read device ID file: %s", exc)

    device_id = str(uuid.uuid4())
    identity_file.write_text(
        json.dumps({"device_id": device_id}, indent=2),
        encoding="utf-8",
    )
    logger.info("Created new device ID and saved to %s", identity_file)
    return device_id


def load_config() -> Config:
    """Build configuration from defaults, persisted state, and environment."""
    return Config()

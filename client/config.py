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

AUTO_RECEIVE_MAX_BYTES = 50 * 1024 * 1024  # default auto-download cap (plan §2.7)


@dataclass
class ReceivePrefs:
    """How incoming shares land on this computer (plan §2.7). Stored in
    ``prefs.json`` next to the device id; missing keys fall back to defaults."""

    receive_folder: str = ""  # "" = ~/Downloads/ClipBoardSync
    auto_receive_max_bytes: int = AUTO_RECEIVE_MAX_BYTES
    put_received_on_clipboard: bool = True
    share_private_clips: bool = False  # plan §2.6: skip do-not-record clips
    extra_blocked_apps: list[str] | None = None


def load_prefs(config_dir: Path) -> ReceivePrefs:
    """Read receive prefs tolerantly (bad file -> defaults)."""
    try:
        data = json.loads((Path(config_dir) / "prefs.json").read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return ReceivePrefs()
        prefs = ReceivePrefs()
        if isinstance(data.get("receive_folder"), str):
            prefs.receive_folder = data["receive_folder"]
        try:
            prefs.auto_receive_max_bytes = max(
                0, int(data.get("auto_receive_max_bytes", prefs.auto_receive_max_bytes))
            )
        except (TypeError, ValueError):
            pass
        if isinstance(data.get("put_received_on_clipboard"), bool):
            prefs.put_received_on_clipboard = data["put_received_on_clipboard"]
        if isinstance(data.get("share_private_clips"), bool):
            prefs.share_private_clips = data["share_private_clips"]
        extra = data.get("extra_blocked_apps")
        if isinstance(extra, list):
            prefs.extra_blocked_apps = [str(a) for a in extra if isinstance(a, str)][:50]
        return prefs
    except (OSError, ValueError):
        return ReceivePrefs()


def save_prefs(config_dir: Path, prefs: ReceivePrefs) -> None:
    """Persist receive prefs (best effort)."""
    try:
        Path(config_dir).mkdir(parents=True, exist_ok=True)
        (Path(config_dir) / "prefs.json").write_text(
            json.dumps(
                {
                    "receive_folder": prefs.receive_folder,
                    "auto_receive_max_bytes": prefs.auto_receive_max_bytes,
                    "put_received_on_clipboard": prefs.put_received_on_clipboard,
                    "share_private_clips": prefs.share_private_clips,
                    "extra_blocked_apps": prefs.extra_blocked_apps or [],
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    except OSError as exc:
        logger.warning("Could not save prefs: %s", exc)


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

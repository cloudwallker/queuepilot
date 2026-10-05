"""Explicit environment configuration; never print credential values."""

import math
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


@dataclass(frozen=True, slots=True)
class Settings:
    db_path: Path = Path("data/queuepilot.db")
    provider: str = "mock"
    api_key: str | None = None
    worker_enabled: bool = True
    poll_seconds: float = 0.5
    lease_seconds: float = 60
    retry_base: float = 2
    ollama_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "qwen3:4b"
    host: str = "127.0.0.1"
    port: int = 8765

    def __post_init__(self):
        if self.provider not in ("mock", "ollama"):
            raise ValueError("QUEUEPILOT_PROVIDER 必须为 mock 或 ollama")
        if not math.isfinite(self.lease_seconds) or self.lease_seconds <= 30:
            raise ValueError("QUEUEPILOT_LEASE_SECONDS 必须大于 30")
        for name, value in (("POLL_SECONDS", self.poll_seconds), ("RETRY_BASE", self.retry_base)):
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"QUEUEPILOT_{name} 必须为正数")
        address = urlsplit(self.ollama_url)
        if (
            address.scheme not in ("http", "https")
            or not address.hostname
            or address.username
            or address.password
            or address.query
            or address.fragment
        ):
            raise ValueError("QUEUEPILOT_OLLAMA_URL 必须为不带凭据的 HTTP(S) 基础地址")
        if not self.ollama_model.strip() or not self.host.strip() or not 1 <= self.port <= 65535:
            raise ValueError("模型名、监听地址或端口配置无效")
        if self.api_key and (not self.api_key.isascii() or any(ord(c) < 33 for c in self.api_key)):
            raise ValueError("QUEUEPILOT_API_KEY 必须为不含空白的 ASCII 字符")

    @classmethod
    def from_env(cls):
        def number(name, default, convert=float):
            try:
                return convert(os.getenv("QUEUEPILOT_" + name, str(default)))
            except ValueError:
                raise ValueError(f"QUEUEPILOT_{name} 数字配置无效") from None

        enabled = os.getenv("QUEUEPILOT_WORKER_ENABLED", "true").lower()
        if enabled not in ("true", "false"):
            raise ValueError("QUEUEPILOT_WORKER_ENABLED 必须为 true 或 false")
        return cls(
            db_path=Path(os.getenv("QUEUEPILOT_DB", "data/queuepilot.db")),
            provider=os.getenv("QUEUEPILOT_PROVIDER", "mock"),
            api_key=os.getenv("QUEUEPILOT_API_KEY") or None,
            worker_enabled=enabled == "true",
            poll_seconds=number("POLL_SECONDS", 0.5),
            lease_seconds=number("LEASE_SECONDS", 60),
            retry_base=number("RETRY_BASE", 2),
            ollama_url=os.getenv("QUEUEPILOT_OLLAMA_URL", "http://127.0.0.1:11434"),
            ollama_model=os.getenv("QUEUEPILOT_OLLAMA_MODEL", "qwen3:4b"),
            host=os.getenv("QUEUEPILOT_HOST", "127.0.0.1"),
            port=number("PORT", 8765, int),
        )

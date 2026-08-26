from __future__ import annotations

import os
import tomllib
from pathlib import Path

# Default model exposed by the local Codex-backed configuration.
DEFAULT_MODEL = "gpt-5.6-luna"


def load_provider_config() -> dict[str, str | None]:
    """Load provider settings without ever returning secrets in API payloads."""
    base_url = os.getenv("AGORA_BASE_URL")
    token = os.getenv("AGORA_API_KEY")
    model = os.getenv("AGORA_MODEL", DEFAULT_MODEL)
    provider_name = os.getenv("AGORA_PROVIDER", "")
    config_path = Path(os.getenv("CODEX_HOME", Path.home() / ".codex")) / "config.toml"
    if config_path.is_file():
        try:
            config = tomllib.loads(config_path.read_text(encoding="utf-8"))
            provider_name = provider_name or str(config.get("model_provider", ""))
            model = model or str(config.get("model", DEFAULT_MODEL))
            provider = config.get("model_providers", {}).get(provider_name, {})
            base_url = base_url or provider.get("base_url")
            token = token or provider.get("experimental_bearer_token")
        except (OSError, tomllib.TOMLDecodeError, AttributeError):
            pass
    return {"base_url": base_url, "api_key": token, "model": model, "provider": provider_name or None}

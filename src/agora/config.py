from __future__ import annotations

import os
import tomllib
from pathlib import Path

# Default model exposed by the local Codex-backed configuration.
DEFAULT_MODEL = "gpt-5.6-luna"


def _load_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return values
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if not key:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


def load_provider_config() -> dict[str, str | None]:
    """Load provider settings without ever returning secrets in API payloads."""
    dotenv = _load_dotenv(Path(os.getenv("AGORA_ENV_FILE", ".env")))

    def env(name: str, *aliases: str) -> str | None:
        for key in (name, *aliases):
            value = os.getenv(key) or dotenv.get(key)
            if value and value.strip():
                return value.strip()
        return None

    base_url = env("AGORA_BASE_URL", "BASE_URL")
    token = env("AGORA_API_KEY", "LLM_APIKEY")
    model = env("AGORA_MODEL", "MODEL") or DEFAULT_MODEL
    provider_name = env("AGORA_PROVIDER") or ""
    api_type = env("AGORA_API_TYPE", "API_TYPE")
    # The short aliases are commonly used with OpenAI-compatible gateways.
    if not api_type and (env("BASE_URL") or env("LLM_APIKEY") or env("MODEL")):
        api_type = "openai-chat"
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
    return {"base_url": base_url, "api_key": token, "model": model, "provider": provider_name or None, "api_type": api_type or "responses"}

"""Phase 4 model serving layer (OpenAI-compatible providers, fake server, benchmark, provider comparison)."""
from .providers import (PRESETS, ChatResult, Provider, ProviderConfig, ProviderError, RateLimitError,  # noqa: F401
                        get_provider, make_provider, parse_json_lenient)

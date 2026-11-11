"""Phase 4 model serving layer (OpenAI-compatible providers, fake server, benchmark)."""
from .providers import (PRESETS, ChatResult, Provider, ProviderConfig, ProviderError, get_provider,  # noqa: F401
                        make_provider, parse_json_lenient)

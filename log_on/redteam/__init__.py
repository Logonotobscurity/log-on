"""Phase 4 red teaming: attack generator, poisoned documents, automated harness, optional Giskard scan."""
from .attacks import generate  # noqa: F401
from .harness import judge, run, summarise  # noqa: F401

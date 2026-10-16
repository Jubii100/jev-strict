"""jev-strict: an unofficial, strict client for TypeSafe's Jev evaluation API. A pinned model, a crash-safe usage meter, a hard
monthly cap, and judgements checked against option-order bias. Standard library only."""
from .client import ALIAS, KEY_NAME, Client, Error, Unavailable, torn_note
from .judge import DATA, NONE_KEY, Blocked, Judge

__version__ = "0.1.0"
__all__ = ["ALIAS", "Blocked", "Client", "DATA", "Error", "Judge", "KEY_NAME", "NONE_KEY", "Unavailable",
           "torn_note", "__version__"]

"""Local host adapters for external runtime/plugin event sources."""

from .registry import (
    CodexCursor,
    PairingChallenge,
    PairingError,
    PluginNotActiveError,
    PluginRegistrationError,
    PluginRegistry,
)
from .codex import CodexDiagnostic, CodexIncrementalAdapter, CodexParseError, CodexReadResult
from .dsh import DshAdapter
from .delivery import AdapterDeliveryError, ComponentEventDelivery
from .claude import ClaudeDiagnostic, ClaudeIncrementalAdapter, ClaudeParseError, ClaudeReadResult
from .claude_desktop import ClaudeDesktopAdapter, ClaudeDesktopParseError, ClaudeDesktopReadResult
from .workbuddy_desktop import WorkBuddyDesktopAdapter, WorkBuddyParseError, WorkBuddyReadResult
from .workbuddy_jsonl import WorkBuddyJsonlAdapter, WorkBuddyJsonlParseError, WorkBuddyJsonlReadResult

__all__ = [
    "DshAdapter",
    "CodexCursor",
    "CodexDiagnostic",
    "CodexIncrementalAdapter",
    "CodexParseError",
    "CodexReadResult",
    "AdapterDeliveryError",
    "ComponentEventDelivery",
    "ClaudeDiagnostic",
    "ClaudeIncrementalAdapter",
    "ClaudeParseError",
    "ClaudeReadResult",
    "ClaudeDesktopAdapter",
    "ClaudeDesktopParseError",
    "ClaudeDesktopReadResult",
    "WorkBuddyDesktopAdapter",
    "WorkBuddyParseError",
    "WorkBuddyReadResult",
    "WorkBuddyJsonlAdapter",
    "WorkBuddyJsonlParseError",
    "WorkBuddyJsonlReadResult",
    "PairingChallenge",
    "PairingError",
    "PluginNotActiveError",
    "PluginRegistrationError",
    "PluginRegistry",
]

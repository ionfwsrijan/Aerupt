"""
AERUPT — Autonomous Reactive Interruptible Agent.

A voice-native, full-duplex assistant core built for the
Samsung Prism "Interruptible Real-Time Agents" theme.

Architecture (mirrors the theme's dual-process model):
    Fast Path  -> low-latency reflexes: acknowledgments, progress narration,
                  speculation on partial input, interruption acks.
    Slow Path  -> chained tool execution, multimodal grounding, clarifications,
                  final responses with structured State Snapshots.
    Coordination Layer -> two async queues (events in / actions out),
                  epoch-based generation control, grace-period cancellation,
                  idempotency ledger, speculative result reuse.

Public entry: aerupt.agent.InterruptibleAgent
"""

__version__ = "1.1.0"

from .agent import InterruptibleAgent  # noqa: E402,F401

__all__ = ["InterruptibleAgent", "__version__"]
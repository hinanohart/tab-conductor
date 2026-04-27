"""tab-conductor: Stable multi-tab orchestrator skill for Claude Code.

Provides supervisor + worker subprocess coordination via JSON state file,
with cost cap, secret deny, stuck detection, and post-mortem persistence.
"""

__version__ = "0.1.1"

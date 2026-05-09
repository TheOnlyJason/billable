"""Project auto-discovery.

Two surfaces:

- ``scan`` — pure observation of what's on this machine: every Cursor
  workspace folder, every recurring window-title pattern from
  ActivityWatch over the last N days, recent example prompts. No
  network, no LLM.

- ``propose`` — feed the scan to an LLM and ask it to write a draft
  ``projects.yaml``. Output is validated as YAML before being shown
  to the user.

The CLI command ``billable discover`` wires these together so the user
never has to author rules from scratch.
"""

from billable.discovery.propose import propose_projects_yaml
from billable.discovery.scan import (
    BrowserPattern,
    CursorWorkspace,
    DiscoveryScan,
    scan_machine,
)

__all__ = [
    "BrowserPattern",
    "CursorWorkspace",
    "DiscoveryScan",
    "propose_projects_yaml",
    "scan_machine",
]

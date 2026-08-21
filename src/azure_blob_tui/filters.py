from __future__ import annotations

import re


AML_SNAPSHOT_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12}-[a-z0-9]{26}$"
)


def is_aml_snapshot(
    container: str,
    workspace_ids: set[str] | None = None,
) -> bool:
    if AML_SNAPSHOT_PATTERN.fullmatch(container):
        return True
    for workspace_id in workspace_ids or set():
        prefix = f"{workspace_id}-"
        suffix = container[len(prefix) :] if container.startswith(prefix) else ""
        if len(suffix) == 26 and suffix.isalnum():
            return True
    return False

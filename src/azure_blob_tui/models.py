from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Subscription:
    id: str
    name: str
    tenant_id: str
    is_default: bool


@dataclass(frozen=True)
class StorageAccount:
    name: str
    resource_group: str
    location: str
    resource_id: str
    endpoint: str
    hns_enabled: bool


@dataclass(frozen=True)
class ContainerItem:
    name: str
    is_deleted: bool = False
    version: str = ""
    deleted_time: str = ""
    remaining_retention_days: int | None = None
    properties: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ContainerUsage:
    completed: bool
    page_count: int
    blob_count: int
    size_bytes: int

    @property
    def status(self) -> str:
        if self.completed:
            return "done"
        if self.page_count:
            return "partial"
        return "pending"


@dataclass(frozen=True)
class FolderUsage:
    depth: int
    blob_count: int
    size_bytes: int


@dataclass(frozen=True)
class BlobItem:
    name: str
    is_prefix: bool
    size_bytes: int = 0
    last_modified: str = ""
    blob_type: str = ""
    access_tier: str = ""
    content_type: str = ""
    etag: str = ""
    properties: dict[str, str] = field(default_factory=dict)
    metadata: dict[str, str] = field(default_factory=dict)
    is_deleted: bool = False
    version_id: str = ""
    deletion_id: str = ""
    deleted_time: str = ""
    remaining_retention_days: int | None = None

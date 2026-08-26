from __future__ import annotations

import http.client
import time
import uuid
import xml.etree.ElementTree as ET
from email.utils import formatdate
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from .azure import AppError, StorageError, TokenProvider
from .models import BlobItem, ContainerItem, StorageAccount


API_VERSION = "2023-11-03"
RETRYABLE_STATUS_CODES = {408, 429, 500, 502, 503, 504}


def human_size(size_bytes: int) -> str:
    value = float(size_bytes)
    units = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.2f} {unit}"
        value /= 1024
    return f"{value:.2f} PiB"


def human_count(value: int) -> str:
    number = float(value)
    for suffix in ("", "K", "M", "B", "T"):
        if abs(number) < 1000 or suffix == "T":
            if not suffix:
                return f"{int(number)}"
            precision = 2 if abs(number) < 10 else 1
            return f"{number:.{precision}f}{suffix}"
        number /= 1000
    return f"{number:.1f}T"


def storage_error_details(body: bytes) -> tuple[str, str]:
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return "Unknown", body.decode("utf-8", errors="replace")[:500]
    return (
        root.findtext("Code", default="Unknown"),
        root.findtext(
            "Message",
            default="Azure Storage request failed.",
        )[:500],
    )


def next_marker(root: ET.Element) -> str:
    return root.findtext("./NextMarker", default="")


def _is_true(value: str | None) -> bool:
    return bool(value and value.casefold() == "true")


def _optional_int(value: str | None) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class BlobRestClient:
    def __init__(
        self,
        token_provider: TokenProvider,
        timeout_seconds: int = 60,
        max_attempts: int = 6,
    ) -> None:
        self.token_provider = token_provider
        self.timeout_seconds = timeout_seconds
        self.max_attempts = max_attempts

    def get_xml(
        self,
        account: StorageAccount,
        path: str,
        parameters: dict[str, str | int],
    ) -> ET.Element:
        url = f"{account.endpoint.rstrip('/')}/{quote(path, safe='/')}"
        url = f"{url}?{urlencode(parameters)}"
        last_error = ""
        for attempt in range(self.max_attempts):
            try:
                token = self.token_provider.get()
            except AppError as error:
                raise StorageError(
                    f"Unable to acquire a Storage token: {error}"
                ) from error
            request = Request(
                url,
                method="GET",
                headers={
                    "Authorization": f"Bearer {token}",
                    "x-ms-date": formatdate(usegmt=True),
                    "x-ms-version": API_VERSION,
                    "x-ms-client-request-id": str(uuid.uuid4()),
                },
            )
            try:
                with urlopen(request, timeout=self.timeout_seconds) as response:
                    body = response.read()
                return ET.fromstring(body)
            except HTTPError as error:
                body = error.read(65536)
                code, message = storage_error_details(body)
                if error.code == 401 and attempt == 0:
                    self.token_provider.invalidate()
                    continue
                if (
                    error.code in RETRYABLE_STATUS_CODES
                    and attempt + 1 < self.max_attempts
                ):
                    retry_ms = error.headers.get("x-ms-retry-after-ms")
                    retry_after = error.headers.get("Retry-After")
                    if retry_ms and retry_ms.isdigit():
                        delay = int(retry_ms) / 1000
                    elif retry_after and retry_after.isdigit():
                        delay = int(retry_after)
                    else:
                        delay = min(2**attempt, 30)
                    time.sleep(delay)
                    continue
                raise StorageError(
                    f"{account.name}: HTTP {error.code} {code}: {message}"
                ) from error
            except ET.ParseError as error:
                raise StorageError(
                    f"{account.name}: Azure Storage returned invalid XML."
                ) from error
            except (
                URLError,
                TimeoutError,
                ConnectionError,
                http.client.HTTPException,
            ) as error:
                last_error = str(
                    error.reason if isinstance(error, URLError) else error
                )
                if attempt + 1 < self.max_attempts:
                    time.sleep(min(2**attempt, 30))
                    continue
                raise StorageError(
                    f"{account.name}: network request failed: {last_error}"
                ) from error
        raise StorageError(f"{account.name}: request failed: {last_error}")

    def delete_blob(
        self,
        account: StorageAccount,
        container: str,
        blob_name: str,
        etag: str = "",
    ) -> None:
        path = (
            f"{quote(container, safe='')}/"
            f"{quote(blob_name, safe='/')}"
        )
        headers = {"If-Match": etag} if etag else {}
        self._mutate(account, "DELETE", "DELETE", path, {}, headers)

    def delete_container(
        self,
        account: StorageAccount,
        container: str,
    ) -> None:
        self._mutate(
            account,
            "DELETE",
            "DELETE",
            quote(container, safe=""),
            {"restype": "container"},
            {},
        )

    def restore_blob(
        self,
        account: StorageAccount,
        container: str,
        blob_name: str,
        deletion_id: str = "",
    ) -> None:
        path = (
            f"{quote(container, safe='')}/"
            f"{quote(blob_name, safe='/')}"
        )
        headers: dict[str, str] = {}
        if account.hns_enabled:
            if not deletion_id:
                raise StorageError(
                    "Azure did not return the DeletionId required to "
                    "restore this HNS path."
                )
            headers["x-ms-undelete-source"] = (
                f"{quote(blob_name, safe='/')}?"
                f"deletionid={quote(deletion_id, safe='')}"
            )
        self._mutate(
            account,
            "PUT",
            "RESTORE",
            path,
            {"comp": "undelete"},
            headers,
        )

    def restore_container(
        self,
        account: StorageAccount,
        container: str,
        version: str,
    ) -> None:
        if not version:
            raise StorageError(
                "Azure did not return the version required to restore "
                "this Container."
            )
        self._mutate(
            account,
            "PUT",
            "RESTORE",
            quote(container, safe=""),
            {"restype": "container", "comp": "undelete"},
            {
                "x-ms-deleted-container-name": container,
                "x-ms-deleted-container-version": version,
            },
        )

    def _mutate(
        self,
        account: StorageAccount,
        method: str,
        operation: str,
        path: str,
        parameters: dict[str, str],
        extra_headers: dict[str, str],
    ) -> None:
        try:
            token = self.token_provider.get()
        except AppError as error:
            raise StorageError(
                f"Unable to acquire a Storage token: {error}"
            ) from error
        url = f"{account.endpoint.rstrip('/')}/{path}"
        if parameters:
            url = f"{url}?{urlencode(parameters)}"
        request = Request(
            url,
            method=method,
            headers={
                "Authorization": f"Bearer {token}",
                "x-ms-date": formatdate(usegmt=True),
                "x-ms-version": API_VERSION,
                "x-ms-client-request-id": str(uuid.uuid4()),
                **({"Content-Length": "0"} if method == "PUT" else {}),
                **extra_headers,
            },
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                response.read()
        except HTTPError as error:
            body = error.read(65536)
            code, message = storage_error_details(body)
            raise StorageError(
                f"{account.name}: {operation} HTTP {error.code} "
                f"{code}: {message}"
            ) from error
        except (
            URLError,
            TimeoutError,
            ConnectionError,
            http.client.HTTPException,
        ) as error:
            reason = str(
                error.reason if isinstance(error, URLError) else error
            )
            raise StorageError(
                f"{operation} response was not received; "
                "the outcome is unknown. "
                f"Refresh before any retry. Network error: {reason}"
            ) from error


def list_containers(
    client: BlobRestClient,
    account: StorageAccount,
    include_deleted: bool = False,
) -> list[ContainerItem]:
    containers: list[ContainerItem] = []
    marker = ""
    while True:
        parameters: dict[str, str | int] = {
            "comp": "list",
            "maxresults": 5000,
        }
        if include_deleted:
            parameters["include"] = "deleted"
        if marker:
            parameters["marker"] = marker
        root = client.get_xml(account, "", parameters)
        containers.extend(parse_container_page(root))
        marker = next_marker(root)
        if not marker:
            return containers


def parse_container_page(root: ET.Element) -> list[ContainerItem]:
    containers: list[ContainerItem] = []
    for node in root.findall("./Containers/Container"):
        name = node.findtext("Name", default="")
        if not name:
            continue
        properties = node.find("./Properties")
        property_values = (
            {
                child.tag: child.text or ""
                for child in properties
            }
            if properties is not None
            else {}
        )
        deleted = node.findtext(
            "Deleted",
            default=property_values.get("Deleted", ""),
        )
        version = node.findtext(
            "Version",
            default=property_values.get("Version", ""),
        )
        deleted_time = node.findtext(
            "DeletedTime",
            default=property_values.get("DeletedTime", ""),
        )
        retention = node.findtext(
            "RemainingRetentionDays",
            default=property_values.get("RemainingRetentionDays", ""),
        )
        containers.append(
            ContainerItem(
                name=name,
                is_deleted=_is_true(deleted),
                version=version,
                deleted_time=deleted_time,
                remaining_retention_days=_optional_int(retention),
                properties=property_values,
            )
        )
    return containers


def parse_blob_page(root: ET.Element) -> tuple[list[BlobItem], str]:
    items: list[BlobItem] = []
    blobs_node = root.find("./Blobs")
    if blobs_node is not None:
        for node in blobs_node:
            if node.tag == "BlobPrefix":
                name = node.findtext("Name", default="")
                if name:
                    items.append(BlobItem(name=name, is_prefix=True))
                continue
            if node.tag != "Blob":
                continue
            properties = node.find("./Properties")
            property_values = (
                {
                    child.tag: child.text or ""
                    for child in properties
                }
                if properties is not None
                else {}
            )
            metadata_node = node.find("./Metadata")
            metadata = (
                {
                    child.tag: child.text or ""
                    for child in metadata_node
                }
                if metadata_node is not None
                else {}
            )
            length_text = (
                properties.findtext("Content-Length", default="0")
                if properties is not None
                else "0"
            )
            try:
                size_bytes = int(length_text)
            except ValueError:
                size_bytes = 0
            deleted = node.findtext(
                "Deleted",
                default=property_values.get("Deleted", ""),
            )
            version_id = node.findtext(
                "VersionId",
                default=property_values.get("VersionId", ""),
            )
            deletion_id = node.findtext(
                "DeletionId",
                default=property_values.get("DeletionId", ""),
            )
            deleted_time = node.findtext(
                "DeletedTime",
                default=property_values.get("DeletedTime", ""),
            )
            retention = node.findtext(
                "RemainingRetentionDays",
                default=property_values.get("RemainingRetentionDays", ""),
            )
            items.append(
                BlobItem(
                    name=node.findtext("Name", default=""),
                    is_prefix=False,
                    is_deleted=_is_true(deleted),
                    version_id=version_id,
                    deletion_id=deletion_id,
                    deleted_time=deleted_time,
                    remaining_retention_days=_optional_int(retention),
                    size_bytes=size_bytes,
                    last_modified=(
                        properties.findtext("Last-Modified", default="")
                        if properties is not None
                        else ""
                    ),
                    blob_type=(
                        properties.findtext("BlobType", default="")
                        if properties is not None
                        else ""
                    ),
                    access_tier=(
                        properties.findtext("AccessTier", default="")
                        if properties is not None
                        else ""
                    ),
                    content_type=(
                        properties.findtext("Content-Type", default="")
                        if properties is not None
                        else ""
                    ),
                    etag=(
                        properties.findtext("Etag", default="")
                        if properties is not None
                        else ""
                    ),
                    properties=property_values,
                    metadata=metadata,
                )
            )
    return items, next_marker(root)


def list_hierarchy_page(
    client: BlobRestClient,
    account: StorageAccount,
    container: str,
    prefix: str,
    start_marker: str,
    page_size: int,
    max_service_pages: int = 100,
    include_deleted: bool = False,
) -> tuple[list[BlobItem], str, int]:
    items: list[BlobItem] = []
    marker = start_marker
    service_pages = 0

    while service_pages < max_service_pages:
        remaining = max(page_size - len(items), 1)
        parameters: dict[str, str | int] = {
            "restype": "container",
            "comp": "list",
            "delimiter": "/",
            "maxresults": min(remaining, 5000),
            "include": "metadata",
        }
        if include_deleted:
            if account.hns_enabled:
                parameters["showonly"] = "deleted"
            else:
                parameters["include"] = "metadata,deleted"
        if prefix:
            parameters["prefix"] = prefix
        if marker:
            parameters["marker"] = marker
        root = client.get_xml(account, container, parameters)
        page_items, marker = parse_blob_page(root)
        items.extend(page_items)
        service_pages += 1
        if not marker or len(items) >= page_size:
            break
    return items, marker, service_pages

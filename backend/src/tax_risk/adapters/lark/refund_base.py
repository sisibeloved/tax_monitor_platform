"""Idempotent Lark Base status writer for income-tax refund records."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import math
from threading import Lock
from time import monotonic
from typing import Final
from urllib.parse import urlsplit

import httpx


_DEFAULT_API_BASE_URL: Final = "https://open.feishu.cn"
_TOKEN_PATH: Final = "/open-apis/auth/v3/tenant_access_token/internal"
_IGNORED_UPDATE_METADATA: Final = frozenset({"ignored_fields"})
_FAILED_UPDATE_METADATA: Final = frozenset(
    {
        "error_fields",
        "errors",
        "failed",
        "failed_fields",
        "failed_field_ids",
        "failed_records",
        "failed_record_ids",
        "failed_record_id_list",
        "failure",
        "failures",
    }
)
_READ_ONLY_FIELD_TYPES: Final = frozenset(
    {
        "auto_number",
        "created_at",
        "created_by",
        "formula",
        "lookup",
        "updated_at",
        "updated_by",
    }
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class LarkRefundBaseError(RuntimeError):
    """Base class for stable, credential-safe Lark refund writeback failures."""

    error_code = "LARK_REFUND_BASE_ERROR"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class LarkRefundAuthenticationError(LarkRefundBaseError):
    error_code = "LARK_REFUND_AUTHENTICATION_FAILED"


class LarkRefundTransportError(LarkRefundBaseError):
    error_code = "LARK_REFUND_TRANSPORT_FAILED"


class LarkRefundHttpError(LarkRefundBaseError):
    error_code = "LARK_REFUND_HTTP_ERROR"

    def __init__(self, operation: str, status_code: int) -> None:
        super().__init__(f"Lark Base {operation} returned HTTP {status_code}")
        self.operation = operation
        self.status_code = status_code


class LarkRefundRateLimitError(LarkRefundHttpError):
    error_code = "LARK_REFUND_RATE_LIMITED"

    def __init__(self, operation: str, retry_after: float | None) -> None:
        super().__init__(operation, 429)
        self.retry_after = retry_after
        self.retry_after_seconds = retry_after


class LarkRefundApiError(LarkRefundBaseError):
    error_code = "LARK_REFUND_API_ERROR"

    def __init__(self, operation: str, api_code: int) -> None:
        super().__init__(f"Lark Base {operation} returned API code {api_code}")
        self.operation = operation
        self.api_code = api_code


class LarkRefundResponseError(LarkRefundBaseError):
    error_code = "LARK_REFUND_INVALID_RESPONSE"


class LarkRefundPaginationError(LarkRefundResponseError):
    error_code = "LARK_REFUND_PAGINATION_ERROR"


class LarkRefundUpdateRejectedError(LarkRefundResponseError):
    error_code = "LARK_REFUND_UPDATE_REJECTED"


class LarkRefundVerificationError(LarkRefundResponseError):
    error_code = "LARK_REFUND_UPDATE_NOT_CONFIRMED"


class LarkRefundPreflightError(LarkRefundResponseError):
    error_code = "LARK_REFUND_PREFLIGHT_FAILED"


class LarkRefundRecordNotFoundError(LarkRefundBaseError):
    error_code = "LARK_REFUND_RECORD_NOT_FOUND"

    def __init__(self, company_code: str) -> None:
        super().__init__("no exact Lark Base refund record was found for the company code")
        self.company_code = company_code


class LarkRefundDuplicateRecordError(LarkRefundBaseError):
    error_code = "LARK_REFUND_DUPLICATE_RECORD"

    def __init__(self, company_code: str) -> None:
        super().__init__("multiple exact Lark Base refund records were found for the company code")
        self.company_code = company_code


@dataclass(frozen=True, slots=True)
class LarkRefundBaseConfig:
    """Connection and schema identifiers for one Lark Base refund table."""

    base_token: str = field(repr=False)
    table_id: str
    company_code_field_id: str
    status_field_id: str
    app_id: str = field(repr=False)
    app_secret: str = field(repr=False)
    api_base_url: str = _DEFAULT_API_BASE_URL
    timeout: float = 30.0
    page_size: int = 100
    max_pages: int = 1_000
    token_refresh_margin: float = 300.0
    allow_untrusted_api_origin: bool = False

    def __post_init__(self) -> None:
        for name in (
            "base_token",
            "table_id",
            "company_code_field_id",
            "status_field_id",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
            normalized = value.strip()
            if any(character in normalized for character in "/?#"):
                raise ValueError(f"{name} contains unsupported characters")
            object.__setattr__(self, name, normalized)

        for name in ("app_id", "app_secret"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
            object.__setattr__(self, name, value.strip())
        if self.company_code_field_id == self.status_field_id:
            raise ValueError("company_code_field_id and status_field_id must be different")

        if not isinstance(self.api_base_url, str) or not self.api_base_url.strip():
            raise ValueError("api_base_url must be a non-empty HTTPS URL")
        normalized_base_url = self.api_base_url.strip().rstrip("/")
        parsed = urlsplit(normalized_base_url)
        if (
            parsed.scheme.lower() != "https"
            or parsed.hostname is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ValueError("api_base_url must be an HTTPS origin without credentials")
        object.__setattr__(self, "api_base_url", normalized_base_url)
        if type(self.allow_untrusted_api_origin) is not bool:
            raise TypeError("allow_untrusted_api_origin must be a boolean")
        if (
            normalized_base_url.lower() != _DEFAULT_API_BASE_URL
            and not self.allow_untrusted_api_origin
        ):
            raise ValueError(
                "api_base_url must be https://open.feishu.cn unless an explicit test override is enabled"
            )

        if isinstance(self.timeout, bool) or not isinstance(self.timeout, (int, float)):
            raise TypeError("timeout must be a number")
        if self.timeout <= 0:
            raise ValueError("timeout must be greater than zero")
        if type(self.page_size) is not int:
            raise TypeError("page_size must be an integer")
        if not 1 <= self.page_size <= 200:
            raise ValueError("page_size must be between 1 and 200")
        if type(self.max_pages) is not int:
            raise TypeError("max_pages must be an integer")
        if self.max_pages <= 0:
            raise ValueError("max_pages must be greater than zero")
        if isinstance(self.token_refresh_margin, bool) or not isinstance(
            self.token_refresh_margin, (int, float)
        ):
            raise TypeError("token_refresh_margin must be a number")
        if self.token_refresh_margin < 0:
            raise ValueError("token_refresh_margin must not be negative")


@dataclass(frozen=True, slots=True)
class LarkRefundWriteResult:
    company_code: str
    record_id: str
    desired_value: str
    previous_value: str | None
    updated: bool


@dataclass(frozen=True, slots=True)
class LarkRefundPreflightResult:
    company_code: str
    record_id: str
    company_code_field_type: str
    status_field_type: str
    status_value: str | None


@dataclass(frozen=True, slots=True)
class _RefundRecord:
    record_id: str
    company_code: str
    status: str | None


class LarkRefundBaseClient:
    """Find exactly one company record and idempotently update its refund status."""

    def __init__(
        self,
        config: LarkRefundBaseConfig,
        client: httpx.Client | None = None,
        clock: Callable[[], float] = monotonic,
        wall_clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._config = config
        self._owns_client = client is None
        self._client = client or httpx.Client(
            timeout=config.timeout,
            trust_env=False,
            follow_redirects=False,
        )
        self._clock = clock
        self._wall_clock = wall_clock
        self._tenant_access_token: str | None = None
        self._token_refresh_at = 0.0
        self._token_lock = Lock()
        self._schema_types: tuple[str, str] | None = None
        self._schema_lock = Lock()

    def __enter__(self) -> LarkRefundBaseClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        """Close only the HTTP client owned by this adapter."""

        if self._owns_client:
            self._client.close()

    def write_status(self, company_code: str, desired_value: str) -> LarkRefundWriteResult:
        """Set a company's status after proving there is exactly one matching record."""

        normalized_company_code = _required_text("company_code", company_code)
        normalized_desired_value = _required_text("desired_value", desired_value)
        record = self._require_unique_record(normalized_company_code)
        if record.status == normalized_desired_value:
            return LarkRefundWriteResult(
                company_code=normalized_company_code,
                record_id=record.record_id,
                desired_value=normalized_desired_value,
                previous_value=record.status,
                updated=False,
            )

        self._patch_status(record.record_id, normalized_desired_value)
        confirmed_records = self._find_exact_records(normalized_company_code)
        if (
            len(confirmed_records) != 1
            or confirmed_records[0].record_id != record.record_id
            or confirmed_records[0].status != normalized_desired_value
        ):
            raise LarkRefundVerificationError(
                "Lark Base did not confirm the requested refund status update"
            )
        return LarkRefundWriteResult(
            company_code=normalized_company_code,
            record_id=record.record_id,
            desired_value=normalized_desired_value,
            previous_value=record.status,
            updated=True,
        )

    def read_status(self, company_code: str) -> str | None:
        """Read one company's status while enforcing the same exact uniqueness contract."""

        normalized_company_code = _required_text("company_code", company_code)
        return self._require_unique_record(normalized_company_code).status

    def preflight(self, company_code: str) -> LarkRefundPreflightResult:
        """Validate field schema and App read access before enabling writeback."""

        normalized_company_code = _required_text("company_code", company_code)
        company_field_type, status_field_type = self.ensure_schema()
        record = self._require_unique_record(normalized_company_code)
        return LarkRefundPreflightResult(
            company_code=normalized_company_code,
            record_id=record.record_id,
            company_code_field_type=company_field_type,
            status_field_type=status_field_type,
            status_value=record.status,
        )

    def ensure_schema(self) -> tuple[str, str]:
        """Validate configured fields once per client process before delivery."""

        if self._schema_types is not None:
            return self._schema_types
        with self._schema_lock:
            if self._schema_types is not None:
                return self._schema_types
            company_field_type = self._read_field_type(
                self._config.company_code_field_id,
                role="company code",
                require_writable=False,
            )
            status_field_type = self._read_field_type(
                self._config.status_field_id,
                role="refund status",
                require_writable=True,
            )
            self._schema_types = (company_field_type, status_field_type)
            return self._schema_types

    def _read_field_type(
        self,
        field_id: str,
        *,
        role: str,
        require_writable: bool,
    ) -> str:
        payload = self._base_request(
            operation=f"{role} field preflight",
            method="GET",
            path=self._field_path(field_id),
            json_body=None,
        )
        data = _required_mapping(payload.get("data"), "field preflight data")
        field_metadata = _required_mapping(data.get("field"), "field metadata")
        returned_id = _required_response_text(field_metadata.get("id"), "field id")
        field_type = _required_response_text(field_metadata.get("type"), "field type")
        if returned_id != field_id:
            raise LarkRefundPreflightError(
                "Lark Base field preflight returned a different field identifier"
            )
        if _field_is_unreadable(field_metadata):
            raise LarkRefundPreflightError(
                f"Lark Base {role} field is not readable"
            )
        if require_writable and _field_is_read_only(field_metadata, field_type):
            raise LarkRefundPreflightError(
                "Lark Base refund status field is not writable"
            )
        if field_type != "text":
            raise LarkRefundPreflightError(
                f"Lark Base {role} field must remain a text field"
            )
        return field_type

    def _require_unique_record(self, company_code: str) -> _RefundRecord:
        matching_records = self._find_exact_records(company_code)
        if not matching_records:
            raise LarkRefundRecordNotFoundError(company_code)
        if len(matching_records) > 1:
            raise LarkRefundDuplicateRecordError(company_code)
        return matching_records[0]

    def _find_exact_records(self, company_code: str) -> list[_RefundRecord]:
        path = self._records_path("search")
        exact_records: list[_RefundRecord] = []
        seen_record_ids: set[str] = set()
        offset = 0

        for _page_number in range(1, self._config.max_pages + 1):
            payload = self._base_request(
                operation="record search",
                method="POST",
                path=path,
                json_body={
                    "keyword": company_code,
                    "search_fields": [self._config.company_code_field_id],
                    "select_fields": [
                        self._config.company_code_field_id,
                        self._config.status_field_id,
                    ],
                    "offset": offset,
                    "limit": self._config.page_size,
                },
            )
            data = _required_mapping(payload.get("data"), "record search data")
            field_ids = _required_text_list(data.get("field_id_list"), "field_id_list")
            if len(field_ids) != len(set(field_ids)):
                raise LarkRefundResponseError(
                    "Lark Base record search returned duplicate projected fields"
                )
            try:
                company_code_index = field_ids.index(self._config.company_code_field_id)
                status_index = field_ids.index(self._config.status_field_id)
            except ValueError:
                raise LarkRefundResponseError(
                    "Lark Base record search omitted a required projected field"
                ) from None

            rows = _required_list(data.get("data"), "record search rows")
            record_ids = _required_text_list(data.get("record_id_list"), "record_id_list")
            if len(rows) != len(record_ids):
                raise LarkRefundResponseError(
                    "Lark Base record search returned inconsistent row metadata"
                )
            if len(rows) > self._config.page_size:
                raise LarkRefundResponseError(
                    "Lark Base record search returned more rows than the requested page size"
                )

            for raw_row, record_id in zip(rows, record_ids, strict=True):
                row = _required_list(raw_row, "record search row")
                if max(company_code_index, status_index) >= len(row):
                    raise LarkRefundResponseError(
                        "Lark Base record search returned an incomplete projected row"
                    )
                if record_id in seen_record_ids:
                    raise LarkRefundPaginationError(
                        "Lark Base record search repeated a record across pages"
                    )
                if (
                    record_id != record_id.strip()
                    or any(character in record_id for character in "/?#")
                ):
                    raise LarkRefundResponseError(
                        "Lark Base record search returned an invalid record identifier"
                    )
                seen_record_ids.add(record_id)
                returned_company_code = _text_cell(row[company_code_index])
                if returned_company_code != company_code:
                    continue
                exact_records.append(
                    _RefundRecord(
                        record_id=record_id,
                        company_code=returned_company_code,
                        status=_text_cell(row[status_index]),
                    )
                )
                if len(exact_records) > 1:
                    raise LarkRefundDuplicateRecordError(company_code)

            has_more = data.get("has_more")
            if not isinstance(has_more, bool):
                raise LarkRefundResponseError(
                    "Lark Base record search returned an invalid has_more value"
                )
            if not has_more:
                return exact_records
            if not rows:
                raise LarkRefundPaginationError(
                    "Lark Base record search returned an empty page before completion"
                )
            offset += len(rows)

        raise LarkRefundPaginationError(
            "Lark Base record search exceeded the configured page limit"
        )

    def _patch_status(self, record_id: str, desired_value: str) -> None:
        payload = self._base_request(
            operation="record update",
            method="PATCH",
            path=self._records_path(record_id),
            json_body={self._config.status_field_id: desired_value},
        )
        _validate_update_response(payload)

    def _records_path(self, suffix: str) -> str:
        return (
            "/open-apis/base/v3/bases/"
            f"{self._config.base_token}/tables/{self._config.table_id}/records/{suffix}"
        )

    def _field_path(self, field_id: str) -> str:
        return (
            "/open-apis/base/v3/bases/"
            f"{self._config.base_token}/tables/{self._config.table_id}/fields/{field_id}"
        )

    def _base_request(
        self,
        *,
        operation: str,
        method: str,
        path: str,
        json_body: Mapping[str, object] | None,
        parameters: Mapping[str, str | int] | None = None,
    ) -> Mapping[str, object]:
        for attempt in range(2):
            token = self._get_tenant_access_token()
            try:
                return self._request_json(
                    operation=operation,
                    method=method,
                    path=path,
                    json_body=json_body,
                    parameters=parameters,
                    authorization=f"Bearer {token}",
                )
            except LarkRefundHttpError as error:
                if attempt == 0 and error.status_code in {401, 403}:
                    self._invalidate_token(token)
                    continue
                raise
        raise AssertionError("Lark Base authentication retry loop exhausted unexpectedly")

    def _get_tenant_access_token(self) -> str:
        now = self._clock()
        if self._tenant_access_token is not None and now < self._token_refresh_at:
            return self._tenant_access_token

        with self._token_lock:
            now = self._clock()
            if self._tenant_access_token is not None and now < self._token_refresh_at:
                return self._tenant_access_token
            payload = self._request_json(
                operation="authentication",
                method="POST",
                path=_TOKEN_PATH,
                json_body={
                    "app_id": self._config.app_id,
                    "app_secret": self._config.app_secret,
                },
            )
            token = _required_response_text(
                payload.get("tenant_access_token"),
                "tenant_access_token",
                authentication=True,
            )
            expires_in = payload.get("expire")
            if type(expires_in) is not int or expires_in <= 0:
                raise LarkRefundAuthenticationError(
                    "Lark authentication returned invalid token expiry metadata"
                )
            refresh_margin = min(float(self._config.token_refresh_margin), expires_in / 2)
            self._tenant_access_token = token
            self._token_refresh_at = self._clock() + expires_in - refresh_margin
            return token

    def _invalidate_token(self, rejected_token: str) -> None:
        with self._token_lock:
            if self._tenant_access_token == rejected_token:
                self._tenant_access_token = None
                self._token_refresh_at = 0.0

    def _request_json(
        self,
        *,
        operation: str,
        method: str,
        path: str,
        json_body: Mapping[str, object] | None,
        parameters: Mapping[str, str | int] | None = None,
        authorization: str | None = None,
    ) -> Mapping[str, object]:
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if authorization is not None:
            headers["Authorization"] = authorization
        try:
            if json_body is None:
                response = self._client.request(
                    method,
                    f"{self._config.api_base_url}{path}",
                    params=parameters,
                    headers=headers,
                    timeout=self._config.timeout,
                )
            else:
                response = self._client.request(
                    method,
                    f"{self._config.api_base_url}{path}",
                    params=parameters,
                    json=dict(json_body),
                    headers=headers,
                    timeout=self._config.timeout,
                )
        except httpx.RequestError:
            raise LarkRefundTransportError(
                f"Lark Base {operation} request failed"
            ) from None
        if response.status_code == 429:
            raise LarkRefundRateLimitError(
                operation,
                _parse_retry_after(
                    response.headers.get("Retry-After"),
                    now=self._wall_clock(),
                ),
            )
        if response.status_code != 200:
            raise LarkRefundHttpError(operation, response.status_code)
        try:
            raw_payload = response.json()
        except ValueError:
            raise LarkRefundResponseError(
                f"Lark Base {operation} returned invalid JSON"
            ) from None
        payload = _required_mapping(raw_payload, f"{operation} response")
        api_code = payload.get("code")
        if type(api_code) is not int:
            raise LarkRefundResponseError(
                f"Lark Base {operation} returned an invalid API code"
            )
        if api_code != 0:
            if operation == "authentication":
                raise LarkRefundAuthenticationError(
                    f"Lark authentication returned API code {api_code}"
                )
            raise LarkRefundApiError(operation, api_code)
        return payload


def _required_text(name: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _required_response_text(
    value: object,
    field_name: str,
    *,
    authentication: bool = False,
) -> str:
    if isinstance(value, str) and value:
        return value
    if authentication:
        raise LarkRefundAuthenticationError(
            "Lark authentication returned incomplete token metadata"
        )
    raise LarkRefundResponseError(
        f"Lark Base response contained an invalid {field_name}"
    )


def _required_mapping(value: object, field_name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise LarkRefundResponseError(
            f"Lark Base response contained an invalid {field_name}"
        )
    return value


def _required_list(value: object, field_name: str) -> list[object]:
    if not isinstance(value, list):
        raise LarkRefundResponseError(
            f"Lark Base response contained an invalid {field_name}"
        )
    return value


def _required_text_list(value: object, field_name: str) -> list[str]:
    items = _required_list(value, field_name)
    if any(not isinstance(item, str) or not item for item in items):
        raise LarkRefundResponseError(
            f"Lark Base response contained an invalid {field_name}"
        )
    return [item for item in items if isinstance(item, str)]


def _text_cell(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        text = value.get("text")
        return text if isinstance(text, str) else None
    if isinstance(value, list):
        parts: list[str] = []
        for part in value:
            if isinstance(part, str):
                parts.append(part)
                continue
            if isinstance(part, Mapping) and isinstance(part.get("text"), str):
                parts.append(part["text"])
                continue
            return None
        return "".join(parts)
    return None


def _validate_update_response(payload: Mapping[str, object]) -> None:
    data = payload.get("data")
    if data is not None and not isinstance(data, Mapping):
        raise LarkRefundResponseError(
            "Lark Base record update returned invalid response metadata"
        )

    if _contains_nonempty_metadata(payload, _IGNORED_UPDATE_METADATA):
        raise LarkRefundUpdateRejectedError(
            "Lark Base record update ignored one or more requested fields"
        )
    if _contains_nonempty_metadata(payload, _FAILED_UPDATE_METADATA):
        raise LarkRefundUpdateRejectedError(
            "Lark Base record update reported failed fields or records"
        )

    containers = [payload]
    if isinstance(data, Mapping):
        containers.append(data)
    for container in containers:
        for flag_name in ("updated", "success"):
            if flag_name in container and container[flag_name] is not True:
                raise LarkRefundUpdateRejectedError(
                    "Lark Base record update did not report a successful update"
                )


def _contains_nonempty_metadata(
    value: object,
    metadata_names: frozenset[str],
) -> bool:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key in metadata_names and _metadata_is_nonempty(item):
                return True
            if _contains_nonempty_metadata(item, metadata_names):
                return True
    elif isinstance(value, list):
        return any(_contains_nonempty_metadata(item, metadata_names) for item in value)
    return False


def _metadata_is_nonempty(value: object) -> bool:
    if value is None or value is False:
        return False
    if isinstance(value, (str, list, Mapping)):
        return bool(value)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value != 0
    return True


def _parse_retry_after(value: str | None, *, now: datetime) -> float | None:
    if value is None:
        return None
    try:
        retry_after = float(value.strip())
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value.strip())
        except (TypeError, ValueError, OverflowError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        retry_after = (
            retry_at.astimezone(timezone.utc) - now.astimezone(timezone.utc)
        ).total_seconds()
        return max(0.0, retry_after) if math.isfinite(retry_after) else None
    if not math.isfinite(retry_after) or retry_after < 0:
        return None
    return retry_after


def _field_is_read_only(field_metadata: Mapping[str, object], field_type: str) -> bool:
    if field_type in _READ_ONLY_FIELD_TYPES:
        return True
    if any(
        field_metadata.get(flag) is True
        for flag in ("readonly", "read_only", "is_read_only")
    ):
        return True
    return any(
        flag in field_metadata and field_metadata[flag] is not True
        for flag in ("writable", "editable", "can_write")
    )


def _field_is_unreadable(field_metadata: Mapping[str, object]) -> bool:
    return any(
        flag in field_metadata and field_metadata[flag] is not True
        for flag in ("readable", "can_read")
    )


__all__ = [
    "LarkRefundApiError",
    "LarkRefundAuthenticationError",
    "LarkRefundBaseClient",
    "LarkRefundBaseConfig",
    "LarkRefundBaseError",
    "LarkRefundDuplicateRecordError",
    "LarkRefundHttpError",
    "LarkRefundPaginationError",
    "LarkRefundPreflightError",
    "LarkRefundPreflightResult",
    "LarkRefundRateLimitError",
    "LarkRefundRecordNotFoundError",
    "LarkRefundResponseError",
    "LarkRefundTransportError",
    "LarkRefundUpdateRejectedError",
    "LarkRefundVerificationError",
    "LarkRefundWriteResult",
]

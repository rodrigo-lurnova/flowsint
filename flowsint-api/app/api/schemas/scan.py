from datetime import datetime, timezone
from typing import Any, List, Optional

from pydantic import UUID4, BaseModel, field_serializer

from .base import ORMBase


class ScanCreate(BaseModel):
    values: Optional[List[str]] = None
    sketch_id: Optional[UUID4] = None
    status: Optional[str] = None
    details: Optional[Any] = None


class ScanRead(ORMBase):
    id: UUID4
    sketch_id: Optional[UUID4]
    status: Optional[str]


def _as_utc_iso(value: Optional[datetime]) -> Optional[str]:
    """ISO 8601 with an explicit offset. Naive values are stored as UTC."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


class ScanListItem(ScanRead):
    """Light row of the per-sketch list: no details, no error."""

    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None

    @field_serializer("started_at", "completed_at")
    def _ser_dt(self, value: Optional[datetime]) -> Optional[str]:
        return _as_utc_iso(value)


class ScanDetail(ScanListItem):
    """Single scan with the stored investigation result."""

    details: Optional[Any] = None
    error: Optional[str] = None

from pydantic import BaseModel, Field
from typing import Literal, Optional

class DomainItem(BaseModel):
    domain: str
    date: str
    reason: Optional[str] = None
    whois: Optional[str] = None


class WaybackSnapshot(BaseModel):
    """Снимок в веб-архиве."""

    timestamp: str  # ISO 8601 UTC с Z
    date: str  # ГГГГ-ММ-ДД, дата снимка в UTC, без сдвига поясов
    url: str  # https://web.archive.org/web/<14 цифр>/<original>


class WaybackHistory(BaseModel):
    """История главной страницы домена в веб-архиве (HTTP 200)."""

    domain: str
    snapshot_count: int = Field(ge=0)
    count_truncated: bool
    first: Optional[WaybackSnapshot]  # None <=> snapshot_count == 0
    last: Optional[WaybackSnapshot]
    cached: bool
    checked_at: str  # ISO 8601 UTC с Z — когда данные получены из архива


class WaybackPending(BaseModel):
    """Задача в очереди сервера, результат ещё не готов (HTTP 202)."""

    domain: str
    status: Literal["pending"] = "pending"
    retry_after: int = Field(ge=1)

from pydantic import BaseModel
from typing import Optional

class DomainItem(BaseModel):
    domain: str
    date: str
    reason: Optional[str] = None
    whois: Optional[str] = None

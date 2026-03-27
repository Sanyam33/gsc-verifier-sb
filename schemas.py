from pydantic import BaseModel, AnyHttpUrl, field_validator
from typing import Optional
from uuid import UUID

class GSCVerificationCreate(BaseModel):
    site_url: AnyHttpUrl 

class GSCVerificationResult(BaseModel):
    site_url: str
    verified: bool
    permission_level: Optional[str] = None

class GSCVerificationDB(GSCVerificationResult):
    id: UUID
    email: Optional[str] = None
    google_account_id: Optional[str] = None 

    class Config:
        from_attributes = True
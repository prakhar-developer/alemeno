from pydantic import BaseModel, Field
from datetime import datetime
from typing import List, Dict, Any, Optional

class JobUploadResponse(BaseModel):
    job_id: str
    status: str

    class Config:
        from_attributes = True

class JobListElement(BaseModel):
    job_id: str = Field(alias="id")
    filename: str
    status: str
    row_count_raw: int
    row_count_clean: int
    created_at: datetime

    class Config:
        from_attributes = True
        populate_by_name = True

class JobStatusSummary(BaseModel):
    total_spend_inr: float
    total_spend_usd: float
    anomaly_count: int
    risk_level: Optional[str]

class JobStatusResponse(BaseModel):
    job_id: str = Field(alias="id")
    status: str
    filename: str
    row_count_raw: int
    row_count_clean: int
    created_at: datetime
    completed_at: Optional[datetime] = None
    summary: Optional[JobStatusSummary] = None

    class Config:
        from_attributes = True
        populate_by_name = True

class TransactionResponse(BaseModel):
    txn_id: Optional[str] = None
    date: Optional[str] = None
    merchant: str
    amount: float
    currency: str
    status: str
    category: str
    account_id: str
    notes: Optional[str] = None
    is_anomaly: bool
    anomaly_reason: Optional[str] = None

    class Config:
        from_attributes = True

class JobSummaryResponse(BaseModel):
    total_spend_inr: float
    total_spend_usd: float
    top_merchants: Optional[List[Dict[str, Any]]] = None
    anomaly_count: int
    narrative: Optional[str] = None
    risk_level: Optional[str] = None

    class Config:
        from_attributes = True

class JobResultsResponse(BaseModel):
    job_id: str = Field(alias="id")
    status: str
    cleaned_transactions: List[TransactionResponse]
    anomalies: List[TransactionResponse]
    category_spend_breakdown: Dict[str, Dict[str, float]]
    llm_summary: Optional[JobSummaryResponse] = None

    class Config:
        from_attributes = True
        populate_by_name = True

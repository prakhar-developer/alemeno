import os
import uuid
import pandas as pd
from contextlib import asynccontextmanager
from typing import List, Optional
from fastapi import FastAPI, UploadFile, File, Depends, HTTPException, Query, status
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from app.config import settings
from app.database import engine, Base, get_db
from app.models import Job, Transaction, JobSummary
from app.schemas import (
    JobUploadResponse,
    JobStatusResponse,
    JobResultsResponse,
    JobListElement,
    JobStatusSummary,
    JobSummaryResponse,
    TransactionResponse
)
from app.tasks import process_transaction_job


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    yield


app = FastAPI(
    title="Transaction Processing API",
    description="Async CSV ingestion, cleaning, anomaly detection and LLM categorisation.",
    version="1.0.0",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/", status_code=status.HTTP_200_OK)
def root():
    return {
        "message": "Transaction Processing Pipeline API is running.",
        "docs_url": "/docs",
        "redoc_url": "/redoc"
    }


@app.post("/jobs/upload", response_model=JobUploadResponse, status_code=status.HTTP_201_CREATED)
async def upload_csv(file: UploadFile = File(...), db: Session = Depends(get_db)):
    if not file.filename.endswith(".csv"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid file format. Only CSV files are allowed."
        )

    unique_filename = f"{uuid.uuid4()}_{file.filename}"
    file_path = os.path.join(settings.UPLOAD_DIR, unique_filename)

    try:
        content = await file.read()
        with open(file_path, "wb") as f:
            f.write(content)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to save uploaded file: {str(e)}"
        )

    try:
        df_raw = pd.read_csv(file_path)
        row_count_raw = len(df_raw)
    except Exception:
        try:
            row_count_raw = len(content.decode("utf-8", errors="ignore").splitlines()) - 1
        except Exception:
            row_count_raw = 0

    job = Job(
        filename=unique_filename,
        status="pending",
        row_count_raw=row_count_raw,
        row_count_clean=0
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    process_transaction_job.delay(job.id)

    return JobUploadResponse(job_id=job.id, status=job.status)


@app.get("/jobs/{job_id}/status", response_model=JobStatusResponse)
def get_job_status(job_id: str, db: Session = Depends(get_db)):
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Job with ID {job_id} not found."
        )

    summary = None
    if job.status == "completed" and job.summary:
        summary = JobStatusSummary(
            total_spend_inr=job.summary.total_spend_inr,
            total_spend_usd=job.summary.total_spend_usd,
            anomaly_count=job.summary.anomaly_count,
            risk_level=job.summary.risk_level
        )

    return JobStatusResponse(
        id=job.id,
        status=job.status,
        filename=job.filename,
        row_count_raw=job.row_count_raw,
        row_count_clean=job.row_count_clean,
        created_at=job.created_at,
        completed_at=job.completed_at,
        summary=summary
    )


@app.get("/jobs/{job_id}/results", response_model=JobResultsResponse)
def get_job_results(job_id: str, db: Session = Depends(get_db)):
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Job with ID {job_id} not found."
        )

    if job.status != "completed":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Job is not completed yet. Current status: {job.status}."
        )

    transactions = db.query(Transaction).filter(Transaction.job_id == job_id).all()
    anomalies = [t for t in transactions if t.is_anomaly]

    breakdown: dict = {}
    for t in transactions:
        cat = t.category
        if cat not in breakdown:
            breakdown[cat] = {"INR": 0.0, "USD": 0.0}
        if t.currency in ("INR", "USD"):
            breakdown[cat][t.currency] = round(breakdown[cat][t.currency] + t.amount, 2)

    db_summary = db.query(JobSummary).filter(JobSummary.job_id == job_id).first()
    llm_summary = None
    if db_summary:
        llm_summary = JobSummaryResponse(
            total_spend_inr=db_summary.total_spend_inr,
            total_spend_usd=db_summary.total_spend_usd,
            top_merchants=db_summary.top_merchants,
            anomaly_count=db_summary.anomaly_count,
            narrative=db_summary.narrative,
            risk_level=db_summary.risk_level
        )

    def to_schema(t):
        return TransactionResponse(
            txn_id=t.txn_id,
            date=t.date.strftime("%Y-%m-%d") if t.date else None,
            merchant=t.merchant,
            amount=t.amount,
            currency=t.currency,
            status=t.status,
            category=t.category,
            account_id=t.account_id,
            notes=t.notes,
            is_anomaly=t.is_anomaly,
            anomaly_reason=t.anomaly_reason
        )

    return JobResultsResponse(
        id=job.id,
        status=job.status,
        cleaned_transactions=[to_schema(t) for t in transactions],
        anomalies=[to_schema(t) for t in anomalies],
        category_spend_breakdown=breakdown,
        llm_summary=llm_summary
    )


@app.get("/jobs", response_model=List[JobListElement])
def list_jobs(
    status: Optional[str] = Query(None),
    db: Session = Depends(get_db)
):
    q = db.query(Job)
    if status:
        q = q.filter(Job.status == status.strip().lower())
    return q.order_by(Job.created_at.desc()).all()

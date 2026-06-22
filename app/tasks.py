import os
import logging
import traceback
import pandas as pd
from datetime import datetime
from app.celery_app import celery_app
from app.config import settings
from app.database import SessionLocal
from app.models import Job, Transaction, JobSummary
from app.utils import clean_and_parse_csv, detect_anomalies
from app.llm import classify_transactions_batch, generate_narrative_summary

logger = logging.getLogger(__name__)


def _top_merchants(df: pd.DataFrame) -> list:
    tmp = df.copy()
    tmp["inr_equiv"] = tmp.apply(
        lambda r: r["amount"] * 83.0 if r["currency"] == "USD" else r["amount"], axis=1
    )
    top = tmp.groupby("merchant")["inr_equiv"].sum().nlargest(3).index.tolist()
    result = []
    for m in top:
        sub = df[df["merchant"] == m]
        result.append({
            "merchant": m,
            "spend_inr": round(float(sub[sub["currency"] == "INR"]["amount"].sum()), 2),
            "spend_usd": round(float(sub[sub["currency"] == "USD"]["amount"].sum()), 2),
        })
    return result


@celery_app.task(name="app.tasks.process_transaction_job", bind=True, max_retries=1)
def process_transaction_job(self, job_id: str):
    logger.info(f"Starting job {job_id}")
    db = SessionLocal()

    try:
        job = db.query(Job).filter(Job.id == job_id).first()
        if not job:
            logger.error(f"Job {job_id} not found")
            return

        job.status = "processing"
        db.commit()

        file_path = os.path.join(settings.UPLOAD_DIR, job.filename)
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"File not found: {file_path}")

        df = clean_and_parse_csv(file_path)
        job.row_count_clean = len(df)
        db.commit()

        df = detect_anomalies(df)

        uncat = df[df["category"] == "Uncategorised"].index.tolist()
        if uncat:
            logger.info(f"{len(uncat)} transactions need classification")
            batch_input = [
                {
                    "id": int(idx),
                    "merchant": df.loc[idx, "merchant"],
                    "amount": float(df.loc[idx, "amount"]),
                    "currency": df.loc[idx, "currency"],
                    "notes": df.loc[idx, "notes"] if pd.notna(df.loc[idx, "notes"]) else "",
                }
                for idx in uncat
            ]
            for i in range(0, len(batch_input), 50):
                batch = batch_input[i:i + 50]
                try:
                    cats, raw = classify_transactions_batch(batch)
                    for idx, cat in cats.items():
                        df.at[idx, "category"] = cat
                        df.at[idx, "llm_category"] = cat
                        df.at[idx, "llm_raw_response"] = raw
                        df.at[idx, "llm_failed"] = False
                except Exception as e:
                    logger.error(f"Batch classification failed: {e}")
                    for tx in batch:
                        df.at[tx["id"], "llm_failed"] = True

        rows = []
        for idx, row in df.iterrows():
            rows.append(Transaction(
                job_id=job_id,
                txn_id=row["txn_id"] if pd.notna(row["txn_id"]) else None,
                date=row["date"] if pd.notna(row["date"]) else None,
                merchant=row["merchant"],
                amount=float(row["amount"]),
                currency=row["currency"],
                status=row["status"],
                category=row["category"],
                account_id=row["account_id"],
                notes=row["notes"] if pd.notna(row["notes"]) else None,
                is_anomaly=bool(row["is_anomaly"]),
                anomaly_reason=row["anomaly_reason"] if row["is_anomaly"] else None,
                llm_category=row.get("llm_category") if "llm_category" in row and pd.notna(row.get("llm_category")) else None,
                llm_raw_response=row.get("llm_raw_response") if "llm_raw_response" in row and pd.notna(row.get("llm_raw_response")) else None,
                llm_failed=bool(row.get("llm_failed", False)),
            ))
        db.add_all(rows)
        db.commit()

        stats = {
            "total_spend_inr": round(float(df[df["currency"] == "INR"]["amount"].sum()), 2),
            "total_spend_usd": round(float(df[df["currency"] == "USD"]["amount"].sum()), 2),
            "anomaly_count": int(df["is_anomaly"].sum()),
            "top_merchants": _top_merchants(df),
        }

        context = [
            {
                "merchant": row["merchant"],
                "amount": float(row["amount"]),
                "currency": row["currency"],
                "category": row["category"],
                "is_anomaly": bool(row["is_anomaly"]),
                "anomaly_reason": row["anomaly_reason"] if row["is_anomaly"] else "",
            }
            for _, row in df.iterrows()
        ]

        summary_data = generate_narrative_summary(context, stats)

        db.add(JobSummary(
            job_id=job_id,
            total_spend_inr=summary_data["total_spend_inr"],
            total_spend_usd=summary_data["total_spend_usd"],
            top_merchants=summary_data["top_merchants"],
            anomaly_count=summary_data["anomaly_count"],
            narrative=summary_data.get("narrative"),
            risk_level=summary_data.get("risk_level"),
        ))

        job.status = "completed"
        job.completed_at = datetime.utcnow()
        db.commit()
        logger.info(f"Job {job_id} completed")

    except Exception as e:
        logger.exception(f"Job {job_id} failed")
        db.rollback()
        try:
            job = db.query(Job).filter(Job.id == job_id).first()
            if job:
                job.status = "failed"
                job.completed_at = datetime.utcnow()
                job.error_message = f"{str(e)}\n\n{traceback.format_exc()}"
                db.commit()
        except Exception as inner:
            logger.error(f"Could not update job status: {inner}")
    finally:
        db.close()

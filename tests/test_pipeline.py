import os
import pytest
import pandas as pd
from datetime import date
from unittest.mock import patch
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import sessionmaker

from app.database import Base, get_db
from app.config import settings
from app.main import app
from app.models import Job, Transaction, JobSummary
from app.utils import clean_amount, clean_date, clean_and_parse_csv, detect_anomalies
from app.llm import mock_classify_transaction, generate_narrative_summary


def test_clean_amount():
    assert clean_amount("123.45") == 123.45
    assert clean_amount("$1000") == 1000.0
    assert clean_amount("  $4,500.50 ") == 4500.50
    assert clean_amount("invalid") == 0.0
    assert clean_amount(None) == 0.0


def test_clean_date():
    assert clean_date("23-11-2024") == date(2024, 11, 23)
    assert clean_date("2024/02/05") == date(2024, 2, 5)
    assert clean_date("2024-07-15") == date(2024, 7, 15)
    assert clean_date("invalid-date") is None
    assert clean_date(None) is None


def test_utils_cleaning_and_duplicates(tmp_path):
    csv_content = (
        "txn_id,date,merchant,amount,currency,status,category,account_id,notes\n"
        "TXN1,04-09-2024,Flipkart,108.55,INR,SUCCESS,Shopping,ACC1,notes1\n"
        "TXN1,04-09-2024,Flipkart,108.55,INR,SUCCESS,Shopping,ACC1,notes1\n"
        "TXN2,2024/02/05,Swiggy,$10.00,inr,success,,ACC2,\n"
        ",15-05-2024,Ola,200.0,USD,failed,Transport,ACC2,\n"
    )
    f = tmp_path / "test.csv"
    f.write_text(csv_content)

    df = clean_and_parse_csv(str(f))
    assert len(df) == 3

    swiggy = df[df["merchant"] == "Swiggy"].iloc[0]
    assert swiggy["amount"] == 10.0
    assert swiggy["date"] == date(2024, 2, 5)
    assert swiggy["currency"] == "INR"
    assert swiggy["status"] == "SUCCESS"
    assert swiggy["category"] == "Uncategorised"

    ola = df[df["merchant"] == "Ola"].iloc[0]
    assert pd.isna(ola["txn_id"])


def test_anomaly_detection():
    data = {
        "account_id": ["ACC1", "ACC1", "ACC1", "ACC1", "ACC2"],
        "amount":     [10.0,   10.0,   12.0,   40.0,   50.0],
        "currency":   ["INR",  "INR",  "INR",  "INR",  "USD"],
        "merchant":   ["Zomato", "Amazon", "Ola", "Flipkart", "Swiggy"],
        "txn_id":     ["T1", "T2", "T3", "T4", "T5"],
    }
    df = pd.DataFrame(data)
    df = detect_anomalies(df)

    t4 = df[df["txn_id"] == "T4"].iloc[0]
    assert bool(t4["is_anomaly"]) is True
    assert "exceeds 3x median" in t4["anomaly_reason"]

    t5 = df[df["txn_id"] == "T5"].iloc[0]
    assert bool(t5["is_anomaly"]) is True
    assert "USD transaction for domestic-only brand" in t5["anomaly_reason"]

    for tid in ("T1", "T2", "T3"):
        row = df[df["txn_id"] == tid].iloc[0]
        assert bool(row["is_anomaly"]) is False


def test_mock_llm_classification():
    assert mock_classify_transaction("Swiggy", "") == "Food"
    assert mock_classify_transaction("Amazon", "") == "Shopping"
    assert mock_classify_transaction("Jio Recharge", "") == "Utilities"
    assert mock_classify_transaction("HDFC ATM", "") == "Cash Withdrawal"
    assert mock_classify_transaction("OtherUnknownBrand", "") == "Other"


def test_mock_llm_summary():
    stats = {
        "total_spend_inr": 1000.0,
        "total_spend_usd": 10.0,
        "anomaly_count": 2,
        "top_merchants": [{"merchant": "Amazon", "spend_inr": 1000.0, "spend_usd": 0.0}]
    }
    summary = generate_narrative_summary([], stats)
    assert summary["total_spend_inr"] == 1000.0
    assert summary["total_spend_usd"] == 10.0
    assert summary["anomaly_count"] == 2
    assert "narrative" in summary
    assert summary["risk_level"] in ("low", "medium", "high")


TEST_DB_URL = "sqlite:///:memory:"
test_engine = create_engine(
    TEST_DB_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool
)
TestSession = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)


def override_get_db():
    db = TestSession()
    try:
        yield db
    finally:
        db.close()


app.dependency_overrides[get_db] = override_get_db


@pytest.fixture(autouse=True)
def setup_db():
    Base.metadata.create_all(bind=test_engine)
    yield
    Base.metadata.drop_all(bind=test_engine)


client = TestClient(app)


def test_root_endpoint():
    r = client.get("/")
    assert r.status_code == 200
    assert "API is running" in r.json()["message"]


@patch("app.main.process_transaction_job.delay")
def test_job_upload_flow(mock_delay, tmp_path):
    csv_content = (
        "txn_id,date,merchant,amount,currency,status,category,account_id,notes\n"
        "TXN1,04-09-2024,Flipkart,108.55,INR,SUCCESS,Shopping,ACC1,notes1"
    )
    f = tmp_path / "transactions.csv"
    f.write_text(csv_content)

    with open(f, "rb") as fh:
        r = client.post("/jobs/upload", files={"file": ("transactions.csv", fh, "text/csv")})

    assert r.status_code == 201
    data = r.json()
    assert "job_id" in data
    assert data["status"] == "pending"
    mock_delay.assert_called_once_with(data["job_id"])

    job_id = data["job_id"]
    sr = client.get(f"/jobs/{job_id}/status")
    assert sr.status_code == 200
    assert sr.json()["status"] == "pending"
    assert sr.json()["id"] == job_id

    lr = client.get("/jobs")
    assert lr.status_code == 200
    assert len(lr.json()) == 1
    assert lr.json()[0]["id"] == job_id


def test_job_not_found():
    r = client.get("/jobs/non-existent-uuid/status")
    assert r.status_code == 404


def test_results_not_completed_yet():
    db = TestSession()
    db.add(Job(id="fake-uuid-123", filename="test.csv", status="processing"))
    db.commit()
    db.close()

    r = client.get("/jobs/fake-uuid-123/results")
    assert r.status_code == 400
    assert "not completed yet" in r.json()["detail"]

import pandas as pd
import numpy as np
from datetime import datetime, date
from typing import Optional

DOMESTIC_BRANDS = {"swiggy", "ola", "irctc", "zomato", "flipkart", "jio recharge", "hdfc atm"}


def clean_amount(val) -> float:
    if pd.isna(val) or val == "":
        return 0.0
    val_str = str(val).strip().replace("$", "").replace(",", "")
    try:
        return float(val_str)
    except ValueError:
        return 0.0


def clean_date(val) -> Optional[date]:
    if pd.isna(val) or val == "":
        return None
    val_str = str(val).strip()
    for fmt in ("%d-%m-%Y", "%Y/%m/%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(val_str, fmt).date()
        except ValueError:
            continue
    try:
        return pd.to_datetime(val_str).date()
    except Exception:
        return None


def clean_and_parse_csv(file_path: str) -> pd.DataFrame:
    df = pd.read_csv(file_path)

    for col in df.columns:
        if df[col].dtype == object:
            df[col] = df[col].astype(str).str.strip()

    df = df.drop_duplicates().copy()

    df["txn_id"] = df["txn_id"].replace("", np.nan).replace("nan", np.nan).replace("None", np.nan)
    df["notes"] = df["notes"].replace("", np.nan).replace("nan", np.nan).replace("None", np.nan)

    df["amount"] = df["amount"].apply(clean_amount)
    df["date"] = df["date"].apply(clean_date)

    df["currency"] = df["currency"].fillna("INR").replace("nan", "INR").astype(str).str.upper()
    df["status"] = df["status"].fillna("PENDING").replace("nan", "PENDING").astype(str).str.upper()

    df["category"] = df["category"].replace("", np.nan).replace("nan", np.nan).fillna("Uncategorised")
    df["merchant"] = df["merchant"].fillna("Unknown").replace("nan", "Unknown")
    df["account_id"] = df["account_id"].fillna("Unknown").replace("nan", "Unknown")

    return df


def detect_anomalies(df: pd.DataFrame) -> pd.DataFrame:
    valid_accounts = df[df["account_id"] != "Unknown"]
    medians = valid_accounts.groupby("account_id")["amount"].median().to_dict()

    df["is_anomaly"] = False
    df["anomaly_reason"] = ""

    for idx, row in df.iterrows():
        reasons = []
        acc = row["account_id"]
        amount = row["amount"]
        currency = row["currency"]
        merchant = row["merchant"]

        if acc in medians:
            median_amt = medians[acc]
            if median_amt > 0 and amount > 3 * median_amt:
                reasons.append(f"Amount {amount} exceeds 3x median of {median_amt:.2f} for account {acc}")

        if currency == "USD" and merchant.lower() in DOMESTIC_BRANDS:
            reasons.append(f"USD transaction for domestic-only brand '{merchant}'")

        if reasons:
            df.at[idx, "is_anomaly"] = True
            df.at[idx, "anomaly_reason"] = "; ".join(reasons)

    return df

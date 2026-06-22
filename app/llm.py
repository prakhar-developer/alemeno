import json
import logging
import httpx
from typing import List, Dict, Any, Tuple
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from app.config import settings

logger = logging.getLogger(__name__)

ALLOWED_CATEGORIES = {
    "Food", "Shopping", "Travel", "Transport", "Utilities",
    "Cash Withdrawal", "Entertainment", "Other"
}


def mock_classify_transaction(merchant: str, notes: str) -> str:
    m = str(merchant).lower()
    n = str(notes).lower() if notes else ""

    if any(k in m for k in ("swiggy", "zomato", "starbucks", "restaurant", "food", "cafe")):
        return "Food"
    if any(k in m for k in ("amazon", "flipkart", "myntra", "grocery", "mall", "store")):
        return "Shopping"
    if any(k in m for k in ("irctc", "makemytrip", "flight", "hotel", "travel", "uber", "ola")):
        if any(k in m for k in ("uber", "ola", "taxi", "metro", "auto")):
            return "Transport"
        return "Travel"
    if any(k in m for k in ("jio", "recharge", "electric", "bill", "water", "utilities", "telecom")):
        return "Utilities"
    if any(k in m for k in ("hdfc", "atm", "cash", "withdrawal")):
        return "Cash Withdrawal"
    if any(k in m for k in ("bookmyshow", "cinema", "movie", "entertainment", "netflix", "spotify")):
        return "Entertainment"
    if "refund" in n or "duplicate" in n:
        return "Other"
    return "Other"


_RETRYABLE = (
    httpx.HTTPStatusError,
    httpx.ConnectError,
    httpx.TimeoutException,
    httpx.ReadTimeout,
)


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=2, max=10),
    retry=retry_if_exception_type(_RETRYABLE),
    reraise=True
)
def _call_gemini(prompt: str) -> str:
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models"
        f"/{settings.GEMINI_MODEL}:generateContent?key={settings.GEMINI_API_KEY}"
    )
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json"}
    }
    with httpx.Client(timeout=30.0) as client:
        resp = client.post(url, json=payload)
        resp.raise_for_status()
        data = resp.json()
        try:
            return data["candidates"][0]["content"]["parts"][0]["text"].strip()
        except (KeyError, IndexError):
            logger.error(f"Unexpected Gemini response shape: {data}")
            raise ValueError("Malformed Gemini response")


def classify_transactions_batch(transactions: List[Dict[str, Any]]) -> Tuple[Dict[int, str], str]:
    if not settings.GEMINI_API_KEY or not settings.GEMINI_API_KEY.strip():
        result = {
            tx["id"]: mock_classify_transaction(tx.get("merchant", ""), tx.get("notes", ""))
            for tx in transactions
        }
        return result, "<mock>"

    prompt = (
        "You are a financial transaction classifier.\n"
        "Assign each transaction exactly one category from: Food, Shopping, Travel, Transport, "
        "Utilities, Cash Withdrawal, Entertainment, Other.\n"
        "Use the merchant name and notes field to decide.\n"
        "Return a JSON object where keys are transaction IDs (strings) and values are categories.\n"
        "No markdown, no explanation — only the JSON object.\n\n"
        f"Transactions:\n{json.dumps(transactions)}"
    )

    try:
        raw = _call_gemini(prompt)
        parsed = json.loads(raw)
        result = {}
        for k, v in parsed.items():
            cat = str(v).strip()
            result[int(k)] = cat if cat in ALLOWED_CATEGORIES else "Other"
        return result, raw
    except Exception:
        logger.exception("LLM batch classification failed")
        raise RuntimeError("LLM batch classification failed")


def generate_narrative_summary(transactions: List[Dict[str, Any]], stats: Dict[str, Any]) -> Dict[str, Any]:
    inr = stats["total_spend_inr"]
    usd = stats["total_spend_usd"]
    count = stats["anomaly_count"]
    merchants = stats["top_merchants"]

    if not settings.GEMINI_API_KEY or not settings.GEMINI_API_KEY.strip():
        names = ", ".join(m["merchant"] for m in merchants) or "N/A"
        risk = "high" if count > 4 else ("medium" if count > 1 else "low")
        return {
            "total_spend_inr": inr,
            "total_spend_usd": usd,
            "top_merchants": merchants,
            "anomaly_count": count,
            "narrative": (
                f"Total spend: {inr:,.2f} INR and {usd:,.2f} USD across multiple accounts. "
                f"Top merchants: {names}. "
                f"{count} anomalies flagged — {risk} risk profile."
            ),
            "risk_level": risk
        }

    prompt = (
        "You are a financial analyst. Summarize the transaction data below.\n"
        f"- Total INR spend: {inr:.2f}\n"
        f"- Total USD spend: {usd:.2f}\n"
        f"- Anomaly count: {count}\n"
        f"- Top merchants: {json.dumps(merchants)}\n\n"
        "Return a JSON object with these exact keys:\n"
        f'{{"total_spend_inr": {inr}, "total_spend_usd": {usd}, '
        f'"top_merchants": {json.dumps(merchants)}, "anomaly_count": {count}, '
        '"narrative": "<2-3 sentence summary>", "risk_level": "<low|medium|high>"}\n\n'
        f"Context (truncated):\n{json.dumps(transactions[:60])}"
    )

    try:
        raw = _call_gemini(prompt)
        result = json.loads(raw)
        result["total_spend_inr"] = inr
        result["total_spend_usd"] = usd
        result["top_merchants"] = merchants
        result["anomaly_count"] = count
        if result.get("risk_level") not in ("low", "medium", "high"):
            result["risk_level"] = "medium"
        result.setdefault("narrative", "Summary generated.")
        return result
    except Exception:
        logger.exception("LLM narrative generation failed")
        risk = "high" if count > 4 else ("medium" if count > 1 else "low")
        return {
            "total_spend_inr": inr,
            "total_spend_usd": usd,
            "top_merchants": merchants,
            "anomaly_count": count,
            "narrative": "Summary unavailable.",
            "risk_level": risk
        }

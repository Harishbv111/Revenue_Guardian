import os
import hmac
import hashlib
import json
import requests

from fastapi import Request, Header
from dotenv import load_dotenv
from datetime import datetime
from typing import Optional
from fastapi import FastAPI, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from database import SessionLocal, engine, Base
from models import RecoveryCase
from ai_recovery import analyze_payment_with_gemini
from ai_insights import generate_merchant_insights



Base.metadata.create_all(bind=engine)
app = FastAPI(title="Revenue Guardian", description="AI Revenue Recovery Agent for Razorpay merchants", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])
load_dotenv()

RAZORPAY_KEY_ID = os.getenv("RAZORPAY_KEY_ID")
RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET")
RAZORPAY_WEBHOOK_SECRET = os.getenv("RAZORPAY_WEBHOOK_SECRET")

RAZORPAY_API_BASE = "https://api.razorpay.com/v1"
STORED_LINKS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stored_payment_links.json")

def save_payment_link_to_file(entry: dict):
    try:
        links = []
        if os.path.exists(STORED_LINKS_FILE):
            with open(STORED_LINKS_FILE, "r", encoding="utf-8") as f:
                try:
                    links = json.load(f)
                except Exception:
                    links = []
        # Update if exists, else prepend
        filtered = [item for item in links if item.get("case_id") != entry.get("case_id")]
        filtered.insert(0, entry)
        with open(STORED_LINKS_FILE, "w", encoding="utf-8") as f:
            json.dump(filtered, f, indent=2)
    except Exception as err:
        print(f"Error saving payment link to file: {err}")

def update_payment_link_status_in_file(case_id: str, new_status: str, recovered_amount: float = None):
    try:
        if not os.path.exists(STORED_LINKS_FILE):
            return
        with open(STORED_LINKS_FILE, "r", encoding="utf-8") as f:
            links = json.load(f)
        for item in links:
            if item.get("case_id") == case_id:
                item["status"] = new_status
                if recovered_amount is not None:
                    item["recovered_amount"] = recovered_amount
                item["updated_at"] = datetime.now().isoformat()
        with open(STORED_LINKS_FILE, "w", encoding="utf-8") as f:
            json.dump(links, f, indent=2)
    except Exception as err:
        print(f"Error updating payment link status in file: {err}")

def get_db():
    db = SessionLocal()
    try: yield db
    finally: db.close()

class RazorpayWebhook(BaseModel):
    event: str
    payment_id: str
    amount: float = Field(gt=0)
    currency: str = "INR"
    failure_reason: Optional[str] = None
class SimulatePayment(BaseModel):
    amount: float = Field(gt=0)
    failure_reason: str
    currency: str = "INR"
class RecoveryResult(BaseModel):
    result: str
    recovered_amount: float = Field(default=0, ge=0)

def calculate_recovery(amount, failure_reason):
    reason = (failure_reason or "").lower()
    if "insufficient" in reason: probability = 0.60
    elif "timeout" in reason: probability = 0.75
    elif "network" in reason or "technical" in reason: probability = 0.80
    elif "authentication" in reason or "auth" in reason: probability = 0.70
    elif "expired" in reason: probability = 0.35
    elif "declined" in reason: probability = 0.45
    else: probability = 0.50
    expected = round(amount * probability, 2)
    priority = "HIGH" if expected >= 5000 or probability >= 0.75 else "MEDIUM" if expected >= 1000 or probability >= 0.50 else "LOW"
    return {"recovery_probability": probability, "expected_recovery": expected, "priority": priority}

def choose_recovery_action(failure_reason, probability):
    reason = (failure_reason or "").lower()
    if any(x in reason for x in ("timeout", "network", "technical", "authentication", "auth")):
        return {"action":"RETRY_PAYMENT", "reason":"Temporary or recoverable payment failure"}
    if "insufficient" in reason: return {"action":"ALTERNATE_PAYMENT", "reason":"Customer balance may be insufficient"}
    if "expired" in reason: return {"action":"ALTERNATE_PAYMENT", "reason":"Payment method may need replacement"}
    if probability >= .75: return {"action":"RETRY_PAYMENT", "reason":"High recovery opportunity"}
    if probability >= .50: return {"action":"ALTERNATE_PAYMENT", "reason":"Moderate recovery opportunity"}
    return {"action":"MANUAL_REVIEW", "reason":"Lower recovery opportunity requires review"}

def create_razorpay_recovery_link(case):
    """
    Creates a real Razorpay Payment Link using Test Mode API credentials.

    This does NOT retry the old failed payment.
    It creates a new payment collection request for the same amount.
    """

    if not RAZORPAY_KEY_ID or not RAZORPAY_KEY_SECRET:
        raise RuntimeError(
            "Razorpay API credentials are not configured"
        )

    amount_paise = int(round(float(case.amount) * 100))

    payload = {
        "amount": amount_paise,
        "currency": case.currency,
        "accept_partial": False,
        "description": f"Revenue recovery for {case.case_id}",
        "reference_id": case.case_id,
        "notes": {
            "recovery_case": case.case_id,
            "original_payment_id": case.payment_id,
            "failure_reason": case.failure_reason or "Unknown"
        }
    }

    response = requests.post(
        f"{RAZORPAY_API_BASE}/payment_links",
        auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET),
        json=payload,
        timeout=15
    )

    if not response.ok:
        try:
            error_data = response.json()
        except Exception:
            error_data = response.text

        raise RuntimeError(
            f"Razorpay API error {response.status_code}: {error_data}"
        )

    return response.json()

def parse_ai_recommendation(text, default_action, default_reason, default_priority):
    action, reason, priority, message = default_action, default_reason, default_priority, None
    for raw in (text or "").splitlines():
        line = raw.strip()
        if line.startswith("ACTION:") and line.split(":",1)[1].strip().upper() in {"RETRY_PAYMENT","ALTERNATE_PAYMENT","MANUAL_REVIEW"}: action = line.split(":",1)[1].strip().upper()
        elif line.startswith("REASON:"): reason = line.split(":",1)[1].strip()[:500]
        elif line.startswith("PRIORITY:") and line.split(":",1)[1].strip().upper() in {"HIGH","MEDIUM","LOW"}: priority = line.split(":",1)[1].strip().upper()
        elif line.startswith("CUSTOMER_MESSAGE:"): message = line.split(":",1)[1].strip()[:1000]
    return action, reason, priority, message

def create_case(db, payment_id, amount, currency, failure_reason):
    recovery = calculate_recovery(amount, failure_reason)
    action = choose_recovery_action(failure_reason, recovery["recovery_probability"])
    ai_action, ai_reason, ai_priority = action["action"], action["reason"], recovery["priority"]
    message = f"We couldn't complete your payment of ₹{amount:,.2f}. Please try another payment method."
    try:
        text = analyze_payment_with_gemini(amount, currency, failure_reason, recovery["recovery_probability"])
        ai_action, ai_reason, ai_priority, ai_message = parse_ai_recommendation(text, ai_action, ai_reason, ai_priority)
        if ai_message: message = ai_message
    except Exception as error: print(f"Gemini unavailable for case analysis: {error}")
    latest = db.query(RecoveryCase).order_by(RecoveryCase.id.desc()).first()
    number = latest.id + 1 if latest else 1
    case = RecoveryCase(case_id=f"RC-{number:03d}", payment_id=payment_id, amount=amount, currency=currency, failure_reason=failure_reason, recovery_probability=recovery["recovery_probability"], expected_recovery=recovery["expected_recovery"], priority=ai_priority, recommended_action=ai_action, action_reason=ai_reason, customer_message=message, status="PENDING")
    db.add(case); db.commit(); db.refresh(case)
    return case

def case_dict(c):
    return {
        "case_id": c.case_id,
        "payment_id": c.payment_id,
        "amount": c.amount,
        "currency": c.currency,
        "failure_reason": c.failure_reason,
        "recovery_probability": c.recovery_probability,
        "expected_recovery": c.expected_recovery,
        "priority": c.priority,
        "recommended_action": c.recommended_action,
        "action_reason": c.action_reason,
        "customer_message": c.customer_message,
        "status": c.status,
        "recovery_result": c.recovery_result,
        "recovered_amount": c.recovered_amount,
        "action_triggered_at": c.action_triggered_at,
        "recovered_at": c.recovered_at,
        "payment_link": getattr(c, "payment_link", None),
        "payment_link_id": getattr(c, "payment_link_id", None)
    }

@app.get("/")
def home(): return {"message":"Revenue Guardian API is running!","status":"healthy"}
@app.get("/health")
def health(): return {"status":"healthy"}

@app.post("/simulate/payment-failure")
def simulate(data: SimulatePayment, db: Session=Depends(get_db)):
    latest=db.query(RecoveryCase).order_by(RecoveryCase.id.desc()).first(); number=latest.id+1 if latest else 1
    case=create_case(db,f"pay_demo_{number:03d}",data.amount,data.currency,data.failure_reason)
    return {"status":"success","message":"Payment failure simulated successfully","case":case_dict(case)}

@app.post("/webhooks/razorpay")
def webhook(data:RazorpayWebhook,db:Session=Depends(get_db)):
    if data.event!="payment.failed": return {"status":"ignored","reason":"Not a failed payment"}
    existing=db.query(RecoveryCase).filter(RecoveryCase.payment_id==data.payment_id).first()
    if existing: return {"status":"already_processed","payment_id":data.payment_id,"case_id":existing.case_id}
    case=create_case(db,data.payment_id,data.amount,data.currency,data.failure_reason)
    return {"status":"recovery_case_created","case":case_dict(case)}
@app.post("/webhooks/razorpay/real")
async def razorpay_real_webhook(
    request: Request,
    db: Session = Depends(get_db)
):
    body = await request.body()

    signature = request.headers.get("X-Razorpay-Signature")

    if not signature:
        return {
            "status": "error",
            "message": "Missing Razorpay webhook signature"
        }

    if not RAZORPAY_WEBHOOK_SECRET:
        return {
            "status": "error",
            "message": "RAZORPAY_WEBHOOK_SECRET not configured"
        }

    expected_signature = hmac.new(
        RAZORPAY_WEBHOOK_SECRET.encode(),
        body,
        hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(signature, expected_signature):
        return {
            "status": "error",
            "message": "Invalid Razorpay webhook signature"
        }

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return {
            "status": "error",
            "message": "Invalid JSON payload"
        }

    event = payload.get("event")

    # =========================================
    # SUCCESSFUL RECOVERY PAYMENT
    # =========================================

    if event in {"payment_link.paid", "payment.captured", "payment.authorized", "order.paid"}:

        payload_obj = payload.get("payload", {})
        plink_entity = payload_obj.get("payment_link", {}).get("entity", {})
        payment_entity = payload_obj.get("payment", {}).get("entity", {})

        payment_id = payment_entity.get("id") or plink_entity.get("id")
        plink_id = plink_entity.get("id") or payment_entity.get("payment_link_id") or payment_entity.get("plink_id")

        # Try to identify the recovery case from Razorpay notes or reference_id
        recovery_case_id = (
            payment_entity.get("notes", {}).get("recovery_case")
            or plink_entity.get("notes", {}).get("recovery_case")
            or plink_entity.get("reference_id")
        )

        existing = None

        if recovery_case_id:
            existing = (
                db.query(RecoveryCase)
                .filter(RecoveryCase.case_id == recovery_case_id)
                .first()
            )

        # Fallback 1: check payment_link_id
        if not existing and plink_id:
            existing = (
                db.query(RecoveryCase)
                .filter(RecoveryCase.payment_link_id == plink_id)
                .first()
            )

        # Fallback 2: check original payment ID
        if not existing and payment_id:
            existing = (
                db.query(RecoveryCase)
                .filter(RecoveryCase.payment_id == payment_id)
                .first()
            )

        if not existing:
            return {
                "status": "payment_received",
                "payment_id": payment_id,
                "plink_id": plink_id,
                "message": "Payment received but no recovery case was matched"
            }

        # Convert Razorpay paise → INR
        amt_raw = (
            payment_entity.get("amount")
            or plink_entity.get("amount_paid")
            or plink_entity.get("amount")
        )
        if amt_raw is not None:
            recovered_amount = float(amt_raw) / 100
        else:
            recovered_amount = float(existing.amount)

        recovered_amount = min(
            recovered_amount,
            float(existing.amount)
        )

        existing.status = "RECOVERED"
        existing.recovery_result = "RECOVERED"
        existing.recovered_amount = recovered_amount

        now = datetime.now().isoformat()

        existing.action_triggered_at = (
            existing.action_triggered_at or now
        )

        existing.recovered_at = now

        db.commit()
        db.refresh(existing)
        update_payment_link_status_in_file(existing.case_id, "RECOVERED", recovered_amount)

        return {
            "status": "recovered",
            "source": "razorpay",
            "event": event,
            "payment_id": payment_id,
            "case_id": existing.case_id,
            "recovered_amount": recovered_amount,
            "message": "Recovery case automatically marked as RECOVERED from payment link"
        }

    # =========================================
    # IGNORE OTHER EVENTS
    # =========================================

    if event != "payment.failed":
        return {
            "status": "ignored",
            "event": event,
            "reason": "Unsupported Razorpay event"
        }

    # =========================================
    # PAYMENT FAILURE
    # =========================================

    payment = (
        payload
        .get("payload", {})
        .get("payment", {})
        .get("entity", {})
    )

    payment_id = payment.get("id")
    amount = payment.get("amount")
    currency = payment.get("currency", "INR")

    failure_reason = (
        payment.get("error_description")
        or payment.get("error_reason")
        or payment.get("error_code")
        or "Unknown payment failure"
    )

    if not payment_id or amount is None:
        return {
            "status": "error",
            "message": "Payment information missing"
        }

    # Razorpay gives INR amounts in paise
    amount = float(amount) / 100

    existing = (
        db.query(RecoveryCase)
        .filter(RecoveryCase.payment_id == payment_id)
        .first()
    )

    if existing:
        return {
            "status": "already_processed",
            "payment_id": payment_id,
            "case_id": existing.case_id
        }

    case = create_case(
        db,
        payment_id,
        amount,
        currency,
        failure_reason
    )

    return {
        "status": "recovery_case_created",
        "source": "razorpay",
        "case": case_dict(case)
    }
@app.get("/recovery/cases")
def cases(db:Session=Depends(get_db)):
    rows=db.query(RecoveryCase).order_by(RecoveryCase.id.desc()).all()
    return {"total_cases":len(rows),"cases":[case_dict(c) for c in rows]}

@app.get("/recovery/stored-links")
def get_stored_links(db: Session = Depends(get_db)):
    # Cases in database that have generated payment links
    linked_cases = db.query(RecoveryCase).filter(RecoveryCase.payment_link != None).order_by(RecoveryCase.id.desc()).all()
    file_records = []
    if os.path.exists(STORED_LINKS_FILE):
        try:
            with open(STORED_LINKS_FILE, "r", encoding="utf-8") as f:
                file_records = json.load(f)
        except Exception:
            file_records = []
    return {
        "status": "success",
        "total_stored_cases": len(linked_cases),
        "total_file_records": len(file_records),
        "stored_links": [case_dict(c) for c in linked_cases],
        "file_records": file_records
    }

def sync_case_with_razorpay(case: RecoveryCase, db: Session) -> dict:
    if not case.payment_link_id:
        return {"case_id": case.case_id, "synced": False, "reason": "no_payment_link_id"}
    if case.status == "RECOVERED":
        return {"case_id": case.case_id, "synced": True, "already_recovered": True, "status": "RECOVERED"}
    if not RAZORPAY_KEY_ID or not RAZORPAY_KEY_SECRET:
        return {"case_id": case.case_id, "synced": False, "reason": "no_credentials"}

    try:
        res = requests.get(
            f"{RAZORPAY_API_BASE}/payment_links/{case.payment_link_id}",
            auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET),
            timeout=10
        )
        if not res.ok:
            return {"case_id": case.case_id, "synced": False, "error": res.text}

        link_data = res.json()
        razorpay_status = link_data.get("status")
        amount_paid_paise = link_data.get("amount_paid", 0)
        amount_paid = float(amount_paid_paise) / 100 if amount_paid_paise else 0.0

        if razorpay_status == "paid" or amount_paid >= float(case.amount):
            now = datetime.now().isoformat()
            case.status = "RECOVERED"
            case.recovery_result = "RECOVERED"
            case.recovered_amount = amount_paid if amount_paid > 0 else float(case.amount)
            case.recovered_at = now
            case.action_triggered_at = case.action_triggered_at or now
            db.commit()
            db.refresh(case)

            update_payment_link_status_in_file(case.case_id, "RECOVERED", case.recovered_amount)
            return {
                "case_id": case.case_id,
                "synced": True,
                "is_paid": True,
                "recovered_amount": case.recovered_amount,
                "status": "RECOVERED",
                "message": f"Payment verified via Razorpay! Case {case.case_id} marked as RECOVERED."
            }

        return {
            "case_id": case.case_id,
            "synced": True,
            "is_paid": False,
            "status": case.status,
            "razorpay_status": razorpay_status
        }
    except Exception as err:
        return {"case_id": case.case_id, "synced": False, "error": str(err)}

@app.post("/recovery/{case_id}/sync")
def sync_single_case(case_id: str, db: Session = Depends(get_db)):
    case = db.query(RecoveryCase).filter(RecoveryCase.case_id == case_id).first()
    if not case:
        return {"status": "error", "message": "Recovery case not found"}
    sync_res = sync_case_with_razorpay(case, db)
    return {"status": "success", "sync_result": sync_res, "case": case_dict(case)}

@app.post("/recovery/sync-pending")
def sync_all_pending(db: Session = Depends(get_db)):
    active_cases = (
        db.query(RecoveryCase)
        .filter(
            RecoveryCase.status.in_(["ACTION_TRIGGERED", "UNDER_REVIEW", "PENDING"]),
            RecoveryCase.payment_link_id != None
        )
        .all()
    )
    results = []
    newly_recovered = []
    for c in active_cases:
        r = sync_case_with_razorpay(c, db)
        results.append(r)
        if r.get("is_paid"):
            newly_recovered.append(r)

    return {
        "status": "success",
        "total_checked": len(active_cases),
        "newly_recovered_count": len(newly_recovered),
        "newly_recovered": newly_recovered,
        "results": results
    }

@app.post("/recovery/{case_id}/action")
def execute(case_id: str, db: Session = Depends(get_db)):
    case = (
        db.query(RecoveryCase)
        .filter(RecoveryCase.case_id == case_id)
        .first()
    )

    if not case:
        return {
            "status": "error",
            "message": "Recovery case not found"
        }

    if case.status != "PENDING":
        return {
            "status": "already_processed",
            "case_id": case.case_id,
            "current_status": case.status
        }

    previous = case.status

    # -----------------------------------------
    # MANUAL REVIEW
    # -----------------------------------------

    if case.recommended_action == "MANUAL_REVIEW":

        case.status = "UNDER_REVIEW"
        case.action_triggered_at = datetime.now().isoformat()

        db.commit()
        db.refresh(case)

        return {
            "status": "action_executed",
            "action_type": "MANUAL_REVIEW",
            "case": {
                **case_dict(case),
                "previous_status": previous,
                "message": "Case assigned for manual merchant review."
            }
        }

    # -----------------------------------------
    # RAZORPAY RECOVERY PAYMENT LINK
    # -----------------------------------------

    if case.recommended_action in {
        "RETRY_PAYMENT",
        "ALTERNATE_PAYMENT"
    }:

        try:
            razorpay_link = create_razorpay_recovery_link(case)

        except Exception as error:

            print(
                f"Razorpay recovery action failed for "
                f"{case.case_id}: {error}"
            )

            return {
                "status": "error",
                "message": "Failed to create Razorpay recovery payment link",
                "case_id": case.case_id,
                "details": str(error)
            }

        pl_id = razorpay_link.get("id")
        pl_url = razorpay_link.get("short_url") or razorpay_link.get("url")

        case.payment_link = pl_url
        case.payment_link_id = pl_id
        case.status = "ACTION_TRIGGERED"
        case.action_triggered_at = datetime.now().isoformat()

        db.commit()
        db.refresh(case)

        # Store in persistent server JSON audit log
        save_payment_link_to_file({
            "case_id": case.case_id,
            "payment_id": case.payment_id,
            "amount": float(case.amount),
            "currency": case.currency,
            "payment_link": pl_url,
            "payment_link_id": pl_id,
            "status": case.status,
            "failure_reason": case.failure_reason,
            "recommended_action": case.recommended_action,
            "action_triggered_at": case.action_triggered_at
        })

        return {
            "status": "action_executed",
            "action_type": case.recommended_action,
            "razorpay": {
                "payment_link_id": pl_id,
                "payment_link": pl_url,
                "status": razorpay_link.get("status")
            },
            "stored": {
                "database": True,
                "file_log": True,
                "case_id": case.case_id,
                "payment_link": pl_url,
                "payment_link_id": pl_id
            },
            "case": {
                **case_dict(case),
                "previous_status": previous,
                "message": (
                    "Razorpay Test Mode recovery payment link "
                    "created and stored successfully."
                )
            }
        }

    return {
        "status": "error",
        "message": "Unknown recovery action"
    }
@app.post("/recovery/{case_id}/result")
def result(case_id:str,data:RecoveryResult,db:Session=Depends(get_db)):
    case=db.query(RecoveryCase).filter(RecoveryCase.case_id==case_id).first()
    if not case:return {"status":"error","message":"Recovery case not found"}
    outcome=data.result.strip().upper()
    if outcome not in {"RECOVERED","FAILED","EXPIRED"}: return {"status":"error","message":"Invalid recovery result","allowed_results":["RECOVERED","FAILED","EXPIRED"]}
    case.recovery_result=outcome; case.recovered_amount=min(data.recovered_amount,case.amount) if outcome=="RECOVERED" else 0; case.status=outcome
    now=datetime.now().isoformat(); case.action_triggered_at=case.action_triggered_at or now; case.recovered_at=now
    db.commit();db.refresh(case)
    update_payment_link_status_in_file(case.case_id, outcome, case.recovered_amount)
    return {"status":"recovery_result_recorded","case":case_dict(case)}

def normalize_failure_reason(reason):
    if not reason:
        return "Unknown"

    key = " ".join(str(reason).strip().lower().split())

    mapping = {
        "insufficient_funds": "Insufficient funds",
        "insufficient funds": "Insufficient funds",
        "insufficient balance": "Insufficient funds",

        "network_error": "Network error",
        "network error": "Network error",
        "network timeout": "Network timeout",
        "timeout": "Network timeout",

        "card_declined": "Card declined",
        "payment declined": "Card declined",
        "declined": "Card declined",

        "bank server error": "Bank server error",
        "bank error": "Bank server error",

        "authentication": "Authentication failure",
        "authentication failure": "Authentication failure",

        "expired": "Payment method expired",
    }

    return mapping.get(key, str(reason).strip().title())



def metrics(cases):
    # Cases that are still candidates for recovery
    active = [
        c for c in cases
        if c.status in {"PENDING", "ACTION_TRIGGERED", "UNDER_REVIEW"}
    ]

    # Successfully recovered cases
    recovered_cases = [
        c for c in cases
        if c.status == "RECOVERED"
    ]

    # -----------------------------
    # Core revenue metrics
    # -----------------------------

    revenue_at_risk = sum(
        float(c.amount or 0)
        for c in active
    )

    expected_recovery = sum(
        float(c.expected_recovery or 0)
        for c in active
    )

    recovered_revenue = sum(
        float(c.recovered_amount or 0)
        for c in recovered_cases
    )

    total_revenue_tracked = revenue_at_risk + recovered_revenue

    recovery_rate = (
        round(
            recovered_revenue / total_revenue_tracked * 100,
            2
        )
        if total_revenue_tracked > 0
        else 0
    )

    # -----------------------------
    # Failure breakdown
    # Only active cases are shown
    # -----------------------------

    breakdown = {}

    for c in active:
        reason = normalize_failure_reason(c.failure_reason)

        if reason not in breakdown:
            breakdown[reason] = {
                "cases": 0,
                "amount": 0,
                "expected_recovery": 0
            }

        breakdown[reason]["cases"] += 1
        breakdown[reason]["amount"] += float(c.amount or 0)
        breakdown[reason]["expected_recovery"] += float(
            c.expected_recovery or 0
        )

    # Round breakdown values
    for reason in breakdown:
        breakdown[reason]["amount"] = round(
            breakdown[reason]["amount"], 2
        )

        breakdown[reason]["expected_recovery"] = round(
            breakdown[reason]["expected_recovery"], 2
        )

    # -----------------------------
    # Top failure reason
    # Based on revenue currently at risk
    # -----------------------------

    if breakdown:
        top_failure_reason = max(
            breakdown,
            key=lambda reason: breakdown[reason]["amount"]
        )
    else:
        top_failure_reason = "None"

    return {
        "revenue_at_risk": round(revenue_at_risk, 2),

        "expected_recovery": round(expected_recovery, 2),

        "recovered_revenue": round(recovered_revenue, 2),

        "total_cases": len(cases),

        "failed_cases": len(active),

        "recovered_cases": len(recovered_cases),

        "recovery_rate": recovery_rate,

        "top_failure_reason": top_failure_reason,

        "failure_breakdown": breakdown
    }
@app.get("/merchant/insights")
def merchant(db:Session=Depends(get_db)):
    rows=db.query(RecoveryCase).all()
    if not rows:return {"status":"no_data","message":"No recovery cases available"}
    m=metrics(rows)
    return {"status":"success","metrics":m,"ai_insights":generate_merchant_insights(m)}

@app.get("/ai/insights")
def ai(db:Session=Depends(get_db)):
    rows=db.query(RecoveryCase).all()
    if not rows:return {"status":"no_data","message":"No recovery cases available"}
    m=metrics(rows)
    return {"status":"success","metrics":m,"insights":generate_merchant_insights(m)}



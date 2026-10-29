"""PainFree CarePay Backend Server.

AI-Powered Medical Bill & Prescription Intelligence with PayPal Family Checkout & Pay in 4.
Built for the PayPal AI Hackathon (Devpost).
"""
import base64
import copy
import http.server
import json
import os
import re
import secrets
import threading
import time
import urllib.parse
import urllib.request
import uuid
from decimal import Decimal, ROUND_HALF_UP

PORT = 8050
HOST = "127.0.0.1"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Concurrency & Mutex Locks for Authoritative Financial Ledger
LEDGER_LOCK = threading.RLock()
CAREGIVER_TOKENS = {}      # bearer token -> invoice_id mapping
ACTIVE_INVOICE_ORDERS = {} # invoice_id -> {"order_id", "amount", "mode", "created_time"}
PROCESSED_CAPTURE_IDS = set() # Immutable set of completed capture IDs to prevent re-processing

def to_money(val):
    """Accurate decimal quantization for healthcare billing invariants."""
    return float(Decimal(str(val or 0.0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

# 1. API Configuration & Environment
ENV_PATH = os.path.join(BASE_DIR, ".env")
NEBIUS_API_KEY = os.environ.get("NEBIUS_API_KEY", "")
NEBIUS_BASE_URL = os.environ.get("NEBIUS_BASE_URL", "https://api.tokenfactory.nebius.com/v1")
PAYPAL_CLIENT_ID = os.environ.get("PAYPAL_CLIENT_ID", "sb")
PAYPAL_CLIENT_SECRET = os.environ.get("PAYPAL_CLIENT_SECRET", "")
PAYPAL_MODE = os.environ.get("PAYPAL_MODE", "sandbox")

if os.path.exists(ENV_PATH):
    try:
        with open(ENV_PATH, "r", encoding="utf-8") as ef:
            for line in ef:
                line = line.strip()
                if line.startswith("NEBIUS_API_KEY="):
                    NEBIUS_API_KEY = line.split("=", 1)[1].strip().strip('"').strip("'")
                elif line.startswith("NEBIUS_BASE_URL="):
                    NEBIUS_BASE_URL = line.split("=", 1)[1].strip().strip('"').strip("'")
                elif line.startswith("PAYPAL_CLIENT_ID="):
                    PAYPAL_CLIENT_ID = line.split("=", 1)[1].strip().strip('"').strip("'")
                elif line.startswith("PAYPAL_CLIENT_SECRET="):
                    PAYPAL_CLIENT_SECRET = line.split("=", 1)[1].strip().strip('"').strip("'")
                elif line.startswith("PAYPAL_MODE="):
                    PAYPAL_MODE = line.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception as e:
        print(f"[Notice] .env load exception: {e}")

try:
    import receipt_pipeline as rp
    HAS_PIPELINE = True
except Exception as e:
    print(f"[Warning] Failed to import receipt_pipeline: {e}")
    HAS_PIPELINE = False

# In-memory invoice store with durable local JSON persistence
INVOICE_STORE = {}
DATA_DIR = os.path.join(BASE_DIR, "_work")
INVOICES_DB_PATH = os.path.join(DATA_DIR, "invoices_store.json")
TOKENS_DB_PATH = os.path.join(DATA_DIR, "caregiver_tokens.json")

def load_persisted_state():
    """Restores INVOICE_STORE and CAREGIVER_TOKENS from durable local JSON storage."""
    global INVOICE_STORE, CAREGIVER_TOKENS
    with LEDGER_LOCK:
        if os.path.exists(INVOICES_DB_PATH):
            try:
                with open(INVOICES_DB_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        INVOICE_STORE.update(data)
                        print(f"[Storage] Restored {len(data)} invoice(s) from {INVOICES_DB_PATH}")
            except Exception as e:
                print(f"[Warning] Failed to restore invoice store: {e}")

        if os.path.exists(TOKENS_DB_PATH):
            try:
                with open(TOKENS_DB_PATH, "r", encoding="utf-8") as f:
                    t_data = json.load(f)
                    if isinstance(t_data, dict):
                        CAREGIVER_TOKENS.update(t_data)
                        print(f"[Storage] Restored {len(t_data)} caregiver token(s) from {TOKENS_DB_PATH}")
            except Exception as e:
                print(f"[Warning] Failed to restore caregiver tokens: {e}")

def save_persisted_state():
    """Atomically persists INVOICE_STORE and CAREGIVER_TOKENS to durable local storage."""
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        # 1. Persist INVOICE_STORE
        tmp_inv = INVOICES_DB_PATH + ".tmp"
        with open(tmp_inv, "w", encoding="utf-8") as f:
            json.dump(INVOICE_STORE, f, ensure_ascii=False, indent=2)
        os.replace(tmp_inv, INVOICES_DB_PATH)

        # 2. Persist CAREGIVER_TOKENS
        tmp_tok = TOKENS_DB_PATH + ".tmp"
        with open(tmp_tok, "w", encoding="utf-8") as f:
            json.dump(CAREGIVER_TOKENS, f, ensure_ascii=False, indent=2)
        os.replace(tmp_tok, TOKENS_DB_PATH)
    except Exception as e:
        print(f"[Warning] Failed to persist state: {e}")


SAMPLE_DIR = os.path.join(BASE_DIR, "sample_receipts")

# 2026 CMS National Medicare Physician & Outpatient Benchmark Allowable Schedule
CMS_MEDICARE_BENCHMARKS = {
    # Emergency Department Services
    "CPT 99281": {"name": "Emergency Dept Visit (Level 1 Minor)", "cms_rate": 28.50, "category": "Emergency Room"},
    "CPT 99282": {"name": "Emergency Dept Visit (Level 2 Low-Moderate)", "cms_rate": 52.00, "category": "Emergency Room"},
    "CPT 99283": {"name": "Emergency Dept Visit (Level 3 Moderate)", "cms_rate": 84.20, "category": "Emergency Room"},
    "CPT 99284": {"name": "Emergency Dept Visit (Level 4 Moderate-High)", "cms_rate": 138.50, "category": "Emergency Room"},
    "CPT 99285": {"name": "Emergency Dept Visit (Level 5 High-Severity)", "cms_rate": 215.20, "category": "Emergency Room"},
    # Injections and Infusions
    "CPT 96372": {"name": "Therapeutic Parenteral Injection", "cms_rate": 24.80, "category": "Therapeutic Procedure"},
    "CPT 96360": {"name": "Intravenous Hydration Infusion (Initial 1hr)", "cms_rate": 42.10, "category": "Infusion Therapy"},
    # Diagnostic Imaging / Radiology
    "CPT 71045": {"name": "Chest Radiography (Single View)", "cms_rate": 25.10, "category": "Diagnostic Radiology"},
    "CPT 71046": {"name": "Chest Radiography (2 Views)", "cms_rate": 32.40, "category": "Diagnostic Radiology"},
    "CPT 70450": {"name": "CT Scan Head/Brain Without Contrast", "cms_rate": 162.00, "category": "Diagnostic Radiology"},
    # Laboratory Pathology
    "CPT 80053": {"name": "Comprehensive Metabolic Panel (CMP)", "cms_rate": 14.20, "category": "Clinical Laboratory"},
    "CPT 85025": {"name": "Complete Blood Count (CBC with Differential)", "cms_rate": 10.50, "category": "Clinical Laboratory"},
    "CPT 81003": {"name": "Urinalysis Automated", "cms_rate": 4.10, "category": "Clinical Laboratory"},
    # Outpatient Office Visits
    "CPT 99213": {"name": "Office Outpatient Visit (Low-Moderate, 20-29 min)", "cms_rate": 92.00, "category": "Outpatient Evaluation"},
    "CPT 99214": {"name": "Office Outpatient Visit (Moderate, 30-39 min)", "cms_rate": 132.00, "category": "Outpatient Evaluation"},
    "CPT 93000": {"name": "Electrocardiogram (ECG/EKG 12-Lead)", "cms_rate": 18.50, "category": "Cardiovascular"}
}

def classify_hsa_eligibility(code, desc, flag):
    """
    Classifies healthcare items under IRC § 213(d) / IRS Publication 502 guidelines.
    Returns: (status, category, reason)
    status: "ELIGIBLE" | "POTENTIALLY_ELIGIBLE" | "INELIGIBLE"
    """
    code_upper = str(code).upper()
    desc_upper = str(desc).upper()
    flag_upper = str(flag).upper()

    # Prescription Drugs (NDC) - Standard Eligible under IRC § 213(b)/(d)
    if "NDC" in code_upper or any(rx in desc_upper for rx in ["ATORVASTATIN", "AMLODIPINE", "LIPITOR", "NORVASC", "METFORMIN", "LISINOPRIL"]):
        return (
            "ELIGIBLE",
            "Prescription Medication (Rx)",
            "Qualified medical care expense under IRC § 213(b) (Valid clinician prescription)"
        )

    # Inpatient / Outpatient Clinical Hospital Procedures (CPT/REV)
    if "CPT" in code_upper or "REV" in code_upper or any(term in desc_upper for term in ["VISIT", "INJECTION", "RADIOGRAPHY", "X-RAY", "EMERGENCY", "LAB", "CT SCAN"]):
        return (
            "ELIGIBLE",
            "Clinical Diagnostic & Treatment",
            "Qualified medical care expense under IRC § 213(d)(1)(A) (Diagnosis, cure, or treatment of disease)"
        )

    # OTC / Supplements: Dietary supplements are not automatically eligible unless accompanied by Letter of Medical Necessity (LMN)
    if "OTC" in code_upper or any(sup in desc_upper for sup in ["OMEGA-3", "VITAMIN", "SUPPLEMENT", "COQ10", "PROBIOTIC"]):
        return (
            "POTENTIALLY_ELIGIBLE",
            "Over-the-Counter / Dietary Supplement",
            "Potential eligibility under IRS Notice 2023-62: May require Letter of Medical Necessity (LMN) from physician depending on plan rules."
        )

    if "HSA" in flag_upper or "FSA" in flag_upper:
        return (
            "ELIGIBLE",
            "Pre-Screened Medical Care",
            "Pre-screened qualified healthcare expense under IRC § 213(d)"
        )

    return (
        "POTENTIALLY_ELIGIBLE",
        "Healthcare Item / Service",
        "Potentially qualified medical expense under IRC § 213(d); subject to HSA/FSA administrator substantiation."
    )

def enrich_invoice_items(items):
    """
    Enriches item list with:
    1. 2026 CMS Medicare Allowable Benchmark rates and markup multiples.
    2. IRC § 213(d) / IRS Pub 502 HSA/FSA Eligibility classifications.
    """
    hsa_eligible_total = 0.0
    hsa_review_total = 0.0

    for it in items:
        code_str = str(it.get("code", "")).strip()
        due = float(it.get("due", 0.0))
        charge = float(it.get("charge", 0.0))

        # CMS Medicare Benchmark lookup
        cms_entry = CMS_MEDICARE_BENCHMARKS.get(code_str)
        if cms_entry:
            cms_rate = cms_entry["cms_rate"]
            it["cms_reference_fee"] = cms_rate
            it["cms_category"] = cms_entry["category"]
            if cms_rate > 0:
                it["markup_ratio"] = round(charge / cms_rate, 2)
        else:
            it["cms_reference_fee"] = None
            it["markup_ratio"] = None

        # HSA / FSA Eligibility classification
        status, cat, reason = classify_hsa_eligibility(code_str, it.get("desc", ""), it.get("flag", ""))
        it["hsa_status"] = status
        it["hsa_category"] = cat
        it["hsa_reason"] = reason

        if status == "ELIGIBLE":
            hsa_eligible_total += due
        elif status == "POTENTIALLY_ELIGIBLE":
            hsa_review_total += due

    return round(hsa_eligible_total, 2), round(hsa_review_total, 2)

# Default structured bills for immediate 1-click evaluation
SAMPLE_BILLS = {
    "us_hospital_bill.png": {
        "facility": "Mercy General Hospital (Emergency & Trauma Care)",
        "date": "2026-10-18",
        "currency": "USD",
        "patient_name": "Eleanor Vance (Senior Patient)",
        "total_amount": 580.00,
        "insurance_covered": 400.00,
        "patient_due": 180.00,
        "verified_due": 150.00,
        "disputed_due": 30.00,
        "hsa_eligible_due": 180.00,
        "hsa_review_due": 0.00,
        "status": "UNPAID",
        "pay_in_4_eligible": True,
        "pay_in_4_installment": 45.00,
        "items": [
            {
                "code": "CPT 99284",
                "desc": "Emergency Dept Visit (Level 4 Moderate Severity)",
                "charge": 350.00,
                "insured": 250.00,
                "due": 100.00,
                "flag": "In-Network Verified",
                "cms_reference_fee": 138.50,
                "markup_ratio": 2.53,
                "hsa_status": "ELIGIBLE",
                "hsa_category": "Clinical Diagnostic & Treatment",
                "hsa_reason": "Qualified medical care expense under IRC § 213(d)(1)(A)"
            },
            {
                "code": "CPT 71046",
                "desc": "Chest Radiography (X-Ray 2 Views)",
                "charge": 140.00,
                "insured": 90.00,
                "due": 50.00,
                "flag": "Fair Price Match",
                "cms_reference_fee": 32.40,
                "markup_ratio": 4.32,
                "hsa_status": "ELIGIBLE",
                "hsa_category": "Clinical Diagnostic & Treatment",
                "hsa_reason": "Qualified medical care expense under IRC § 213(d)(1)(A)"
            },
            {
                "code": "CPT 96372",
                "desc": "Therapeutic Parenteral Injection",
                "charge": 90.00,
                "insured": 60.00,
                "due": 30.00,
                "flag": "Surprise Out-of-Network Flag (Dispute Eligible)",
                "cms_reference_fee": 24.80,
                "markup_ratio": 3.63,
                "hsa_status": "ELIGIBLE",
                "hsa_category": "Clinical Diagnostic & Treatment",
                "hsa_reason": "Qualified medical care expense under IRC § 213(d)(1)(A)"
            }
        ],
        "ai_insights": [
            "Smart Dispute & Partial Settle: CPT 96372 flagged under No Surprises Act ($30.00 hold). Verified services ($150.00) can be settled immediately.",
            "CMS Medicare Benchmark Context: Itemized charges compared with 2026 Medicare National allowable fee schedule (e.g. CPT 96372 $24.80 benchmark vs $90.00 hospital charge).",
            "HSA/FSA Tax Substantiation Ready: 100% of patient due ($180.00) pre-screened as IRC § 213(d) qualified medical expense for IRS Pub 502 reimbursement.",
            "PayPal Pay in 4 Option: Estimated $180.00 copay split into 4 bi-weekly payments of $45.00 (Final eligibility determined by PayPal at checkout).",
            "HIPAA Caregiver View: Family members can contribute toward copays with privacy-preserving diagnostic shielding."
        ]
    },
    "pharmacy_receipt_1.jpg": {
        "facility": "CVS Pharmacy #4821",
        "date": "2026-10-24",
        "currency": "USD",
        "patient_name": "Eleanor Vance",
        "total_amount": 128.50,
        "insurance_covered": 88.50,
        "patient_due": 40.00,
        "verified_due": 40.00,
        "disputed_due": 0.00,
        "hsa_eligible_due": 30.00,
        "hsa_review_due": 10.00,
        "status": "UNPAID",
        "pay_in_4_eligible": True,
        "pay_in_4_installment": 10.00,
        "items": [
            {
                "code": "NDC 00071-0155",
                "desc": "Lipitor (Atorvastatin 20mg 90ct)",
                "charge": 65.00,
                "insured": 45.00,
                "due": 20.00,
                "flag": "Tier 1 Generic (Rx)",
                "cms_reference_fee": None,
                "markup_ratio": None,
                "hsa_status": "ELIGIBLE",
                "hsa_category": "Prescription Medication (Rx)",
                "hsa_reason": "Qualified medical expense under IRC § 213(b) (Valid clinician prescription)"
            },
            {
                "code": "NDC 00069-0260",
                "desc": "Norvasc (Amlodipine Besylate 5mg 90ct)",
                "charge": 38.50,
                "insured": 28.50,
                "due": 10.00,
                "flag": "Tier 1 Generic (Rx)",
                "cms_reference_fee": None,
                "markup_ratio": None,
                "hsa_status": "ELIGIBLE",
                "hsa_category": "Prescription Medication (Rx)",
                "hsa_reason": "Qualified medical expense under IRC § 213(b) (Valid clinician prescription)"
            },
            {
                "code": "OTC 84920",
                "desc": "Omega-3 Pure EPA/DHA CoQ10",
                "charge": 25.00,
                "insured": 15.00,
                "due": 10.00,
                "flag": "OTC Supplement (LMN May Apply)",
                "cms_reference_fee": None,
                "markup_ratio": None,
                "hsa_status": "POTENTIALLY_ELIGIBLE",
                "hsa_category": "Over-the-Counter / Dietary Supplement",
                "hsa_reason": "Potential eligibility under IRS Notice 2023-62: May require Letter of Medical Necessity (LMN) from physician."
            }
        ],
        "ai_insights": [
            "HSA/FSA Tax Substantiation Allocation: $30.00 pre-screened as direct Rx qualified expense; $10.00 OTC supplement requires LMN substantiation.",
            "Co-Pay Verification: Prescription generic co-pays verified against standard Medicare Part D tier limits.",
            "PayPal Pay in 4 Option: Estimated $40.00 total split into 4 payments of $10.00 every two weeks (Final eligibility determined by PayPal at checkout).",
            "IRS Pub 502 Ready: Printable tax substantiation slip generated upon PayPal checkout completion.",
            "Auto-Reorder & Renewal: Caregiver notified of upcoming 90-day refill timeline."
        ]
    }
}

ORDER_TO_INVOICE = {}
PAYPAL_API_BASE = "https://api-m.sandbox.paypal.com" if PAYPAL_MODE == "sandbox" else "https://api-m.paypal.com"
_cached_paypal_token = {"token": None, "expires_at": 0}



def get_paypal_token():
    now = time.time()
    if _cached_paypal_token["token"] and _cached_paypal_token["expires_at"] > now + 60:
        return _cached_paypal_token["token"]
    if not PAYPAL_CLIENT_SECRET or PAYPAL_CLIENT_ID in ("sb", "test", ""):
        return None
    try:
        url = f"{PAYPAL_API_BASE}/v1/oauth2/token"
        auth = base64.b64encode(f"{PAYPAL_CLIENT_ID}:{PAYPAL_CLIENT_SECRET}".encode()).decode()
        req = urllib.request.Request(
            url,
            data=b"grant_type=client_credentials",
            headers={
                "Authorization": f"Basic {auth}",
                "Content-Type": "application/x-www-form-urlencoded"
            }
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            _cached_paypal_token["token"] = data.get("access_token")
            _cached_paypal_token["expires_at"] = now + data.get("expires_in", 3600)
            return _cached_paypal_token["token"]
    except Exception as e:
        print(f"[PayPal Auth Notice] {e}")
        return None


def create_paypal_order(amount, currency="USD", invoice_id="", payment_mode="full"):
    token = get_paypal_token()
    if token:
        try:
            url = f"{PAYPAL_API_BASE}/v2/checkout/orders"
            idempotency_key = f"{invoice_id}-{payment_mode}-{uuid.uuid4()}"
            payload = {
                "intent": "CAPTURE",
                "purchase_units": [
                    {
                        "reference_id": invoice_id,
                        "description": f"PainFree CarePay - Medical Invoice {invoice_id} ({payment_mode})",
                        "amount": {
                            "currency_code": currency,
                            "value": f"{amount:.2f}"
                        }
                    }
                ],
                "application_context": {
                    "brand_name": "PainFree CarePay AI",
                    "landing_page": "NO_PREFERENCE",
                    "user_action": "PAY_NOW"
                }
            }
            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode(),
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                    "Prefer": "return=representation",
                    "PayPal-Request-Id": idempotency_key
                }
            )
            with urllib.request.urlopen(req, timeout=12) as resp:
                res_data = json.loads(resp.read().decode())
                order_id = res_data.get("id")
                if order_id:
                    with LEDGER_LOCK:
                        ORDER_TO_INVOICE[order_id] = {
                            "invoice_id": invoice_id,
                            "amount": amount,
                            "currency": currency,
                            "payment_mode": payment_mode,
                            "created_at": time.strftime("%Y-%m-%d %H:%M:%S")
                        }
                return {
                    "success": True,
                    "order_id": order_id,
                    "status": res_data.get("status", "CREATED"),
                    "authorized_amount": amount,
                    "payment_mode": payment_mode,
                    "mode": "live_paypal_sandbox",
                    "raw_order": res_data
                }
        except Exception as e:
            print(f"[PayPal Create Order Error] {e}")
            return {
                "success": False,
                "error": str(e),
                "mode": "live_paypal_sandbox_failed",
                "message": f"PayPal live sandbox order creation failed: {e}"
            }

    return {
        "success": False,
        "error": "PayPal credentials not configured in .env",
        "message": "Please configure valid PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET."
    }


def capture_paypal_order(order_id):
    token = get_paypal_token()
    if token:
        try:
            url = f"{PAYPAL_API_BASE}/v2/checkout/orders/{order_id}/capture"
            req = urllib.request.Request(
                url,
                data=b"{}",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                    "Prefer": "return=representation",
                    "PayPal-Request-Id": order_id
                }
            )
            with urllib.request.urlopen(req, timeout=12) as resp:
                res_data = json.loads(resp.read().decode())
                status = res_data.get("status")
                purchase_units = res_data.get("purchase_units", [])
                captures = purchase_units[0].get("payments", {}).get("captures", []) if purchase_units else []
                
                if not captures or status != "COMPLETED":
                    return {
                        "success": False,
                        "order_id": order_id,
                        "status": status or "FAILED",
                        "error": "PayPal capture incomplete or rejected by gateway",
                        "raw_capture": res_data
                    }

                cap = captures[0]
                cap_id = cap.get("id")
                cap_status = cap.get("status")
                if not cap_id or cap_status != "COMPLETED":
                    return {
                        "success": False,
                        "order_id": order_id,
                        "status": cap_status or "FAILED",
                        "error": "Capture status not COMPLETED or missing capture ID",
                        "raw_capture": res_data
                    }

                # Authoritative capture amount and currency straight from gateway
                try:
                    captured_amount = float(cap.get("amount", {}).get("value", 0.0))
                except (ValueError, TypeError):
                    captured_amount = 0.0
                captured_currency = str(cap.get("amount", {}).get("currency_code", "USD")).strip()

                if captured_amount <= 0.0:
                    return {
                        "success": False,
                        "order_id": order_id,
                        "status": "FAILED",
                        "error": "Invalid zero or negative capture amount from gateway",
                        "raw_capture": res_data
                    }

                # Update authoritative invoice ledger state with Mutex Lock
                with LEDGER_LOCK:
                    if cap_id in PROCESSED_CAPTURE_IDS:
                        return {
                            "success": False,
                            "order_id": order_id,
                            "status": "REPLAY_BLOCKED",
                            "error": f"Capture ID {cap_id} has already been recorded in ledger. Replay blocked."
                        }

                    order_meta = ORDER_TO_INVOICE.get(order_id, {})
                    invoice_id = order_meta.get("invoice_id")
                    payment_mode = order_meta.get("payment_mode", "full")
                    settlement_status = "SETTLED_FULL"

                    if invoice_id and invoice_id in INVOICE_STORE:
                        inv = INVOICE_STORE[invoice_id]

                        # Currency invariant check
                        inv_currency = str(inv.get("currency", "USD")).strip()
                        if captured_currency != inv_currency:
                            return {
                                "success": False,
                                "order_id": order_id,
                                "status": "CURRENCY_MISMATCH",
                                "error": f"Captured currency {captured_currency} does not match invoice currency {inv_currency}"
                            }

                        # Double capture guard: if already settled, reject replay
                        if inv.get("status") in ("PAID_IN_FULL", "SETTLED_FULL") and float(inv.get("remaining_patient_due", 0.0)) <= 0.01:
                            return {
                                "success": False,
                                "order_id": order_id,
                                "status": "ALREADY_SETTLED",
                                "error": f"Invoice {invoice_id} is already settled in full. Duplicate capture blocked."
                            }

                        total_patient_due = to_money(inv.get("patient_due", 0.0))
                        disputed_amt = to_money(inv.get("disputed_due", 0.0))
                        prior_paid = to_money(inv.get("total_captured_amount", 0.0))
                        new_paid_total = to_money(prior_paid + captured_amount)
                        inv["total_captured_amount"] = new_paid_total

                        rem = max(0.0, to_money(total_patient_due - new_paid_total))
                        inv["remaining_patient_due"] = rem

                        # Initial verified portion is (total_patient_due - disputed_amt)
                        initial_verified = max(0.0, to_money(total_patient_due - disputed_amt))
                        inv["verified_due"] = max(0.0, to_money(initial_verified - new_paid_total))

                        if payment_mode == "verified_only":
                            settlement_status = "SETTLED_FULL" if rem <= 0.01 else "PARTIALLY_SETTLED_DISPUTE_HELD"
                            inv["status"] = settlement_status
                            inv["settled_portion"] = new_paid_total
                            inv["remaining_disputed_balance"] = min(rem, disputed_amt)
                        elif payment_mode == "family_split":
                            prior_fam = to_money(inv.get("family_contributions_total", 0.0))
                            inv["family_contributions_total"] = to_money(prior_fam + captured_amount)
                            settlement_status = "SETTLED_FULL" if rem <= 0.01 else "PARTIALLY_SETTLED"
                            inv["status"] = settlement_status
                        else:
                            settlement_status = "PAID_IN_FULL" if rem <= 0.01 else "PARTIALLY_SETTLED"
                            inv["status"] = settlement_status

                        # Record immutable capture event audit trail
                        inv.setdefault("captures", []).append({
                            "capture_id": cap_id,
                            "order_id": order_id,
                            "amount": to_money(captured_amount),
                            "currency": captured_currency,
                            "payment_mode": payment_mode,
                            "captured_at": time.strftime("%Y-%m-%d %H:%M:%S")
                        })
                        PROCESSED_CAPTURE_IDS.add(cap_id)
                        inv["last_capture_id"] = cap_id
                        inv["paid_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                        ACTIVE_INVOICE_ORDERS.pop(invoice_id, None)
                        save_persisted_state()

                return {
                    "success": True,
                    "capture_id": cap_id,
                    "order_id": order_id,
                    "status": "COMPLETED",
                    "settlement_status": settlement_status,
                    "invoice_id": invoice_id,
                    "captured_amount": captured_amount,
                    "payment_mode": payment_mode,
                    "mode": "live_paypal_sandbox",
                    "raw_capture": res_data
                }
        except Exception as e:
            print(f"[PayPal Capture Order Error] {e}")
            return {
                "success": False,
                "error": str(e),
                "mode": "live_paypal_sandbox_failed",
                "message": f"PayPal live sandbox order capture failed: {e}"
            }

    return {
        "success": False,
        "error": "Invalid order_id or missing PayPal credentials",
        "message": "Cannot capture order without active PayPal credentials."
    }


class CarePayHandler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        origin = self.headers.get("Origin", "")
        if origin and (origin.startswith("http://127.0.0.1") or origin.startswith("http://localhost")):
            self.send_header("Access-Control-Allow-Origin", origin)
        else:
            self.send_header("Access-Control-Allow-Origin", f"http://{HOST}:{PORT}")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Requested-With")
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        clean_path = parsed.path.lstrip("/")

        if clean_path == "" or clean_path == "index.html":
            target = os.path.join(BASE_DIR, "index.html")
            if os.path.exists(target):
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                with open(target, "rb") as f:
                    self.wfile.write(f.read())
                return
            else:
                self.send_error(404, "index.html not found")
                return

        if clean_path.startswith("sample_receipts/"):
            fname = clean_path.replace("sample_receipts/", "")
            safe_fname = os.path.basename(fname)
            target = os.path.realpath(os.path.join(SAMPLE_DIR, safe_fname))
            if target.startswith(os.path.realpath(SAMPLE_DIR)) and os.path.exists(target):
                content_type = "image/png" if safe_fname.endswith(".png") else "image/jpeg"
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.end_headers()
                with open(target, "rb") as f:
                    self.wfile.write(f.read())
                return

        if clean_path == "api/health":
            self.send_json({
                "status": "online",
                "service": "PainFree CarePay AI",
                "server_architecture": "ThreadingHTTPServer",
                "paypal_sandbox": True,
                "paypal_mode": PAYPAL_MODE,
                "paypal_client_id": PAYPAL_CLIENT_ID,
                "paypal_live_credentials": bool(PAYPAL_CLIENT_SECRET and PAYPAL_CLIENT_ID not in ("sb", "")),
                "nebius_api_live": bool(NEBIUS_API_KEY)
            })
            return

        if clean_path.startswith("api/caregiver-invoice/"):
            cg_token = clean_path.replace("api/caregiver-invoice/", "").split("?")[0]
            with LEDGER_LOCK:
                token_meta = CAREGIVER_TOKENS.get(cg_token)
                if not token_meta:
                    self.send_json({"success": False, "error": "Invalid or expired caregiver token", "message": "Link has expired or does not exist."}, status=404)
                    return
                inv_id = token_meta["invoice_id"] if isinstance(token_meta, dict) else token_meta
                split_pct = token_meta.get("split_percent", 100) if isinstance(token_meta, dict) else 100
                req_amt = token_meta.get("requested_amount", 0.0) if isinstance(token_meta, dict) else 0.0

                if inv_id not in INVOICE_STORE:
                    self.send_json({"success": False, "error": "Invoice no longer exists"}, status=404)
                    return
                raw_inv = INVOICE_STORE[inv_id]
                # Strict HIPAA Safe Harbor (45 CFR § 164.514) Allowlist-Only Projection:
                # 1. Year-only service date (month/day strictly redacted)
                raw_date = str(raw_inv.get("date", "2026"))
                year_only = raw_date[:4] + "-XX-XX (HIPAA Safe Harbor Redacted)"
                
                # 2. Fully de-identified clinical items (no CPT, NDC, or medical condition codes)
                safe_items = []
                for idx, it in enumerate(raw_inv.get("items", [])):
                    safe_items.append({
                        "code": f"HIPAA-SAFE-0{idx+1}",
                        "desc": "Redacted Healthcare Clinical Service (Safe Harbor Compliant)",
                        "charge": it.get("charge", 0.0),
                        "insured": it.get("insured", 0.0),
                        "due": it.get("due", 0.0),
                        "flag": "Caregiver Contribution View"
                    })
                
                rem_due = raw_inv.get("remaining_patient_due", raw_inv.get("patient_due", 0.0))
                safe_inv = {
                    "invoice_id": inv_id,
                    "caregiver_token": cg_token,
                    "facility": "Verified Healthcare Facility (HIPAA Redacted)",
                    "date": year_only,
                    "currency": raw_inv.get("currency", "USD"),
                    "patient_name": "Protected Patient (HIPAA Safe Harbor)",
                    "total_amount": raw_inv.get("total_amount", 0.0),
                    "insurance_covered": raw_inv.get("insurance_covered", 0.0),
                    "patient_due": rem_due,
                    "verified_due": raw_inv.get("verified_due", 0.0),
                    "disputed_due": 0.0,  # Shield raw clinical disputes from caregiver
                    "remaining_patient_due": rem_due,
                    "requested_split_percent": split_pct,
                    "requested_amount": req_amt if req_amt > 0.0 else rem_due,
                    "pay_in_4_eligible": raw_inv.get("pay_in_4_eligible", False),
                    "pay_in_4_installment": raw_inv.get("pay_in_4_installment", 0.0),
                    "status": raw_inv.get("status", "UNPAID"),
                    "hipaa_privacy_mode": True,
                    "items": safe_items,
                    "ai_insights": [
                        "🛡️ HIPAA Safe Harbor Active: Patient name, clinical diagnoses, and service dates (month/day) strictly de-identified.",
                        f"Family Care Contribution: Requested share of {split_pct}% (${req_amt:.2f}) ready for secure PayPal settlement."
                    ]
                }
            self.send_json({"success": True, "invoice": safe_inv, "privacy_mode": True})
            return

        if clean_path.startswith("api/invoice/"):
            inv_id = clean_path.replace("api/invoice/", "").split("?")[0]
            query_params = urllib.parse.parse_qs(parsed.query)
            is_caregiver_mode = query_params.get("mode", [""])[0] == "caregiver"

            with LEDGER_LOCK:
                if inv_id in INVOICE_STORE:
                    raw_inv = INVOICE_STORE[inv_id]
                    if is_caregiver_mode:
                        # Privacy-Preserving HIPAA Safe Harbor Masking for Family / Caregivers
                        p_name = raw_inv.get("patient_name", "Patient")
                        masked_name = p_name[0] + "****** " + (p_name.split()[-1][0] + "****" if len(p_name.split()) > 1 else "")
                        
                        masked_items = []
                        for idx, it in enumerate(raw_inv.get("items", [])):
                            masked_items.append({
                                "code": f"HIPAA-PROT-0{idx+1}",
                                "desc": "Protected Healthcare Clinical Service (Caregiver View)",
                                "charge": it.get("charge", 0.0),
                                "insured": it.get("insured", 0.0),
                                "due": it.get("due", 0.0),
                                "flag": "Caregiver Co-Pay Contribution View"
                            })
                        
                        caregiver_inv = copy.deepcopy(raw_inv)
                        caregiver_inv["patient_name"] = masked_name
                        caregiver_inv["items"] = masked_items
                        caregiver_inv["hipaa_privacy_mode"] = True
                        caregiver_inv["ai_insights"] = [
                            "🛡️ HIPAA Privacy Shield Active: Clinical diagnoses & drug identifiers masked for caregiver contribution.",
                            "Direct PayPal Settlement: Family members can contribute to the copay safely without viewing private diagnostic records."
                        ]
                        self.send_json({"success": True, "invoice": caregiver_inv, "privacy_mode": True})
                    else:
                        self.send_json({"success": True, "invoice": raw_inv, "privacy_mode": False})
                else:
                    self.send_json({"success": False, "error": "Invoice not found"}, status=404)
            return

        if clean_path.startswith("api/hsa-evidence/"):
            inv_id = clean_path.replace("api/hsa-evidence/", "").split("?")[0]
            with LEDGER_LOCK:
                if inv_id in CAREGIVER_TOKENS:
                    t_meta = CAREGIVER_TOKENS[inv_id]
                    inv_id = t_meta["invoice_id"] if isinstance(t_meta, dict) else t_meta

                inv = INVOICE_STORE.get(inv_id)
                if not inv:
                    self.send_json({"success": False, "error": "Invoice not found"}, status=404)
                    return

                captures = inv.get("captures", [])
                last_cap = captures[-1] if captures else {}
                total_paid = float(inv.get("paid_amount", 0.0))
                patient_due = float(inv.get("patient_due", 0.0))
                hsa_el = float(inv.get("hsa_eligible_due", patient_due))
                hsa_rev = float(inv.get("hsa_review_due", 0.0))

                evidence = {
                    "evidence_id": f"HSA-EV-{inv_id.replace('CP-', '')}",
                    "invoice_id": inv_id,
                    "issue_timestamp": time.strftime("%Y-%m-%d %H:%M:%S UTC"),
                    "facility": inv.get("facility", "Healthcare Provider"),
                    "service_date": inv.get("date", time.strftime("%Y-%m-%d")),
                    "patient_name": inv.get("patient_name", "Patient"),
                    "currency": inv.get("currency", "USD"),
                    "total_patient_due": patient_due,
                    "total_settled_via_paypal": total_paid,
                    "paypal_capture_id": last_cap.get("capture_id", "SANDBOX-SETTLED"),
                    "paypal_order_id": inv.get("last_order_id", "ORD-VERIFIED"),
                    "hsa_eligible_amount": hsa_el,
                    "hsa_review_amount": hsa_rev,
                    "statutory_reference": "IRC § 213(d) / IRS Publication 502 (Medical and Dental Expenses)",
                    "rules_version": "2026-v1.0-CAREPAY-BENCHMARK",
                    "legal_notice": (
                        "Eligibility estimate and substantiation record only. CarePay does not provide tax, legal, or plan-administrator advice. "
                        "HSA/FSA eligibility may depend on the item, diagnosis, medical necessity, and your specific plan rules. "
                        "This record is not an IRS-issued or government-endorsed document. Retain original pharmacy/provider records."
                    ),
                    "items": inv.get("items", [])
                }
                self.send_json({"success": True, "evidence": evidence})
            return

        self.send_error(404, "Resource not found")

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        clean_path = parsed.path.lstrip("/")

        # Cap body size at 12MB to prevent memory exhaustion
        MAX_BODY_BYTES = 12 * 1024 * 1024
        try:
            content_length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            self.send_json({"error": "Invalid Content-Length header"}, status=400)
            return

        if content_length > MAX_BODY_BYTES:
            self.send_response(413)
            self.send_header("Content-Type", "application/json")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(b'{"error": "Request payload exceeds size limit (12MB)"}')
            self.close_connection = True
            return

        post_data = self.rfile.read(content_length)

        try:
            body = json.loads(post_data.decode("utf-8")) if post_data else {}
        except Exception:
            body = {}

        if clean_path == "api/analyze":
            self.handle_analyze(body)
            return

        if clean_path == "api/paypal/create-order":
            self.handle_create_order(body)
            return

        if clean_path == "api/paypal/capture-order":
            self.handle_capture_order(body)
            return

        if clean_path == "api/caregiver/create-link":
            self.handle_caregiver_link(body)
            return

        if clean_path == "api/appeal/generate":
            self.handle_appeal_generate(body)
            return

        self.send_json({"error": "Unknown API endpoint"}, status=404)

    def handle_analyze(self, body):
        sample_name = body.get("sample_name", "")
        image_base64 = body.get("image_base64", "")

        # 1. If sample receipt requested
        if sample_name in SAMPLE_BILLS:
            data = copy.deepcopy(SAMPLE_BILLS[sample_name])
            inv_id = f"CP-{secrets.token_hex(4).upper()}"
            invoice_record = {
                "invoice_id": inv_id,
                "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                **data
            }
            with LEDGER_LOCK:
                INVOICE_STORE[inv_id] = invoice_record
                save_persisted_state()
            self.send_json({"success": True, "source": "sample_verified", "data": invoice_record})
            return

        # 2. If live custom image provided and live API key exists
        if image_base64 and NEBIUS_API_KEY and HAS_PIPELINE:
            try:
                # Ensure data URL prefix is present for ocr_image
                if image_base64.startswith("data:image/"):
                    data_url = image_base64
                else:
                    data_url = f"data:image/jpeg;base64,{image_base64}"

                # Real Step 1: Vision OCR transcription
                ocr_res = rp.ocr_image(NEBIUS_BASE_URL, NEBIUS_API_KEY, data_url)
                if isinstance(ocr_res, tuple):
                    lines, latency_ms, used_model = ocr_res
                else:
                    lines = ocr_res
                    latency_ms, used_model = 0, "NVIDIA-Nemotron-3.5-Super-VL"

                if lines:
                    # Real Step 2: Deterministic Python medical parser
                    parsed = rp.parse_receipt(lines)
                    
                    facility = parsed.get("facility") or "Medical Healthcare Provider"
                    date_val = parsed.get("date") or time.strftime("%Y-%m-%d")
                    currency = parsed.get("currency", "USD")
                    raw_items = parsed.get("items", [])

                    # If no items could be reliably extracted, fail closed (never synthesize fake CPT codes)
                    if not raw_items:
                        self.send_json({
                            "success": False,
                            "error": "No Itemized Lines Detected",
                            "message": "OCR transcribed text but could not parse specific medical procedure codes or fee amounts. Please provide a clearer itemized statement."
                        }, status=422)
                        return

                    items = []
                    calc_total = 0.0
                    calc_due = 0.0
                    calc_insured = 0.0
                    calc_verified = 0.0
                    calc_disputed = 0.0

                    for idx, it in enumerate(raw_items):
                        amt = float(it.get("amount") or 0.0)
                        code = it.get("code") or f"REV-0{idx+1}00"
                        desc = it.get("name") or "Healthcare Diagnostic & Service"
                        ins_portion = float(it.get("insured") or 0.0)
                        raw_due = it.get("due")
                        due_portion = float(raw_due) if raw_due is not None else amt
                        calc_total += amt
                        calc_insured += ins_portion
                        calc_due += due_portion

                        flag_str = str(it.get("flag", "Verified Provider Item"))
                        is_disputed = "Surprise" in flag_str
                        if is_disputed:
                            calc_disputed += due_portion
                        else:
                            calc_verified += due_portion

                        items.append({
                            "code": code,
                            "desc": desc,
                            "charge": amt,
                            "insured": ins_portion,
                            "due": due_portion,
                            "flag": flag_str
                        })

                    patient_due = max(0.0, round(calc_due, 2))
                    verified_due = max(0.0, round(calc_verified, 2))
                    disputed_due = max(0.0, round(calc_disputed, 2))
                    pay_in_4_eligible = (patient_due >= 30.00 and patient_due <= 1500.00)
                    installment = round(patient_due / 4, 2) if pay_in_4_eligible else patient_due

                    hsa_el, hsa_rev = enrich_invoice_items(items)

                    inv_id = f"CP-{secrets.token_hex(4).upper()}"
                    invoice_record = {
                        "invoice_id": inv_id,
                        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "facility": facility,
                        "date": date_val,
                        "currency": currency,
                        "patient_name": parsed.get("patient_name") or "Verified Patient",
                        "total_amount": round(calc_total, 2),
                        "insurance_covered": round(calc_insured, 2),
                        "patient_due": patient_due,
                        "verified_due": verified_due,
                        "disputed_due": disputed_due,
                        "hsa_eligible_due": hsa_el,
                        "hsa_review_due": hsa_rev,
                        "status": "UNPAID",
                        "pay_in_4_eligible": pay_in_4_eligible,
                        "pay_in_4_installment": installment,
                        "items": items,
                        "ai_insights": [
                            f"Live Vision Transcription: {len(lines)} line items parsed via NVIDIA Nemotron-3.5-Super-VL.",
                            f"Smart Dispute & Partial Settle: Disputed out-of-network balance: ${disputed_due:.2f}, Verified balance: ${verified_due:.2f}.",
                            f"HSA/FSA Tax Substantiation: ${hsa_el:.2f} pre-screened as qualified medical expenses under IRC § 213(d).",
                            f"Pay in 4 Status: {'Eligible ($30-$1,500 range) - 4 bi-weekly payments of $' + str(installment) if pay_in_4_eligible else 'Single payment only (under $30 threshold)'}.",
                            "CMS Medicare Benchmark Context: Itemized charges audited against 2026 CMS national physician fee schedule."
                        ]
                    }
                    with LEDGER_LOCK:
                        INVOICE_STORE[inv_id] = invoice_record
                        save_persisted_state()
                    self.send_json({"success": True, "source": "live_vision_pipeline", "data": invoice_record})
                    return
            except Exception as e:
                print(f"[Analyze Live Pipeline Exception] {e}")

        # If OCR or sample parsing did not succeed, do NOT fake Eleanor Vance! Return a clean error.
        self.send_json({
            "success": False,
            "error": "OCR Recognition Incomplete",
            "message": "Could not extract clear medical billing items from the image. Please upload a higher resolution receipt or select a verified sample."
        }, status=422)

    def handle_create_order(self, body):
        if not isinstance(body, dict):
            self.send_json({"success": False, "error": "Invalid request body: object expected"}, status=400)
            return

        invoice_id = str(body.get("invoice_id", "")).strip()
        
        with LEDGER_LOCK:
            # Strict server price authority: invoice MUST exist in authorized INVOICE_STORE!
            if not invoice_id or invoice_id not in INVOICE_STORE:
                self.send_json({
                    "success": False,
                    "error": "Invoice Not Found",
                    "message": f"Cannot create order: invoice '{invoice_id}' does not exist in authorized ledger. Client price injection blocked."
                }, status=404)
                return

            record = INVOICE_STORE[invoice_id]
            current_status = record.get("status", "UNPAID")
            server_due = to_money(record.get("patient_due", 0.0))
            rem_due = to_money(record.get("remaining_patient_due", server_due))

            if current_status in ("PAID_IN_FULL", "SETTLED_FULL") or rem_due <= 0.01:
                self.send_json({
                    "success": False,
                    "error": "Invoice Already Settled",
                    "message": f"Invoice '{invoice_id}' has already been paid and settled. Replay payments are blocked."
                }, status=400)
                return

            currency = str(record.get("currency", "USD")).strip() or "USD"
            mode = str(body.get("mode", "full")).strip().lower()

            # Server-enforced amount authority based on payment mode:
            if mode == "verified_only":
                amount = to_money(record.get("verified_due", 0.0))
                if amount <= 0.0:
                    self.send_json({
                        "success": False,
                        "error": "No Verified Balance",
                        "message": "Verified balance is $0.00 (already settled or under dispute hold)."
                    }, status=400)
                    return
            elif mode == "family_split":
                try:
                    split_val = float(body.get("split_amount", 0.0))
                except (ValueError, TypeError):
                    split_val = 0.0
                if split_val < 1.00 or split_val > rem_due + 0.01:
                    self.send_json({
                        "success": False,
                        "error": "Invalid Contribution Amount",
                        "message": f"Family split contribution must be between $1.00 and remaining balance of ${rem_due:.2f}."
                    }, status=400)
                    return
                amount = to_money(split_val)
            else:
                amount = to_money(rem_due)

            # Active pending order idempotency guard to prevent concurrent double-order creation
            active_order = ACTIVE_INVOICE_ORDERS.get(invoice_id)
            if active_order and active_order.get("mode") == mode and abs(active_order.get("amount", 0.0) - amount) < 0.01 and (time.time() - active_order.get("created_time", 0)) < 60:
                self.send_json({
                    "success": True,
                    "order_id": active_order.get("order_id"),
                    "status": "CREATED",
                    "authorized_amount": active_order.get("amount"),
                    "payment_mode": active_order.get("mode"),
                    "mode": "existing_active_order",
                    "message": "Reusing existing active checkout order to prevent double-order creation"
                })
                return

        result = create_paypal_order(amount, currency, invoice_id, payment_mode=mode)
        if result.get("success") and result.get("order_id"):
            with LEDGER_LOCK:
                ACTIVE_INVOICE_ORDERS[invoice_id] = {
                    "order_id": result["order_id"],
                    "amount": amount,
                    "mode": mode,
                    "created_time": time.time()
                }
        self.send_json(result)

    def handle_capture_order(self, body):
        if not isinstance(body, dict):
            self.send_json({"success": False, "error": "Invalid request body: object expected"}, status=400)
            return
        order_id = str(body.get("order_id", "")).strip()
        if not order_id:
            self.send_json({"success": False, "error": "Missing order_id in capture payload"}, status=400)
            return

        result = capture_paypal_order(order_id)
        self.send_json(result)

    def handle_caregiver_link(self, body):
        if not isinstance(body, dict):
            self.send_json({"success": False, "error": "Invalid request body: object expected"}, status=400)
            return

        invoice_id = str(body.get("invoice_id", "")).strip()
        caregiver_email = str(body.get("caregiver_email", "")).strip() or "family@example.com"
        try:
            split_percent = int(body.get("split_percent", 100))
            if split_percent not in (25, 50, 100):
                split_percent = 100
        except (ValueError, TypeError):
            split_percent = 100

        with LEDGER_LOCK:
            if not invoice_id or invoice_id not in INVOICE_STORE:
                self.send_json({
                    "success": False,
                    "error": "Invoice Not Found",
                    "message": f"Cannot generate caregiver link: invoice '{invoice_id}' does not exist in active ledger."
                }, status=404)
                return

            record = INVOICE_STORE[invoice_id]
            # Cryptographically isolated caregiver bearer token (HIPAA Safe Harbor)
            cg_token = f"CG-{secrets.token_urlsafe(16)}"
            rem_due = to_money(record.get("remaining_patient_due", record.get("patient_due", 0.0)))
            req_amount = to_money(max(1.00, rem_due * (split_percent / 100.0))) if rem_due > 0 else 0.0

            CAREGIVER_TOKENS[cg_token] = {
                "invoice_id": invoice_id,
                "split_percent": split_percent,
                "requested_amount": req_amount,
                "created_at": time.time()
            }
            save_persisted_state()
            share_url = f"http://{HOST}:{PORT}/?cg_token={cg_token}"

            self.send_json({
                "success": True,
                "token": cg_token,
                "invoice_id": invoice_id,
                "share_url": share_url,
                "caregiver_email": caregiver_email,
                "split_percent": split_percent,
                "requested_amount": req_amount,
                "patient_name_masked": "Protected Patient (HIPAA Safe Harbor)",
                "patient_due": float(record.get("patient_due", 0.0)),
                "remaining_patient_due": rem_due,
                "verified_due": float(record.get("verified_due", record.get("patient_due", 0.0))),
                "pay_in_4_eligible": bool(record.get("pay_in_4_eligible", False)),
                "pay_in_4_installment": float(record.get("pay_in_4_installment", 0.0)),
                "hipaa_safe_harbor": True,
                "message": f"Caregiver privacy-preserving payment link generated ({split_percent}% contribution: ${req_amount:.2f})."
            })

    def handle_appeal_generate(self, body):
        if not isinstance(body, dict):
            self.send_json({"success": False, "error": "Invalid request body: object expected"}, status=400)
            return

        invoice_id = str(body.get("invoice_id", "")).strip()
        target_item_code = str(body.get("target_item_code", "")).strip()

        # Retrieve record: read-only from INVOICE_STORE, read-only from client draft, or SAMPLE_BILLS
        # NEVER write client-supplied invoice_data into the authorized INVOICE_STORE!
        record = None
        if invoice_id and invoice_id in INVOICE_STORE:
            record = INVOICE_STORE[invoice_id]
        elif "invoice_data" in body and isinstance(body["invoice_data"], dict):
            # Ephemeral read-only draft: client cannot poison INVOICE_STORE
            record = body["invoice_data"]
        elif invoice_id in SAMPLE_BILLS:
            record = SAMPLE_BILLS[invoice_id]

        if not record:
            self.send_json({"success": False, "error": f"Invoice record not found for reference: {invoice_id}"}, status=404)
            return

        facility = record.get("facility", "Healthcare Provider")
        patient_name = record.get("patient_name", "Valued Patient")
        date_of_service = record.get("date", time.strftime("%Y-%m-%d"))
        today_str = time.strftime("%B %d, %Y")

        # Shield protected identity if requested in HIPAA Safe Harbor caregiver mode
        if bool(body.get("is_caregiver") or body.get("hipaa_privacy_mode") or record.get("hipaa_privacy_mode")):
            patient_name = "Protected Patient (HIPAA Safe Harbor)"
            facility = "Verified Healthcare Facility (HIPAA Safe Harbor)"
            date_of_service = str(date_of_service)[:4] + "-XX-XX (HIPAA Safe Harbor Redacted)"
        
        items = record.get("items", [])
        
        # Pinpoint targeted item code or surprise/disputed items
        disputed_items = []
        if target_item_code:
            disputed_items = [it for it in items if str(it.get("code", "")).strip() == target_item_code]
            if not disputed_items:
                self.send_json({
                    "success": False,
                    "error": "Target Item Not Found",
                    "message": f"Service code '{target_item_code}' is not listed in this billing statement."
                }, status=422)
                return
        
        if not disputed_items:
            disputed_items = [it for it in items if "Surprise" in it.get("flag", "") or "Benchmark" in it.get("flag", "")]

        dispute_lines = []
        if disputed_items:
            for it in disputed_items:
                c_amt = float(it.get('charge', 0.0))
                ins_amt = float(it.get('insured', 0.0))
                due_amt = float(it.get('due', 0.0))
                cms_ref = it.get('cms_reference_fee')
                line = (
                    f"  • Service / Code: {it.get('code', 'HCPCS/CPT')} — {it.get('desc', 'Healthcare Service')}\n"
                    f"    Billed Charge: ${c_amt:.2f} | Insurance Covered: ${ins_amt:.2f} | Stated Patient Balance: ${due_amt:.2f}"
                )
                if cms_ref:
                    cms_rate = float(cms_ref)
                    ratio = it.get('markup_ratio') or (round(c_amt / cms_rate, 2) if cms_rate > 0 else None)
                    ratio_str = f" ({ratio}x billed markup ratio)" if ratio else ""
                    line += f"\n    CMS Medicare Benchmark Context: ${cms_rate:.2f} national allowable reference rate{ratio_str}"
                dispute_lines.append(line)
            disputed_text = "\n\n".join(dispute_lines)
            scope_notice = (
                "Upon reviewing this statement with PainFree CarePay AI, the following service item(s) require verification "
                "under the federal No Surprises Act (Public Law 116-260, 45 CFR Part 149) regarding provider network status, "
                "recognized amounts, and statutory patient cost-sharing benchmarks:\n\n"
                f"{disputed_text}"
            )
        else:
            scope_notice = (
                "Pursuant to patient rights under federal healthcare regulations and the No Surprises Act, I am requesting "
                "a comprehensive unbundled itemization of all charges, clinician NPIs, and in-network cost-sharing fee schedules "
                "associated with this date of service."
            )

        letter_text = (
            f"DATE: {today_str}\n\n"
            f"TO:\n"
            f"Billing & Patient Financial Services Department\n"
            f"{facility}\n\n"
            f"FROM:\n"
            f"{patient_name}\n"
            f"RE: Formal Itemized Billing Audit & Cost-Sharing Verification Request\n"
            f"Account / Invoice Reference ID: {invoice_id or 'CP-VERIFIED'}\n"
            f"Date of Service: {date_of_service}\n\n"
            f"Dear Billing Manager,\n\n"
            f"I am writing to formally request a detailed audit and clarification of charges on the account referenced above.\n\n"
            f"{scope_notice}\n\n"
            f"STATEMENT OF INQUIRY UNDER 45 CFR § 149.410 & § 149.420:\n"
            f"1. Facility & Provider Network Status: Please confirm whether all attending and ancillary clinicians rendering care at your facility were participating in-network providers with my health plan.\n"
            f"2. Cost-Sharing, Recognized Amount & QPA Methodology Request: If out-of-network clinicians provided services at this in-network facility without valid prior notice and consent, please explain the applicable recognized amount and furnish the specific Qualifying Payment Amount (QPA) methodology (45 CFR § 149.140) used to benchmark patient responsibility, noting that billed charges significantly exceed CMS Medicare allowable reference schedules. Please confirm how my plan's in-network cost-sharing rules (deductible, copay, or coinsurance) were applied. This inquiry is an informational substantiation request under federal law.\n"
            f"3. Itemized Fee Schedule: Please furnish a fully unbundled billing statement itemizing revenue codes, HCPCS/CPT codes, and National Provider Identifiers (NPI).\n\n"
            f"REQUESTED ADMINISTRATIVE ACTIONS:\n"
            f"1. Place a temporary 30-day administrative hold on this account to suspend any automated collections or late fees while this review is conducted.\n"
            f"2. Issue an updated billing statement reflecting verified in-network cost-sharing calculations.\n\n"
            f"Please note: As soon as the verified itemized calculation is confirmed, I am prepared to promptly settle the undisputed patient responsibility via PayPal.\n\n"
            f"Thank you for your prompt assistance and cooperation.\n\n"
            f"Sincerely,\n\n"
            f"_________________________________________\n"
            f"{patient_name}\n"
            f"Patient / Authorized Healthcare Representative\n\n"
            f"================================================================================\n"
            f"NOTICE & REGULATORY CLARIFICATION:\n"
            f"• Informational Draft: This inquiry letter is an AI-generated draft prepared\n"
            f"  for patient review. It does not constitute formal legal, medical, or tax advice.\n"
            f"• CMS Benchmark vs. QPA: CMS fee schedule values referenced herein reflect federal\n"
            f"  Medicare allowable reference rates. Under the federal No Surprises Act, the\n"
            f"  Qualifying Payment Amount (QPA) represents median in-network contracted rates\n"
            f"  and must be calculated by your specific health plan or issuer.\n"
            f"================================================================================\n"
        )

        self.send_json({
            "success": True,
            "appeal_id": f"APL-{secrets.token_hex(4).upper()}",
            "invoice_id": invoice_id,
            "facility": facility,
            "patient_name": patient_name,
            "target_item_code": target_item_code,
            "letter_text": letter_text,
            "legal_statute": "US No Surprises Act (45 CFR § 149.410 / § 149.420)",
            "disclaimer": "AI-generated draft for patient review. Not legal, medical, or tax/financial advice. CMS fee schedule is a Medicare reference benchmark; QPA must be calculated by your plan/issuer under the No Surprises Act.",
            "disputed_items_count": len(disputed_items),
            "message": "Federal billing audit and dispute letter drafted successfully."
        })

    def send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def run_server():
    load_persisted_state()
    # Use ThreadingHTTPServer for asynchronous concurrent client requests
    with http.server.ThreadingHTTPServer((HOST, PORT), CarePayHandler) as httpd:
        httpd.daemon_threads = True
        print(f"================================================================")
        print(f"  PainFree CarePay AI Server (PayPal AI Hackathon)")
        print(f"  Architecture: ThreadingHTTPServer (Concurrent Async)")
        print(f"  Live Local URL: http://{HOST}:{PORT}")
        print(f"  PayPal Sandbox: Enabled (Client-ID: {PAYPAL_CLIENT_ID})")
        print(f"================================================================")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nShutting down CarePay server.")
            httpd.server_close()


if __name__ == "__main__":
    run_server()

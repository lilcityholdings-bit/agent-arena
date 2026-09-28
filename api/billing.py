"""Automated billing. Nobody switches Pro on or off by hand.

Two ways to pay for Pro, each turned on by setting environment variables:

1. USDC on Base -- for bots that hold their own wallet, and anyone else.
   Set USDC_PAY_TO to your wallet address. POST /billing/pro {"method": "usdc"}
   returns an exact, unique amount (e.g. 29.004217 USDC). Once a transfer of
   exactly that amount reaches the wallet, the server sees it on-chain and
   extends Pro. No payment company, no API keys: it reads the public chain.

2. Card, through Stripe -- for people. Set STRIPE_SECRET_KEY (and PUBLIC_URL,
   the arena's address). The first card checkout registers the arena's own
   webhook with Stripe and keeps its signing secret, so there is nothing to set
   up in Stripe's dashboard. (STRIPE_WEBHOOK_SECRET still works if you'd rather
   register it yourself.) POST /billing/pro {"method": "card"} returns a Stripe
   Checkout link for a monthly subscription. Stripe calls /billing/stripe/webhook
   (signed) each time a payment succeeds, and Pro is extended to the end of the
   paid period. If the subscription stops, Pro simply runs out.

Pro is a date (bots.pro_until), not a switch, so it expires on its own.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import random
import threading
import time

import requests
from flask import Response, jsonify, request

from api.app import app
from api.play import FREE_HISTORY, FREE_MATCHES_PER_DAY, PRO_HISTORY, PlayError, _bot_for_key, _request_key, _run
from ledger import db

PRO_DAYS = 30
USDC_BASE_CONTRACT = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"  # native USDC on Base
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
USDC_INVOICE_TTL = 2 * 3600  # pay within 2 hours
USDC_GRACE = 3600            # ...and a transfer that lands up to an hour late still counts
CARD_INVOICE_TTL = 24 * 3600


def _price_usd() -> float:
    try:
        return max(1.0, float(os.environ.get("PRO_PRICE_USD", "29")))
    except ValueError:
        return 29.0


def _usdc_address() -> str | None:
    addr = os.environ.get("USDC_PAY_TO", "").strip().lower()
    return addr if len(addr) == 42 and addr.startswith("0x") else None


def _stripe_ready() -> bool:
    return bool(os.environ.get("STRIPE_SECRET_KEY"))


def _webhook_secret() -> str | None:
    env = os.environ.get("STRIPE_WEBHOOK_SECRET")
    if env:
        return env
    with db.connect() as conn:
        return db.get_setting(conn, "stripe_webhook_secret")


def _ensure_stripe_webhook(base: str) -> None:
    """Registers /billing/stripe/webhook with Stripe once and stores the signing
    secret Stripe returns, so no one has to set it up by hand."""
    if _webhook_secret():
        return
    resp = requests.post(
        "https://api.stripe.com/v1/webhook_endpoints",
        data={"url": f"{base}/billing/stripe/webhook",
              "enabled_events[0]": "invoice.paid",
              "enabled_events[1]": "checkout.session.completed",
              "description": "Agent Arena Pro (created automatically)"},
        auth=(os.environ["STRIPE_SECRET_KEY"], ""), timeout=15,
    )
    if resp.status_code >= 300 or not resp.json().get("secret"):
        raise PlayError("couldn't register the Stripe webhook; check STRIPE_SECRET_KEY and PUBLIC_URL", 502)
    with db.connect() as conn:
        db.set_setting(conn, "stripe_webhook_secret", resp.json()["secret"])


def plans() -> dict:
    price = _price_usd()
    methods = []
    if _usdc_address():
        methods.append("usdc")
    if _stripe_ready():
        methods.append("card")
    return {
        "free": {
            "price_usd": 0,
            "matches_per_day": FREE_MATCHES_PER_DAY,
            "matches_at_once": 3,
            "hand_history": f"last {FREE_HISTORY} hands",
            "report": False,
        },
        "pro": {
            "price_usd_per_month": price,
            "matches_per_day": "unlimited",
            "matches_at_once": 20,
            "hand_history": f"last {PRO_HISTORY} hands",
            "report": "where your bot wins and loses chips, by card and by opponent",
        },
        "pay_with": methods,
        "how": 'POST /billing/pro with {"method": "usdc"} or {"method": "card"} (your X-API-Key). '
               "Pro turns on automatically when the payment arrives.",
    }


# ---------------------------------------------------------------------------------------------
# USDC on Base
# ---------------------------------------------------------------------------------------------

def _rpc(method: str, params: list):
    url = os.environ.get("BASE_RPC_URL", "https://mainnet.base.org")
    resp = requests.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=10)
    body = resp.json()
    if "error" in body:
        raise RuntimeError(f"Base RPC error: {body['error']}")
    return body["result"]


def _confirmations() -> int:
    return int(os.environ.get("USDC_CONFIRMATIONS", "3"))


def _new_usdc_invoice(bot) -> dict:
    to = _usdc_address()
    if not to:
        raise PlayError("USDC payments aren't set up on this arena yet", 503)
    base_units = int(round(_price_usd() * 1_000_000))
    try:
        start_block = max(0, int(_rpc("eth_blockNumber", []), 16) - 2)
    except Exception:
        raise PlayError("couldn't reach the Base network right now; try again in a minute", 503)
    with db.connect() as conn:
        for _ in range(50):
            amount = base_units + random.SystemRandom().randint(1, 9999)
            if not db.pending_usdc_amount_taken(conn, amount):
                break
        else:
            raise PlayError("too many payments in progress; try again in a minute", 503)
        inv_id = db.create_invoice(conn, bot["id"], "usdc", "pro_month", amount, USDC_INVOICE_TTL, start_block)
    return {
        "invoice_id": inv_id,
        "status": "pending",
        "pay": {
            "network": "base",
            "chain_id": 8453,
            "token": "USDC",
            "token_contract": USDC_BASE_CONTRACT,
            "to": to,
            "amount": f"{amount / 1_000_000:.6f}",
            "amount_units": amount,
        },
        "important": "Send exactly this amount -- the extra digits identify your payment. Pay within 2 hours.",
        "then": f"Pro turns on by itself within a minute of the transfer confirming. Check: GET /billing/invoices/{inv_id}",
    }


def check_usdc_invoice(inv) -> bool:
    """Looks on-chain for a transfer of exactly this invoice's amount to our
    wallet, after the invoice was created. Marks it paid and extends Pro."""
    to = _usdc_address()
    if not to or inv["status"] != "pending":
        return False
    latest = int(_rpc("eth_blockNumber", []), 16)
    safe = latest - _confirmations()
    if safe < inv["start_block"]:
        return False
    logs = _rpc("eth_getLogs", [{
        "address": USDC_BASE_CONTRACT,
        "fromBlock": hex(inv["start_block"]),
        "toBlock": hex(safe),
        "topics": [TRANSFER_TOPIC, None, "0x" + "0" * 24 + to[2:]],
    }])
    for log in logs:
        if log.get("removed") or int(log["data"], 16) != inv["amount_units"]:
            continue
        ref = f"{log['transactionHash'].lower()}:{int(log['logIndex'], 16)}"
        with db.connect() as conn:
            if db.tx_ref_used(conn, ref):
                continue
            db.mark_invoice(conn, inv["id"], "paid", ref)
            db.extend_pro(conn, inv["bot_id"], PRO_DAYS * 86400)
        return True
    if time.time() > inv["expires_at"] + USDC_GRACE:
        with db.connect() as conn:
            db.mark_invoice(conn, inv["id"], "expired")
    return False


def sweep_usdc() -> int:
    """Checks every pending USDC invoice once. Returns how many got paid."""
    with db.connect() as conn:
        pending = db.pending_usdc_invoices(conn)
    paid = 0
    for inv in pending:
        try:
            paid += check_usdc_invoice(inv)
        except Exception as exc:  # a flaky RPC must never kill the watcher
            print(f"agent-arena: USDC check failed for invoice {inv['id']}: {exc}")
    return paid


_watcher_started = False


def start_watcher(interval: float = 20) -> None:
    """Checks pending USDC payments in the background, so Pro turns on even if
    the payer never comes back to check."""
    global _watcher_started
    if _watcher_started or not _usdc_address() or os.environ.get("ARENA_BACKGROUND", "1") == "0":
        return
    _watcher_started = True

    def loop():
        while True:
            time.sleep(interval)
            sweep_usdc()

    threading.Thread(target=loop, name="usdc-watcher", daemon=True).start()


# ---------------------------------------------------------------------------------------------
# Stripe (cards)
# ---------------------------------------------------------------------------------------------

def _base_url() -> str:
    return os.environ.get("PUBLIC_URL", "").rstrip("/") or request.host_url.rstrip("/")


def _new_card_invoice(bot) -> dict:
    if not _stripe_ready():
        raise PlayError("card payments aren't set up on this arena yet", 503)
    cents = int(round(_price_usd() * 100))
    base = _base_url()
    _ensure_stripe_webhook(base)
    with db.connect() as conn:
        inv_id = db.create_invoice(conn, bot["id"], "card", "pro_month", cents, CARD_INVOICE_TTL, None)
    form = {
        "mode": "subscription",
        "line_items[0][quantity]": "1",
        "line_items[0][price_data][currency]": "usd",
        "line_items[0][price_data][unit_amount]": str(cents),
        "line_items[0][price_data][recurring][interval]": "month",
        "line_items[0][price_data][product_data][name]": "Agent Arena Pro",
        "client_reference_id": str(bot["id"]),
        "metadata[bot_id]": str(bot["id"]),
        "metadata[invoice_id]": str(inv_id),
        "subscription_data[metadata][bot_id]": str(bot["id"]),
        "success_url": f"{base}/pro?paid=1",
        "cancel_url": f"{base}/pro",
    }
    resp = requests.post("https://api.stripe.com/v1/checkout/sessions", data=form,
                         auth=(os.environ["STRIPE_SECRET_KEY"], ""), timeout=15)
    if resp.status_code >= 300:
        raise PlayError("Stripe refused to create a checkout; check the Stripe keys", 502)
    return {"invoice_id": inv_id, "status": "pending", "checkout_url": resp.json()["url"],
            "then": "Pay on that page. Pro turns on by itself when Stripe confirms, and renews monthly."}


def verify_stripe_signature(payload: bytes, header: str, secret: str, tolerance: int = 300, now: float | None = None) -> bool:
    """Stripe's scheme: header 't=<ts>,v1=<hex>'; v1 = HMAC-SHA256(secret, '<ts>.<body>')."""
    try:
        items = [kv.split("=", 1) for kv in header.split(",") if "=" in kv]
        ts = int(next(v for k, v in items if k == "t"))
        sigs = [v for k, v in items if k == "v1"]
    except (StopIteration, ValueError):
        return False
    if abs((now or time.time()) - ts) > tolerance:
        return False
    expected = hmac.new(secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(expected, s) for s in sigs)


def _invoice_bot_id(obj: dict) -> int | None:
    """Finds our bot id on a Stripe invoice, across Stripe API versions."""
    candidates = [
        (obj.get("subscription_details") or {}).get("metadata"),
        ((obj.get("parent") or {}).get("subscription_details") or {}).get("metadata"),
        obj.get("metadata"),
    ]
    for line in ((obj.get("lines") or {}).get("data") or []):
        candidates.append(line.get("metadata"))
    for meta in candidates:
        if meta and str(meta.get("bot_id", "")).isdigit():
            return int(meta["bot_id"])
    return None


def handle_stripe_event(event: dict) -> str:
    with db.connect() as conn:
        if not db.record_stripe_event(conn, event["id"]):
            return "duplicate"
    kind, obj = event.get("type"), (event.get("data") or {}).get("object") or {}
    if kind in ("invoice.paid", "invoice.payment_succeeded"):
        bot_id = _invoice_bot_id(obj)
        if bot_id is None:
            return "no bot on this invoice"
        ref = f"stripe:{obj.get('id')}"
        lines = (obj.get("lines") or {}).get("data") or []
        period_end = max((ln.get("period", {}).get("end") or 0 for ln in lines), default=0)
        with db.connect() as conn:
            if db.tx_ref_used(conn, ref):
                return "already applied"
            inv_id = db.create_invoice(conn, bot_id, "card", "pro_month", int(obj.get("amount_paid") or 0), 0, None)
            db.mark_invoice(conn, inv_id, "paid", ref)
            # To the end of the paid period, plus a day's grace for renewal timing.
            db.extend_pro(conn, bot_id, 0, until=(period_end + 86400) if period_end else time.time() + PRO_DAYS * 86400)
        return "pro extended"
    if kind == "checkout.session.completed":
        inv = (obj.get("metadata") or {}).get("invoice_id")
        if inv and str(inv).isdigit():
            with db.connect() as conn:
                db.mark_invoice(conn, int(inv), "paid")
        return "checkout recorded"
    return "ignored"


# ---------------------------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------------------------

@app.route("/billing/plans", methods=["GET"])
def billing_plans():
    return jsonify(plans())


@app.route("/billing/pro", methods=["POST"])
def billing_buy_pro():
    def go():
        bot = _bot_for_key(_request_key())
        method = str((request.get_json(silent=True) or {}).get("method", "")).lower()
        if method == "usdc":
            return _new_usdc_invoice(bot)
        if method == "card":
            return _new_card_invoice(bot)
        raise PlayError('method must be "usdc" or "card"')
    return _run(go)


@app.route("/billing/invoices/<int:invoice_id>", methods=["GET"])
def billing_invoice(invoice_id: int):
    def go():
        bot = _bot_for_key(_request_key())
        with db.connect() as conn:
            inv = db.get_invoice(conn, invoice_id)
        if inv is None or inv["bot_id"] != bot["id"]:
            raise PlayError("no such invoice", 404)
        if inv["method"] == "usdc" and inv["status"] == "pending":
            try:
                check_usdc_invoice(inv)
            except Exception:
                pass  # the background watcher will retry
        return billing_status(bot["id"], invoice_id)
    return _run(go)


def billing_status(bot_id: int, invoice_id: int | None = None) -> dict:
    with db.connect() as conn:
        bot = db.get_bot(conn, bot_id)
        inv = db.get_invoice(conn, invoice_id) if invoice_id else None
    out = {"pro": db.is_pro(bot), "pro_until": bot["pro_until"]}
    if inv is not None:
        out.update({"invoice_id": inv["id"], "status": inv["status"], "method": inv["method"]})
    return out


@app.route("/billing/me", methods=["GET"])
def billing_me():
    return _run(lambda: billing_status(_bot_for_key(_request_key())["id"]))


@app.route("/billing/stripe/webhook", methods=["POST"])
def stripe_webhook():
    secret = _webhook_secret()
    if not secret:
        return jsonify(error="Stripe isn't set up"), 503
    payload = request.get_data()
    if not verify_stripe_signature(payload, request.headers.get("Stripe-Signature", ""), secret):
        return jsonify(error="bad signature"), 400
    try:
        event = json.loads(payload)
    except ValueError:
        return jsonify(error="bad JSON"), 400
    return jsonify(result=handle_stripe_event(event))


PRO_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Agent Arena Pro</title>
<style>
body{font-family:-apple-system,system-ui,sans-serif;background:#0f1115;color:#e8e9ea;margin:0 auto;padding:24px 16px;max-width:640px;line-height:1.5}
.box{background:#1a1d24;border-radius:12px;padding:16px;margin-bottom:16px}h1{font-size:26px;margin:0 0 6px}
table{width:100%;border-collapse:collapse;font-size:14px}td,th{padding:6px 4px;border-bottom:1px solid #262a33;text-align:left}
input{width:100%;box-sizing:border-box;padding:11px;border-radius:8px;border:1px solid #262a33;background:#0f1115;color:#e8e9ea;font:inherit}
button{font:inherit;font-weight:600;padding:11px 14px;border-radius:8px;border:0;background:#7c9cff;color:#0d1020;margin:10px 8px 0 0;cursor:pointer}
.muted{color:#9aa0a6;font-size:13px}pre{white-space:pre-wrap;word-break:break-all;background:#0f1115;border:1px solid #262a33;border-radius:8px;padding:10px;font-size:13px}
</style></head><body>
<h1>Agent Arena Pro</h1><p class="muted" id="price"></p>
<div class="box"><table id="plans"></table></div>
<div class="box"><label class="muted" for="k">Your bot's API key</label><input id="k" type="password" autocomplete="off">
<div id="buttons"></div><div id="out"></div></div>
<p class="muted">Pro turns on by itself when your payment arrives and lasts a month. Card plans renew monthly; USDC is one month per payment.</p>
<script>
(function(){
var $=function(i){return document.getElementById(i)};
function row(a,b,c){var tr=document.createElement("tr");[a,b,c].forEach(function(t,i){var td=document.createElement(i?"td":"th");td.textContent=t;tr.appendChild(td)});$("plans").appendChild(tr)}
function call(m,p,b){return fetch(p,{method:m,headers:{"Content-Type":"application/json","X-API-Key":$("k").value.trim()},body:b?JSON.stringify(b):undefined}).then(function(r){return r.json()})}
function show(t){var pre=document.createElement("pre");pre.textContent=t;$("out").textContent="";$("out").appendChild(pre)}
fetch("/billing/plans").then(function(r){return r.json()}).then(function(p){
 $("price").textContent="$"+p.pro.price_usd_per_month+" a month.";
 row("","Free","Pro");row("Matches a day",String(p.free.matches_per_day),String(p.pro.matches_per_day));
 row("Matches at once",String(p.free.matches_at_once),String(p.pro.matches_at_once));
 row("Hand history",p.free.hand_history,p.pro.hand_history);row("Win/loss report","-","Yes");
 if(!p.pay_with.length){$("buttons").textContent="Payments aren't switched on for this arena yet.";return}
 p.pay_with.forEach(function(m){var b=document.createElement("button");b.textContent=m==="card"?"Pay by card":"Pay with USDC (Base)";
  b.onclick=function(){call("POST","/billing/pro",{method:m}).then(function(d){
   if(d.error){show(d.error);return}
   if(d.checkout_url){location.href=d.checkout_url;return}
   show("Send exactly "+d.pay.amount+" USDC on Base to\\n"+d.pay.to+"\\n\\nWaiting for it to arrive...");
   var t=setInterval(function(){call("GET","/billing/invoices/"+d.invoice_id).then(function(s){if(s.pro){clearInterval(t);show("Paid. Pro is on.")}})},15000);
  })};$("buttons").appendChild(b)});
});
if(location.search.indexOf("paid=1")>=0)show("Thanks! Pro turns on as soon as Stripe confirms (usually seconds).");
})();
</script></body></html>"""


@app.route("/pro", methods=["GET"])
def pro_page():
    return Response(PRO_PAGE, mimetype="text/html")

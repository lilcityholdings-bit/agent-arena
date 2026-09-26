"""Automated billing: USDC on Base (matched on-chain) and cards through Stripe
(signed webhooks). Pro is a date that payments extend and that runs out on its
own -- these tests make sure nothing needs a human, and that nobody gets Pro
without paying, pays twice for one transfer, or forges a Stripe event."""
import hashlib
import hmac
import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

fd, _TEST_DB_PATH = tempfile.mkstemp(suffix=".db")
os.close(fd)
os.environ["ARENA_DB_PATH"] = _TEST_DB_PATH
os.environ["ARENA_BACKGROUND"] = "0"
os.environ.pop("USDC_PAY_TO", None)
os.environ.pop("STRIPE_SECRET_KEY", None)
os.environ.pop("STRIPE_WEBHOOK_SECRET", None)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api import billing, play  # noqa: E402
from api.app import create_app  # noqa: E402
from ledger import db  # noqa: E402

WALLET = "0x" + "ab" * 20
_n = [0]


class FakeChain:
    """Stands in for the Base network's JSON-RPC."""

    def __init__(self):
        self.block = 1000
        self.logs = []

    def pay(self, amount_units, to=WALLET, tx=None, log_index=0):
        self.block += 1
        _n[0] += 1
        self.logs.append({
            "blockNumber": hex(self.block),
            "transactionHash": tx or "0x" + f"{_n[0]:064x}",
            "logIndex": hex(log_index),
            "data": hex(amount_units),
            "topics": [billing.TRANSFER_TOPIC, "0x" + "0" * 64, "0x" + "0" * 24 + to[2:]],
        })

    def __call__(self, method, params):
        if method == "eth_blockNumber":
            return hex(self.block)
        if method == "eth_getLogs":
            f = params[0]
            lo, hi = int(f["fromBlock"], 16), int(f["toBlock"], 16)
            return [l for l in self.logs if lo <= int(l["blockNumber"], 16) <= hi and l["topics"][2] == f["topics"][2]]
        raise AssertionError(method)


def stripe_sig(payload: bytes, secret: str, ts=None) -> str:
    ts = ts or int(time.time())
    return f"t={ts},v1=" + hmac.new(secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()


class BillingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = create_app()
        cls.app.testing = True

    @classmethod
    def tearDownClass(cls):
        os.remove(_TEST_DB_PATH)

    def setUp(self):
        self.client = self.app.test_client()
        self.chain = FakeChain()
        self.rpc = mock.patch.object(billing, "_rpc", self.chain)
        self.rpc.start()
        os.environ["USDC_PAY_TO"] = WALLET
        os.environ["USDC_CONFIRMATIONS"] = "2"

    def tearDown(self):
        self.rpc.stop()
        for k in ("USDC_PAY_TO", "STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET"):
            os.environ.pop(k, None)

    def bot(self):
        _n[0] += 1
        r = self.client.post("/bots", json={"name": f"payer-{_n[0]}"}).get_json()
        return {"X-API-Key": r["api_key"]}, r["id"]

    def pro(self, bot_id):
        with db.connect() as conn:
            return db.is_pro(db.get_bot(conn, bot_id))

    def usdc_invoice(self, headers):
        r = self.client.post("/billing/pro", json={"method": "usdc"}, headers=headers)
        self.assertEqual(r.status_code, 200, r.get_json())
        return r.get_json()


class TestUSDC(BillingTest):
    def test_paying_the_exact_amount_turns_pro_on_by_itself(self):
        headers, bot_id = self.bot()
        inv = self.usdc_invoice(headers)
        self.assertEqual(inv["pay"]["to"], WALLET)
        self.assertEqual(inv["pay"]["token_contract"], billing.USDC_BASE_CONTRACT)
        self.assertFalse(self.pro(bot_id))
        self.chain.pay(inv["pay"]["amount_units"])
        self.assertEqual(billing.sweep_usdc(), 0, "not enough confirmations yet")
        self.chain.block += 2
        self.assertEqual(billing.sweep_usdc(), 1, "the background check finds it, no one has to ask")
        self.assertTrue(self.pro(bot_id))
        status = self.client.get(f"/billing/invoices/{inv['invoice_id']}", headers=headers).get_json()
        self.assertEqual(status["status"], "paid")
        self.assertGreater(status["pro_until"], time.time() + 29 * 86400)

    def test_the_wrong_amount_or_wallet_does_nothing(self):
        headers, bot_id = self.bot()
        inv = self.usdc_invoice(headers)
        self.chain.pay(inv["pay"]["amount_units"] - 1)
        self.chain.pay(inv["pay"]["amount_units"], to="0x" + "cd" * 20)
        self.chain.block += 5
        billing.sweep_usdc()
        self.assertFalse(self.pro(bot_id))

    def test_one_transfer_can_only_pay_one_invoice(self):
        headers, bot_id = self.bot()
        inv = self.usdc_invoice(headers)
        self.chain.pay(inv["pay"]["amount_units"], tx="0x" + "11" * 32)
        self.chain.block += 5
        billing.sweep_usdc()
        with db.connect() as conn:
            other = db.create_invoice(conn, bot_id, "usdc", "pro_month", inv["pay"]["amount_units"], 7200, 0)
            other_row = db.get_invoice(conn, other)
        self.assertFalse(billing.check_usdc_invoice(other_row))

    def test_every_open_invoice_gets_a_different_amount(self):
        headers, _ = self.bot()
        amounts = {self.usdc_invoice(headers)["pay"]["amount_units"] for _ in range(20)}
        self.assertEqual(len(amounts), 20)
        base = int(billing._price_usd() * 1_000_000)
        self.assertTrue(all(base < a < base + 10_000 for a in amounts), "at most a cent over the price")

    def test_an_unpaid_invoice_expires(self):
        headers, _ = self.bot()
        inv = self.usdc_invoice(headers)
        self.chain.block += 5
        with mock.patch("time.time", return_value=time.time() + billing.USDC_INVOICE_TTL + billing.USDC_GRACE + 1):
            billing.sweep_usdc()
        with db.connect() as conn:
            self.assertEqual(db.get_invoice(conn, inv["invoice_id"])["status"], "expired")

    def test_usdc_is_off_until_a_wallet_is_set(self):
        os.environ.pop("USDC_PAY_TO")
        headers, _ = self.bot()
        self.assertEqual(self.client.post("/billing/pro", json={"method": "usdc"}, headers=headers).status_code, 503)
        self.assertNotIn("usdc", self.client.get("/billing/plans").get_json()["pay_with"])


class TestStripe(BillingTest):
    SECRET = "whsec_test"

    def setUp(self):
        super().setUp()
        os.environ["STRIPE_SECRET_KEY"] = "sk_test_x"
        os.environ["STRIPE_WEBHOOK_SECRET"] = self.SECRET

    def post_event(self, event, secret=None, ts=None):
        payload = json.dumps(event).encode()
        return self.client.post("/billing/stripe/webhook", data=payload, headers={
            "Stripe-Signature": stripe_sig(payload, secret or self.SECRET, ts), "Content-Type": "application/json"})

    def paid_event(self, bot_id, event_id, invoice_id="in_1", period_end=None):
        return {"id": event_id, "type": "invoice.paid", "data": {"object": {
            "id": invoice_id, "amount_paid": 2900,
            "parent": {"subscription_details": {"metadata": {"bot_id": str(bot_id)}}},
            "lines": {"data": [{"period": {"end": period_end or int(time.time()) + 30 * 86400}}]},
        }}}

    def test_checkout_link_is_a_monthly_subscription_tagged_with_the_bot(self):
        headers, bot_id = self.bot()
        fake = mock.Mock(status_code=200)
        fake.json.return_value = {"url": "https://checkout.stripe.com/c/pay/abc"}
        with mock.patch.object(billing.requests, "post", return_value=fake) as post:
            r = self.client.post("/billing/pro", json={"method": "card"}, headers=headers).get_json()
        self.assertEqual(r["checkout_url"], "https://checkout.stripe.com/c/pay/abc")
        form = post.call_args.kwargs["data"]
        self.assertEqual(form["mode"], "subscription")
        self.assertEqual(form["line_items[0][price_data][recurring][interval]"], "month")
        self.assertEqual(form["subscription_data[metadata][bot_id]"], str(bot_id))

    def test_the_webhook_registers_itself_when_only_the_secret_key_is_set(self):
        os.environ.pop("STRIPE_WEBHOOK_SECRET")
        headers, bot_id = self.bot()
        hook = mock.Mock(status_code=200)
        hook.json.return_value = {"secret": "whsec_auto"}
        session = mock.Mock(status_code=200)
        session.json.return_value = {"url": "https://checkout.stripe.com/c/pay/xyz"}
        with mock.patch.object(billing.requests, "post", side_effect=[hook, session]) as post:
            self.client.post("/billing/pro", json={"method": "card"}, headers=headers)
        self.assertIn("webhook_endpoints", post.call_args_list[0].args[0])
        self.assertTrue(post.call_args_list[0].kwargs["data"]["url"].endswith("/billing/stripe/webhook"))
        r = self.post_event(self.paid_event(bot_id, "evt_auto", "in_auto"), secret="whsec_auto")
        self.assertEqual(r.get_json()["result"], "pro extended", "events signed with the stored secret are accepted")
        with db.connect() as conn:
            conn.execute("DELETE FROM settings WHERE key = 'stripe_webhook_secret'")

    def test_a_paid_invoice_turns_pro_on_until_the_period_ends(self):
        headers, bot_id = self.bot()
        end = int(time.time()) + 30 * 86400
        r = self.post_event(self.paid_event(bot_id, "evt_1", "in_a", end))
        self.assertEqual(r.get_json()["result"], "pro extended")
        self.assertTrue(self.pro(bot_id))
        with db.connect() as conn:
            self.assertAlmostEqual(db.get_bot(conn, bot_id)["pro_until"], end + 86400, delta=2)

    def test_forged_old_and_repeated_events_do_nothing(self):
        headers, bot_id = self.bot()
        self.assertEqual(self.post_event(self.paid_event(bot_id, "evt_f"), secret="whsec_wrong").status_code, 400)
        self.assertEqual(self.post_event(self.paid_event(bot_id, "evt_o"), ts=int(time.time()) - 3600).status_code, 400)
        self.assertFalse(self.pro(bot_id))
        self.post_event(self.paid_event(bot_id, "evt_r", "in_r"))
        with db.connect() as conn:
            first = db.get_bot(conn, bot_id)["pro_until"]
        self.assertEqual(self.post_event(self.paid_event(bot_id, "evt_r", "in_r")).get_json()["result"], "duplicate")
        self.assertEqual(self.post_event(self.paid_event(bot_id, "evt_r2", "in_r")).get_json()["result"], "already applied")
        with db.connect() as conn:
            self.assertEqual(db.get_bot(conn, bot_id)["pro_until"], first)

    def test_pro_runs_out_on_its_own(self):
        headers, bot_id = self.bot()
        self.post_event(self.paid_event(bot_id, "evt_exp", "in_exp", period_end=int(time.time()) - 3 * 86400))
        self.assertFalse(self.pro(bot_id), "a period that already ended (plus grace) doesn't leave Pro on")


class TestWhatProBuys(BillingTest):
    def make_pro(self, bot_id):
        with db.connect() as conn:
            db.extend_pro(conn, bot_id, 86400)

    def test_free_bots_have_a_daily_match_limit_and_pro_bots_dont(self):
        headers, bot_id = self.bot()
        with mock.patch.object(play, "FREE_MATCHES_PER_DAY", 2):
            for _ in range(2):
                s = self.client.post("/play", json={"game": "poker", "opponent": "easy", "length": 1, "wait": 0}, headers=headers).get_json()
                while s["status"] != "match_over":
                    s = self.client.post(f"/play/{s['match_id']}/move", json={"move": "fold" if "fold" in s["legal_moves"] else s["legal_moves"][0], "wait": 0}, headers=headers).get_json()
            r = self.client.post("/play", json={"game": "poker", "opponent": "easy", "wait": 0}, headers=headers)
            self.assertEqual(r.status_code, 402)
            self.assertEqual(r.get_json()["upgrade"], "/pro")
            self.make_pro(bot_id)
            self.assertEqual(self.client.post("/play", json={"game": "poker", "opponent": "easy", "wait": 0}, headers=headers).status_code, 200)

    def test_the_report_is_pro_only_and_gives_advice(self):
        headers, bot_id = self.bot()
        s = self.client.post("/play", json={"game": "poker", "opponent": "easy", "length": 12, "wait": 0}, headers=headers).get_json()
        while s["status"] != "match_over":
            s = self.client.post(f"/play/{s['match_id']}/move", json={"move": "call" if "call" in s["legal_moves"] else "check", "wait": 0}, headers=headers).get_json()
        self.assertEqual(self.client.get("/me/report?game=poker", headers=headers).status_code, 402)
        self.assertEqual(self.client.get("/me/history?game=poker", headers=headers).get_json()["limit"], play.FREE_HISTORY)
        self.make_pro(bot_id)
        rep = self.client.get("/me/report?game=poker", headers=headers).get_json()
        self.assertEqual(rep["hands"], 12)
        self.assertIn("by_your_card", rep)
        self.assertTrue(rep["advice"])
        self.assertEqual(self.client.get("/me/history?game=poker", headers=headers).get_json()["limit"], play.PRO_HISTORY)

    def test_the_pro_page_and_plans(self):
        self.assertEqual(self.client.get("/pro").status_code, 200)
        plans = self.client.get("/billing/plans").get_json()
        self.assertEqual(plans["pro"]["price_usd_per_month"], 29.0)
        self.assertIn("usdc", plans["pay_with"])


if __name__ == "__main__":
    unittest.main()

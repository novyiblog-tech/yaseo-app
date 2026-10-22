"""Адаптер Т-Банка без сети: подпись по примеру из документации, статусы, сумма."""
import hashlib
from decimal import Decimal
from unittest import mock

from pgtest import PgTestCase

from yaseo_app import billing


class TBankTest(PgTestCase):
    def prov(self):
        return billing.TBankProvider(self.conn, "Merchant", "11111111111111")

    def test_token_is_sha256_of_sorted_root_values_with_password(self):
        t = self.prov().token({"TerminalKey": "Merchant", "Amount": 19200, "OrderId": "00000",
                               "Description": "Карта", "Receipt": {"x": 1}, "DATA": {"a": "b"}})
        raw = "19200Карта0000011111111111111Merchant"  # Amount, Description, OrderId, Password, TerminalKey
        self.assertEqual(t, hashlib.sha256(raw.encode()).hexdigest())

    def test_status_mapping_and_amount(self):
        p = self.prov()
        for bank, ours in (("CONFIRMED", "succeeded"), ("REJECTED", "canceled"),
                           ("NEW", "pending"), ("AUTHORIZED", "pending")):
            with mock.patch.object(p, "call", return_value={"Status": bank, "Amount": 129000}):
                st = p.fetch("1")
            self.assertEqual((st["status"], str(st["amount"])), (ours, "1290"))


class OffTest(PgTestCase):
    def test_off_refuses_purchase_and_creates_no_payment(self):
        from yaseo_app.accounts import Refused
        u = self.user()
        with self.assertRaises(Refused):
            billing.start_purchase(self.conn, u, "start", billing.OffProvider(), "/r?p={payment}")
        n = self.conn.execute("SELECT count(*) AS n FROM payments").fetchone()["n"]
        self.assertEqual(n, 0)


class RecurrentTest(PgTestCase):
    def setUp(self):
        super().setUp()
        self.p = billing.TBankProvider(self.conn, "Merchant", "11111111111111")
        self.u = self.user()

    def pay(self, **kw):
        return {"id": 7, "user_id": self.u["id"], "plan": "pro", "amount_rub": "2990.00",
                "auto_renew": False, **kw}

    def test_first_payment_with_consent_asks_bank_to_save_card(self):
        with mock.patch.object(self.p, "call", return_value={"PaymentId": 5, "PaymentURL": "u"}) as call:
            self.p.create(self.pay(auto_renew=True), "/r")
        f = call.call_args.args[1]
        self.assertEqual((f["Recurrent"], f["OperationInitiatorType"]), ("Y", "1"))
        self.assertEqual(f["CustomerKey"], f"yaseo-{self.u['id']}")
        self.assertEqual(f["Amount"], 299000)

    def test_payment_without_consent_does_not_save_card(self):
        with mock.patch.object(self.p, "call", return_value={"PaymentId": 5, "PaymentURL": "u"}) as call:
            self.p.create(self.pay(), "/r")
        self.assertNotIn("Recurrent", call.call_args.args[1])
        self.assertNotIn("CustomerKey", call.call_args.args[1])

    def test_notification_signature(self):
        body = {"TerminalKey": "Merchant", "OrderId": "yaseo-7", "Success": True,
                "Status": "AUTHORIZED", "PaymentId": 5, "Amount": 299000, "RebillId": 123,
                "Data": {"a": 1}}
        body["Token"] = self.p.token({k: v for k, v in body.items()})
        self.assertTrue(self.p.notification_valid(body))
        self.assertFalse(self.p.notification_valid({**body, "RebillId": 124}))
        self.assertFalse(self.p.notification_valid({**body, "Token": "x"}))
        self.assertFalse(self.p.notification_valid({**body, "TerminalKey": "Other"}))
        self.assertFalse(self.p.notification_valid({k: v for k, v in body.items() if k != "Token"}))

    def test_charge_inits_then_charges_saved_card(self):
        calls = []

        def fake(method, fields):
            calls.append((method, fields))
            return {"PaymentId": 9} if method == "Init" else {"Status": "CONFIRMED"}
        with mock.patch.object(self.p, "call", side_effect=fake):
            pid, state = self.p.charge(self.pay(), "555")
        self.assertEqual((pid, state["status"]), ("9", "succeeded"))
        self.assertEqual([m for m, _ in calls], ["Init", "Charge"])
        self.assertEqual(calls[0][1]["OperationInitiatorType"], "R")
        self.assertEqual(calls[1][1], {"PaymentId": "9", "RebillId": "555"})

    def test_charge_rejected_is_canceled_with_reason(self):
        out = {"Init": {"PaymentId": 9}, "Charge": {"Status": "REJECTED"}}
        with mock.patch.object(self.p, "call", side_effect=lambda m, f: out[m]):
            _, state = self.p.charge(self.pay(), "555")
        self.assertEqual(state["status"], "canceled")
        self.assertIn("REJECTED", state["error"])

    def test_saved_method_takes_last_active_card(self):
        cards = [{"RebillId": "1", "Status": "A"}, {"RebillId": None, "Status": "A"},
                 {"RebillId": "3", "Status": "D"}, {"RebillId": "4", "Status": "A"}]
        with mock.patch.object(self.p, "call", return_value={"Cards": cards}):
            self.assertEqual(self.p.saved_method(self.pay()), "4")

    def test_rebill_from_notification_enables_renewal_and_charge(self):
        """Платёж с согласием → уведомление с RebillId → тариф продлевается списанием."""
        prov = self.p
        url = billing.start_purchase(self.conn, self.u, "pro", _Stub(prov), "/r?p={payment}",
                                     auto_renew=True)
        self.assertEqual(url, "u")
        billing.save_method(self.conn, prov, "5", "555")
        with mock.patch.object(prov, "fetch", return_value={
                "status": "succeeded", "method": None, "amount": Decimal("4990")}):
            billing.confirm(self.conn, prov, "5")
        sub = billing.subscription(self.conn, self.u["id"])
        self.assertEqual((sub["payment_method"], sub["auto_renew"]), ("555", True))
        self.conn.execute("UPDATE subscriptions SET period_end = now() - interval '1 minute'")
        with mock.patch.object(prov, "charge", return_value=("9", {"status": "succeeded"})) as ch:
            out = billing.renew_due(self.conn, prov)
        self.assertEqual(out["renewed"], 1)
        self.assertEqual(ch.call_args.args[1], "555")


class _Stub:
    """Оборачивает TBankProvider: create без сети, остальное настоящее."""
    def __init__(self, prov):
        self.prov, self.name = prov, prov.name

    def create(self, payment, return_url):
        self.prov.last = payment
        return "5", "u"


class RefundTest(PgTestCase):
    def setUp(self):
        super().setUp()
        self.p = billing.TBankProvider(self.conn, "Merchant", "11111111111111")
        self.u = self.user()
        self.pid = self.conn.execute(
            "INSERT INTO payments (user_id, plan, amount_rub, purpose, provider, provider_id,"
            " idempotence_key, status, months) VALUES (%s, 'start', 2990, 'purchase', 'tbank',"
            " '55', 'k', 'succeeded', 1) RETURNING id", (self.u["id"],)).fetchone()["id"]

    def test_cancel_sends_full_amount_and_refund_receipt(self):
        with mock.patch.dict("os.environ", {"YASEO_TBANK_TAXATION": "usn_income"}), \
                mock.patch.object(self.p, "call", return_value={"Status": "REFUNDED"}) as call:
            self.p.refund("55")
        method, f = call.call_args.args
        self.assertEqual((method, f["PaymentId"], f["Amount"]), ("Cancel", "55", 299000))
        self.assertEqual(f["Receipt"]["Taxation"], "usn_income")
        self.assertEqual(f["Receipt"]["Items"][0]["Amount"], 299000)

    def test_refund_payment_closes_plan_when_bank_confirms(self):
        self.conn.execute("UPDATE users SET plan = 'start' WHERE id = %s", (self.u["id"],))
        self.conn.execute(
            "INSERT INTO subscriptions (user_id, plan, status, period_start, period_end, months)"
            " VALUES (%s, 'start', 'active', now(), now() + interval '30 days', 1)", (self.u["id"],))
        calls = []

        def fake(method, fields):
            calls.append(method)
            return {"Status": "REFUNDED", "Amount": 299000}

        with mock.patch.object(self.p, "call", side_effect=fake):
            pay = billing.refund_payment(self.conn, self.p, self.pid)
        self.assertEqual(calls, ["Cancel", "GetState"])
        self.assertEqual(pay["status"], "refunded")

    def test_refund_refuses_unpaid_payment(self):
        from yaseo_app.accounts import Refused
        self.conn.execute("UPDATE payments SET status = 'pending' WHERE id = %s", (self.pid,))
        with self.assertRaises(Refused):
            billing.refund_payment(self.conn, self.p, self.pid)

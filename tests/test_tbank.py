"""Адаптер Т-Банка без сети: подпись по примеру из документации, статусы, сумма."""
import hashlib
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

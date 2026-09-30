"""Временная база на локальном Postgres: создаётся на класс тестов и удаляется после."""
import os
import secrets
import unittest

import psycopg

from yaseo_app import db

ADMIN_DSN = os.environ.get("YASEO_TEST_ADMIN_DSN", "dbname=postgres")


class PgTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dbname = f"yaseo_test_{secrets.token_hex(4)}"
        try:
            with psycopg.connect(ADMIN_DSN, autocommit=True) as admin:
                admin.execute(f'CREATE DATABASE "{cls.dbname}"')
        except psycopg.OperationalError as exc:
            raise unittest.SkipTest(f"нет локального Postgres: {exc}")
        cls.dsn = f"{ADMIN_DSN.replace('dbname=postgres', '')} dbname={cls.dbname}".strip()
        cls.conn = db.connect(cls.dsn)
        db.migrate(cls.conn)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        with psycopg.connect(ADMIN_DSN, autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{cls.dbname}" WITH (FORCE)')

    def setUp(self):
        self.conn.execute("TRUNCATE spend, cache, jobs, sites, users RESTART IDENTITY CASCADE")
        self.conn.execute("DROP TABLE IF EXISTS fake_provider")
        self.conn.execute("TRUNCATE outbox, email_tokens, invites, waitlist CASCADE")
        self.conn.execute("DELETE FROM plan_terms")
        self.conn.execute("DELETE FROM plans")
        self.conn.execute("DELETE FROM applied")  # тарифы — как в свежей базе
        self.conn.execute("DELETE FROM sources")
        db.migrate(self.conn)  # вернуть источники к заводским настройкам

    def user(self, email="u@test", plan="free") -> dict:
        return self.conn.execute(
            "INSERT INTO users (email, plan, email_confirmed_at) VALUES (%s, %s, now())"
            " RETURNING *", (email, plan)
        ).fetchone()

    def set_source(self, name, **fields):
        cols = ", ".join(f"{k} = %s" for k in fields)
        self.conn.execute(f"UPDATE sources SET {cols} WHERE name = %s",
                          (*fields.values(), name))

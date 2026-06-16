"""SQLite connection management and schema initialisation."""

from __future__ import annotations

import os
import sqlite3

from . import config


def db_connect() -> sqlite3.Connection:
    directory = os.path.dirname(config.DATABASE_PATH)
    if directory:
        os.makedirs(directory, exist_ok=True)
    connection = sqlite3.connect(config.DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    # WAL keeps readers from blocking the single writer; busy_timeout lets
    # connections wait briefly for a lock instead of failing immediately.
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=5000")
    connection.execute("PRAGMA synchronous=NORMAL")
    return connection


def init_database() -> None:
    with db_connect() as connection:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS devices (
                device_id TEXT PRIMARY KEY,
                first_seen INTEGER NOT NULL,
                last_seen INTEGER NOT NULL,
                remote_addr TEXT,
                user_agent TEXT,
                payload_json TEXT NOT NULL DEFAULT '{}',
                status_json TEXT NOT NULL DEFAULT '{}',
                status_dirty INTEGER NOT NULL DEFAULT 0
            )
        """)
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(devices)").fetchall()
        }
        if "status_dirty" not in columns:
            connection.execute("ALTER TABLE devices ADD COLUMN status_dirty INTEGER NOT NULL DEFAULT 0")
        connection.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id TEXT PRIMARY KEY,
                received_at INTEGER NOT NULL,
                device_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{}',
                delivered_at INTEGER,
                acked_at INTEGER,
                attempts INTEGER NOT NULL DEFAULT 0,
                ack_ok INTEGER,
                ack_error TEXT
            )
        """)
        connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_events_pending
            ON events(acked_at, delivered_at, received_at)
        """)
        connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_events_recent
            ON events(received_at DESC)
        """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS dashboard_snapshots (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                received_at INTEGER NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{}'
            )
        """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS paired_devices (
                device_id TEXT PRIMARY KEY,
                first_seen INTEGER NOT NULL,
                last_seen INTEGER NOT NULL,
                remote_addr TEXT,
                user_agent TEXT,
                payload_json TEXT NOT NULL DEFAULT '{}'
            )
        """)

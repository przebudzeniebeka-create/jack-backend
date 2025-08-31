import os
import sqlite3
from contextlib import closing

DB_FILE = os.getenv("SQLITE_PATH", "jack_memory.db")

def init_db():
    with closing(sqlite3.connect(DB_FILE)) as conn, closing(conn.cursor()) as cursor:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT,
                role TEXT,
                message TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()

def save_message(user_id, role, message):
    with closing(sqlite3.connect(DB_FILE)) as conn, closing(conn.cursor()) as cursor:
        cursor.execute(
            "INSERT INTO conversations (user_id, role, message) VALUES (?, ?, ?)",
            (user_id, role, message),
        )
        conn.commit()



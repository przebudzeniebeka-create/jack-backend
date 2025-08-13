import sqlite3

def get_db_connection():
    conn = sqlite3.connect("conversations.db")
    conn.row_factory = sqlite3.Row
    return conn

def create_tables():
    conn = get_db_connection()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS conversations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            user_message TEXT,
            assistant_message TEXT
        )
    """)
    conn.commit()
    conn.close()

def save_message(session_id, user_message, assistant_message):
    conn = get_db_connection()
    conn.execute("""
        INSERT INTO conversations (session_id, user_message, assistant_message)
        VALUES (?, ?, ?)
    """, (session_id, user_message, assistant_message))
    conn.commit()
    conn.close()

def get_conversation(session_id):
    conn = get_db_connection()
    rows = conn.execute("""
        SELECT user_message, assistant_message
        FROM conversations
        WHERE session_id = ?
    """, (session_id,)).fetchall()
    conn.close()
    return [{"user": row["user_message"], "assistant": row["assistant_message"]} for row in rows]

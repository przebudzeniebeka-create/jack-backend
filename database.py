import sqlite3

def init_db():
    conn = sqlite3.connect('jack_memory.db')
    cursor = conn.cursor()

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS conversations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT,
            role TEXT,
            message TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    conn.commit()
    conn.close()


def save_message(user_id, role, message):
    conn = sqlite3.connect('jack_memory.db')
    cursor = conn.cursor()

    cursor.execute('''
        INSERT INTO conversations (user_id, role, message)
        VALUES (?, ?, ?)
    ''', (user_id, role, message))

    conn.commit()
    conn.close()

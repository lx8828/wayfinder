import sqlite3
from pathlib import Path


DB_PATH = Path(__file__).parent / "memory.db"


def get_connection():
    return sqlite3.connect(DB_PATH)


def init_db():

    conn = get_connection()

    cursor = conn.cursor()


    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS memories (

            id INTEGER PRIMARY KEY AUTOINCREMENT,

            key TEXT NOT NULL UNIQUE,

            value TEXT NOT NULL,

            category TEXT,

            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP

        )
        """
    )


    conn.commit()

    conn.close()


if __name__ == "__main__":

    init_db()

    print("数据库初始化完成")
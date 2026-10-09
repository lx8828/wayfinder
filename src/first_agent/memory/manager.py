from first_agent.memory.database import get_connection


def get_memory(key):
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT value, category
            FROM memories
            WHERE key = ?
            """,
            (key,)
        )
        result = cursor.fetchone()
        if result:
            return {"value": result[0], "category": result[1]}
        return None
    finally:
        conn.close()


def get_all_memory():
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT key, value
            FROM memories
            """
        )
        return cursor.fetchall()
    finally:
        conn.close()


def delete_memory(key):
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            DELETE FROM memories
            WHERE key = ?
            """,
            (key,)
        )
        conn.commit()
    finally:
        conn.close()


def clear_memory():
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM memories")
        conn.commit()
    finally:
        conn.close()
def update_memory(key, value, category=None):

    conn = get_connection()

    try:

        cursor = conn.cursor()

        if category:
            cursor.execute(
                """
                UPDATE memories
                SET value = ?, category = ?
                WHERE key = ?
                """,
                (
                    value,
                    category,
                    key
                )
            )
        else:
            cursor.execute(
                """
                UPDATE memories
                SET value = ?
                WHERE key = ?
                """,
                (
                    value,
                    key
                )
            )

        conn.commit()

    finally:

        conn.close()
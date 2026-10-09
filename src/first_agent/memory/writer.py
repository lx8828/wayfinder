from first_agent.memory.database import get_connection


def save_memory(memory):

    if not memory.get("key"):
        return


    conn = get_connection()

    cursor = conn.cursor()


    cursor.execute(
        """
        INSERT INTO memories
        (
            key,
            value,
            category
        )
        VALUES
        (
            ?,
            ?,
            ?
        )
        ON CONFLICT(key)
        DO UPDATE SET value=excluded.value, category=excluded.category
        """,
        (
            memory["key"],
            memory["value"],
            memory["category"]
        )
    )


    conn.commit()

    conn.close()
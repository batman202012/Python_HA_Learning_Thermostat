"""
database.py
Handles all SQLite connections, schema initialization, and queries.
"""

import sqlite3
from datetime import datetime
from config import DB_PATH

# --- DATABASE SETUP ---
def initialize_brain():
    """Creates the SQLite database and necessary tables if they do not exist."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS session_state (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS q_table (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            time_block TEXT,
            temp_band TEXT,
            humidity_band TEXT,
            is_peak_pricing BOOLEAN,
            action_taken TEXT,
            q_score REAL DEFAULT 0.0,
            UNIQUE(time_block, temp_band, humidity_band, is_peak_pricing, action_taken)
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS history_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            time_block TEXT,
            actual_temp REAL,
            target_temp REAL,  -- NEW COLUMN ADDED HERE
            actual_humidity REAL,
            action_taken TEXT,
            kwh_consumed REAL,
            user_overrides INTEGER,
            reward_granted REAL
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS schedule (
            time_block TEXT PRIMARY KEY,
            target_temp REAL
        )
    ''')

    try:
        cursor.execute("ALTER TABLE q_table ADD COLUMN visits INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass # Column already exists, safe to ignore
    conn.commit()
    conn.close()
    print("brain.db successfully initialized.")

def save_session_state(key, value):
    """Writes a piece of volatile state to the database."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute("INSERT OR REPLACE INTO session_state (key, value) VALUES (?, ?)", (key, str(value)))
            conn.commit()
    except Exception as e:
        print(f"⚠️ Session Save Error: {e}")

def get_session_state(key):
    """Retrieves a piece of state from the database."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT value FROM session_state WHERE key = ?", (key,))
            row = cursor.fetchone()
            return row[0] if row else None
    except Exception:
        return None

def get_last_known_state():
    """Reads the AI's last recorded action from the execution history."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()

            # --- FIX: Tell the SQL query to ignore dummy startup logs ---
            cursor.execute("SELECT target_temp, action_taken" \
            " FROM history_log WHERE action_taken != 'System Initialization'" \
            " ORDER BY id DESC LIMIT 1")

            row = cursor.fetchone()
            if row:
                return {"target_temp": float(row[0]), "action_taken": row[1]}
    except Exception as e:
        print(f"⚠️ Could not fetch last state from memory: {e}")
    return None

def update_q_score(time_block, temp_band, humidity_band, is_peak, action, final_reward):
    """Updates the Q-table with the finalized reward and increments the visit counter."""
    alpha = 0.5  # Learning Rate

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # --- 1. GET CURRENT SCORE & VISITS ---
    cursor.execute('''
        SELECT q_score, visits FROM q_table
        WHERE time_block = ? AND temp_band = ? AND humidity_band = ? AND is_peak_pricing = ? AND action_taken = ?
    ''', (time_block, temp_band, humidity_band, is_peak, action))
    row = cursor.fetchone()

    current_q = row[0] if row else 0.0
    # Handle older rows that might have a NULL visit count before the migration
    visits = row[1] if row and row[1] is not None else 0

    # --- 2. TEMPORAL DIFFERENCE MATH ---
    new_q = current_q + alpha * (final_reward - current_q)
    new_visits = visits + 1

    # --- 3. SAVE TO MATRIX ---
    cursor.execute('''
        INSERT OR REPLACE INTO q_table 
        (time_block, temp_band, humidity_band, is_peak_pricing, action_taken, q_score, visits)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    ''', (time_block, temp_band, humidity_band, is_peak, action, new_q, new_visits))

    conn.commit()
    conn.close()

    print(f"💾 Q-Table Updated | Block: {time_block} | Action: {action}")
    print(f"   ↳ New Q-Score: {new_q:.2f} | Total Days Experienced: {new_visits}")

    return new_q

def log_history(time_block: str, actual_temp: float, target_temp: float, actual_humidity: float,
                action_taken: str, kwh_consumed: float, user_overrides: int, reward_granted: float):
    """Saves performance data. Ensure brain.db was deleted recently to support 'target_temp'."""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        local_now = datetime.now().isoformat()
        cursor.execute('''
            INSERT INTO history_log (date_time, time_block, actual_temp, target_temp, actual_humidity, 
                                     action_taken, kwh_consumed, user_overrides, reward_granted)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (local_now, time_block, actual_temp, target_temp, actual_humidity,
              action_taken, kwh_consumed, user_overrides, reward_granted))
        conn.commit()
        conn.close()
    except sqlite3.OperationalError as e:
        print(f"Database error: {e}. (Did you delete brain.db to add the new column?)")

def get_scheduled_temp(time_block: str) -> float:
    """Reads the user's baseline schedule from the database."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Fetch the target temp for the specific time block
    cursor.execute('SELECT target_temp FROM schedule WHERE time_block = ?', (time_block,))
    result = cursor.fetchone()
    conn.close()

    # If the user hasn't set a schedule for this block, default to 72F
    if result is None:
        return 72.0

    return float(result[0])

def get_state_experience_count(time_block: str, temp_band: str, humid_band: str, is_peak: bool) -> int:
    """Counts how many days the AI has been graded in this EXACT weather state."""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute('''
            SELECT SUM(visits) FROM q_table 
            WHERE time_block = ? AND temp_band = ? AND humidity_band = ? AND is_peak_pricing = ?
        ''', (time_block, temp_band, humid_band, is_peak))
        total_visits = cursor.fetchone()[0]
        conn.close()
        return total_visits if total_visits else 0
    except Exception as e:
        print(f"⚠️ State Experience Check Error: {e}")
        return 0

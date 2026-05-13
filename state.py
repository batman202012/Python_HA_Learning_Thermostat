"""
state.py
Holds volatile application memory (RAM), terminal buffers, and JSON file I/O.
"""

import json
import os
from config import WAITING_ROOM_FILE

def save_waiting_room(pending_data):
    """Saves the pending block to the hard drive."""
    try:
        with open(WAITING_ROOM_FILE, "w", encoding="utf-8") as f:
            json.dump(pending_data, f)
    except Exception as e:
        print(f"⚠️ Failed to save waiting room: {e}")

def load_waiting_room():
    """Loads the pending block from the hard drive on boot."""
    if os.path.exists(WAITING_ROOM_FILE):
        try:
            with open(WAITING_ROOM_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"⚠️ Waiting room corrupted or empty: {e}")
    return None

def clear_waiting_room():
    """Deletes the waiting room file so we don't accidentally double-grade it."""
    if os.path.exists(WAITING_ROOM_FILE):
        try:
            os.remove(WAITING_ROOM_FILE)
        except Exception:
            pass

APP_STATE = {
    "expected_target_temp": None,
    "user_override_count": 0,
    "start_kwh": 0.0,
    "last_evaluated_minute": -1,
    "last_grade_run": None,
    "block_start_time": None,
    "target_reached_time": None,
    "active_block": None,      # Tracks which time block we are in
    "locked_action": None,     # The decision we are sticking with
    "locked_target": None,     # The temperature we are maintaining
    "current_band": None,       # The weather conditions when we decided
    "forecast_max_temp": None,
    "forecast_max_humidity": None,
    "pending_grade": load_waiting_room(),
    "last_f_temp": None,
    "last_f_humid": None
}

if APP_STATE["pending_grade"]:
    print(f"🔄 Recovered '{APP_STATE['pending_grade']['block']}' from the JSON waiting room.")

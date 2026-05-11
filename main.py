"""
Learning Thermostat Backend
Handles scheduling, Home Assistant integration, and Reinforcement Learning.
"""

import sqlite3
import asyncio
import json
from datetime import datetime, timedelta
from contextlib import asynccontextmanager
import httpx
import websockets
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
import uvicorn
import random
import os
from dotenv import load_dotenv
import sys
from collections import deque
import subprocess

terminal_buffer = deque(maxlen=100)

class ConsoleInterceptor:
    """Intercepts the systemctl logs to print to index.html"""
    def __init__(self, original_stdout):
        """Initiates the console output"""
        self.original_stdout = original_stdout

    def write(self, text):
        """Writes the text to the console output"""
        # 1. Still print to the actual machine terminal
        self.original_stdout.write(text)

        # 2. Filter out empty lines and Uvicorn API request spam
        clean_text = text.strip()
        if clean_text:
            # Uvicorn access logs usually contain "HTTP/1.1" or "GET /api"
            if "HTTP/1.1" not in clean_text and "GET /api" not in clean_text:
                terminal_buffer.append(clean_text)

    def flush(self):
        """Flushes the console output"""
        self.original_stdout.flush()

    def __getattr__(self, name):
        """Passes any unknown requests to the terminal"""
        # Pass any unknown requests (like .isatty()) to the original terminal
        return getattr(self.original_stdout, name)

# Hijack the standard output
sys.stdout = ConsoleInterceptor(sys.stdout)

# Load the hidden variables from the .env file
load_dotenv()
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, 'brain.db')

print(f"📂 Database localized to: {DB_PATH}")

# --- CONFIGURATION ---
HA_URL = "http://" + os.getenv("HA_ADD") + "/api/services/climate/set_temperature"
HA_TOKEN = os.getenv("HA_TOKEN")
HA_URL_FORECAST = "http://" + os.getenv("HA_ADD") + "/api/services/weather/get_forecasts?return_response"
HA_URL_STATE = "http://" + os.getenv("HA_ADD") + "/api/states/"
IP = os.getenv("IP")
PORT = int(os.getenv("PORT"))
THERMOSTAT_ENTITY_ID = os.getenv("THERMOSTAT_ENTITY_ID")
COOLING_ENERGY = os.getenv("COOLING_ENERGY_USAGE_SENSOR")
OUTSIDE_TEMP_SENSOR = os.getenv("OUTSIDE_TEMP_SENSOR")
OUTSIDE_HUMD_SENSOR = os.getenv("OUTSIDE_HUMD_SENSOR")
MET_IO_FORCAST = os.getenv("MET_IO_FORCAST")
WAITING_ROOM_FILE = os.path.join(BASE_DIR, 'waiting_room.json')

def save_waiting_room(pending_data):
    """Saves the pending block to the hard drive."""
    try:
        with open(WAITING_ROOM_FILE, "w") as f:
            json.dump(pending_data, f)
    except Exception as e:
        print(f"⚠️ Failed to save waiting room: {e}")

def load_waiting_room():
    """Loads the pending block from the hard drive on boot."""
    if os.path.exists(WAITING_ROOM_FILE):
        try:
            with open(WAITING_ROOM_FILE, "r") as f:
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

# --- STATE TRACKING ---
# Replaces global variables to keep Pylint happy and state organized.
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

# Maps the current block to the next sequential block
NEXT_BLOCK_MAP = {
    "Overnight": "Early Morning",
    "Early Morning": "Late Morning",
    "Late Morning": "Mid-Day",
    "Mid-Day": "Early Afternoon",
    "Early Afternoon": "Late Afternoon",
    "Late Afternoon": "Peak Hours",
    "Peak Hours": "Evening",
    "Evening": "Late Night",
    "Late Night": "Overnight"
}

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
            cursor.execute("SELECT target_temp, action_taken FROM history_log WHERE action_taken != 'System Initialization' ORDER BY id DESC LIMIT 1")

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

# --- HOME ASSISTANT ACTIONS ---
async def trigger_cooling(target_temp: float):
    """Sends a REST API call to HA to change the thermostat temperature."""
    APP_STATE["expected_target_temp"] = target_temp

    headers = {
        "Authorization": f"Bearer {HA_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "entity_id": THERMOSTAT_ENTITY_ID,
        "temperature": target_temp
    }
    async with httpx.AsyncClient() as client:
        response = await client.post(HA_URL, headers=headers, json=payload)
        return response.status_code

async def get_sensor_state(entity_id: str):
    """Gets the state of sensors from home assistant"""
    headers = {"Authorization": f"Bearer {HA_TOKEN}", "Content-Type": "application/json"}
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(f"{HA_URL_STATE}{entity_id}", headers=headers)
            if response.status_code == 200:
                data = response.json()
                state_val = data.get('state')
                # Check for non-numeric states common in HA
                if state_val in [None, 'unavailable', 'unknown', 'none']:
                    return None
                try:
                    return float(state_val)
                except (ValueError, TypeError):
                    return None
            return None
        except Exception:
            return None

async def get_current_indoor_temp() -> float:
    """Fetches the actual indoor temperature from the thermostat attributes."""
    headers = {
        "Authorization": f"Bearer {HA_TOKEN}",
        "Content-Type": "application/json"
    }

    try:
        async with httpx.AsyncClient() as client:
            # Hit the states endpoint for your specific thermostat
            response = await client.get(f"{HA_URL_STATE}" + THERMOSTAT_ENTITY_ID, headers=headers)

            if response.status_code == 200:
                data = response.json()
                # Dive into the attributes dictionary to pull the exact temperature
                current_temp = data.get("attributes", {}).get("current_temperature")

                if current_temp is not None:
                    return float(current_temp)
    except Exception as e:
        print(f"⚠️ API Error: {e}")
    return None

async def get_afternoon_forecast():
    """Fetches hourly forecast with the required return_response parameter."""
    headers = {
        "Authorization": f"Bearer {HA_TOKEN}",
        "Content-Type": "application/json"
    }
    # Ensure this matches your Met.no entity exactly
    payload = {
        "entity_id": MET_IO_FORCAST,
        "type": "hourly"
    }

    print("📡 Requesting forecast from weather.forecast_home...")
    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=10) as client:
            # The URL now contains ?return_response
                response = await client.post(HA_URL_FORECAST, headers=headers, json=payload)

                if response.status_code != 200:
                    print(f"❌ HA Forecast Error: {response.status_code} - {response.text}")
                    return []
                else:
                    data = response.json()
                    service_output = data.get("service_response", {})
                    entity_forecast = service_output.get(MET_IO_FORCAST, {})
                    forecasts = entity_forecast.get("forecast", [])
                    if forecasts:
                        print(f"✅ Success! Parsed {len(forecasts)} forecast points from service_response.")
                    else:
                        if isinstance(service_output, list):
                            forecasts = service_output
                            print(f"✅ Parsed {len(forecasts)} points from flat list.")
                        else:
                            print(f"⚠️ Keys in service_response: {list(service_output.keys())}")
                    return forecasts
                print(f"⚠️ Forecast Attempt {attempt+1} failed: {response.status_code}")
                await asyncio.sleep(2)
        except Exception as e:
            print(f"⚠️ Forecast Connection Attempt {attempt+1} failed: {e}")
            await asyncio.sleep(2)
    return []

# --- WEBSOCKET LISTENER ---
def get_override_count():
    """Returns the current override count and resets it for the next cycle."""
    current_count = APP_STATE["user_override_count"]
    APP_STATE["user_override_count"] = 0
    return current_count

def sync_ha_to_schedule(new_temp: float):
    """Updates the baseline schedule when a manual change is made in HA."""
    current_block = APP_STATE.get("active_block", "Mid-Day")

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO schedule (time_block, target_temp)
        VALUES (?, ?)
        ON CONFLICT(time_block) DO UPDATE SET target_temp = excluded.target_temp
    ''', (current_block, new_temp))
    conn.commit()
    conn.close()

    # CRITICAL: Update the live memory so the 5-minute loop doesn't overwrite this!
    APP_STATE["locked_target"] = new_temp
    print(f"🔄 Memory Synced: AI will now maintain {new_temp}°F for the rest of this block.")

async def handle_thermostat_change(state_data):
    """Parses HA state changes and strictly identifies manual overrides."""
    new_state = state_data.get("new_state")
    if not new_state:
        return

    new_temp_raw = new_state.get("attributes", {}).get("temperature")
    if new_temp_raw is None:
        return

    new_temp = float(new_temp_raw)
    expected = APP_STATE.get("expected_target_temp")

    # If there's no expected temp yet, or the new temp is different from the AI's last command
    if expected is None:
        print(f"📡 Initial Sync: Thermostat is at {new_temp}°F. Memory updated.")
        APP_STATE["expected_target_temp"] = float(new_temp)
        APP_STATE["locked_target"] = float(new_temp)
        return
    elif abs(new_temp - float(expected)) > 0.5:
        print(f"🚨 MANUAL OVERRIDE DETECTED: House set to {new_temp}°F")
        APP_STATE["is_manual_override"] = True
        now = datetime.now()
        # If the change happens within 15 mins of a block start, we ignore the 'penalty'
        # and just treat it as a new scheduled preference.
        APP_STATE["expected_target_temp"] = new_temp
        is_grace_period = now.minute < 15
        current_ai_action = APP_STATE.get("locked_action")
        if not is_grace_period:
            APP_STATE["user_override_count"] += 1
            # --- 1. THE Q-TABLE PENALTY ---
            # Make sure we don't punish an empty state or a state that is already manual
            if current_ai_action and current_ai_action not in ["Manual", "None"]:
                print(f"💥 WRIST SLAP: Applying a -20.0 penalty to AI strategy '{current_ai_action}'.")

                # Retrieve the environment state at the exact moment of failure
                # (Adjust these variable fetches to match how your script tracks them)
                time_block = APP_STATE.get("active_block", "Mid-Day")
                if time_block == "Peak Hours":
                    is_peak = 1
                else:
                    is_peak = 0
                f_temp = APP_STATE.get("last_f_temp", 75.0)
                f_humid = APP_STATE.get("last_f_humid", 20.0)
                
                # Fetch the peak temp from memory to accurately penalize the exact state
                peak_temp = APP_STATE.get("forecast_max_temp", None)
                temp_band, humid_band = get_state_bands(f_temp, f_humid, peak_temp)

                # Deliver the instant Bellman update
                update_q_score(time_block, temp_band, humid_band, is_peak, current_ai_action, -20.0)
        else:
            print("Adjustment: Logged without penalty.")

        # 3. Sync to DB and Memory
        sync_ha_to_schedule(new_temp)
    else:
        # This was an AI-driven change, so we ignore it for the override counter
        print(f"✅ Automated change to {new_temp}°F confirmed.")

async def listen_to_ha():
    """Maintains a persistent WebSocket connection to Home Assistant."""
    uri = f"ws://{os.getenv('HA_ADD')}/api/websocket"
    while True:
        try:
            async with websockets.connect(uri) as websocket:
                print("Connected to HA WebSocket")

                # 1. Read the initial "auth_required" greeting from HA
                await websocket.recv()

                # 2. Send our token
                await websocket.send(json.dumps({"type": "auth", "access_token": HA_TOKEN}))

                # 3. Read the actual authentication response
                auth_response = await websocket.recv()
                auth_data = json.loads(auth_response)

                if auth_data.get("type") != "auth_ok":
                    print(f"Authentication failed! HA says: {auth_data}")
                    return

                print("✅ Authentication successful")

                # 4. Subscribe to events...
                await websocket.send(json.dumps({
                    "id": 1,
                    "type": "subscribe_events",
                    "event_type": "state_changed"
                }))

                while True:
                    message = await websocket.recv()
                    data = json.loads(message)
                    if data.get("type") == "event":
                        event_data = data.get("event", {})
                        if event_data.get("event_type") == "state_changed":
                            entity_id = event_data.get("data", {}).get("entity_id")
                            if entity_id == THERMOSTAT_ENTITY_ID:
                                await handle_thermostat_change(event_data["data"])

        except websockets.exceptions.ConnectionClosed:
            print("Connection lost. Reconnecting in 5 seconds...")
            await asyncio.sleep(5)
        # pylint: disable=broad-exception-caught
        except Exception as e:
            print(f"WebSocket Error: {e}")
            await asyncio.sleep(5)

# --- LEARNING & SCHEDULING LOGIC ---
def calculate_reward(user_overrides: int, kwh_used: float, is_peak_pricing: bool):
    """Calculates the success score of a completed cycle."""
    reward = 0
    if user_overrides == 0:
        reward += 10
    else:
        reward -= (20 * user_overrides)

    cost_multiplier = 4.0 if is_peak_pricing else 2.5
    cost_penalty = kwh_used * cost_multiplier
    reward -= cost_penalty
    return reward

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

def get_state_bands(temp, humidity, peak_temp=None):
    """Converts the outside temp and humidity into bands for use in the reward function"""
    # Temp Bands: <75, 80, 85, 90, 95, 100, 105, 110
    t_list = [75, 80, 85, 90, 95, 100, 105, 110]
    t_band = "110+"
    if temp < 75:
        t_band = "<75"
    else:
        for val in t_list:
            if temp < val:
                t_band = f"{val-5}-{val}"
                break

    # Humidity Bands: <5, 10, 15, 20, 25, 30, 45, 60
    h_list = [5, 10, 15, 20, 25, 30, 45, 60]
    h_band = "60%+"
    if humidity < 5:
        h_band = "<5%"
    else:
        # Special handling for the 30-45 and 45-60 jumps
        for val in h_list:
            if humidity < val:
                if val == 45: h_band = "30-45%"
                elif val == 60: h_band = "45-60%"
                else: h_band = f"{val-5}-{val}%"
                break
    if peak_temp is not None:
        if peak_temp >= 105:
            forecast_band = "Threat: 105+"
        elif peak_temp >= 100:
            forecast_band = "Threat: 100-104"
        elif peak_temp >= 95:
            forecast_band = "Threat: 95-99"
        else:
            forecast_band = "Threat: <95"
    else:
        forecast_band = "Threat: None"

    # --- THE POMDP FIX ---
    # Attach the forecast directly to the temp band so the SQLite DB treats it 
    # as a unique memory state without needing an ALTER TABLE command.
    combined_temp_state = f"{t_band} [{forecast_band}]"

    return combined_temp_state, h_band

def get_best_q_action(time_block: str, forecast_temp: float, forecast_humidity: float,
                     is_peak_pricing: bool, baseline_temp: float, peak_temp) -> tuple[str, float]:
    """Calculates the best action for the current temp and humidity"""

    temp_band, humidity_band = get_state_bands(forecast_temp, forecast_humidity, peak_temp)

    # FIX: Use "in" to catch 'Early Morning', 'Late Morning', etc.
    if "Morning" in time_block or "Afternoon" in time_block or "Mid-Day" in time_block:
        available_actions = ["Normal", "Pre-cool 2°F", "Pre-cool 4°F"]
    else:
        # Evening/Night/Overnight toolkit
        available_actions = ["Normal", "Night Drop 2°F", "Eco Mode +2°F"]
    print(f"🔍 DEBUG X-RAY: Searching DB for -> Block: '{time_block}', Temp: '{temp_band}', Humid: '{humidity_band}', Peak: {is_peak_pricing}")


    # 2. Check the historical cheat sheet (Q-Table)
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        SELECT action_taken, q_score FROM q_table
        WHERE time_block = ? AND temp_band = ? AND humidity_band = ? AND is_peak_pricing = ?
    ''', (time_block, temp_band, humidity_band, is_peak_pricing))
    results = cursor.fetchall()
    conn.close()

    q_scores = {row[0]: row[1] for row in results}
    # Ensure all available actions are in the dictionary before we pick the max!
    for action in available_actions:
        if action not in q_scores:
            q_scores[action] = 0.0
    print(f"🔍 DEBUG X-RAY: Found in DB -> {q_scores}")

    # 3. Epsilon-Greedy Logic (15% chance to experiment)
    epsilon = 0.20
    is_exploring = random.random() < epsilon

    if not q_scores or is_exploring:
        untried = [a for a in available_actions if a not in q_scores]
        chosen_action = random.choice(untried) if untried else random.choice(available_actions)
        print(f"🧠 AI is EXPLORING: Trying '{chosen_action}'")
    else:
        chosen_action = max(q_scores, key=q_scores.get)
        print(f"🧠 AI is EXPLOITING: Using proven strategy '{chosen_action}'")

    # 4. Translate strategy to math
    if "4°F" in chosen_action:
        raw_target = baseline_temp - 4.0
    elif "+2°F" in chosen_action:
        raw_target = baseline_temp + 2.0
    elif "2°F" in chosen_action:
        raw_target = baseline_temp - 2.0
    else:
        raw_target = baseline_temp

    # --- SAFETY MAX/MIN CLAMP ---
    # Adjust these values to whatever your comfort limits are
    SAFETY_MIN = 68.0  # Never go below this, even if pre-cooling
    SAFETY_MAX = 78.0  # Never go above this, even in Eco Mode

    # This line ensures target_temp is NEVER lower than 68 and NEVER higher than 78
    target_temp = max(min(raw_target, SAFETY_MAX), SAFETY_MIN)

    # Log it if the safety kicked in so you know why it's not hitting the math
    if target_temp != raw_target:
        print(f"⚠️ Safety Clamp active: Adjusted {raw_target}°F to {target_temp}°F")

    return chosen_action, target_temp


async def evaluate_precooling():
    """Analyzes forecast with UTC-to-Local conversion."""
    print("🔍 Advisor is checking today's thermal outlook...")

    max_predicted_temp = 0.0
    peak_humidity = 0.0

    forecasts = await get_afternoon_forecast()
    if not forecasts:
        return None, None

    # Get today's date in local time
    today_local = datetime.now().date()

    for block in forecasts:
        dt_str = block.get('datetime', '')
        if not dt_str: continue

        # Convert UTC string to aware datetime object
        dt_utc = datetime.fromisoformat(dt_str.replace('Z', '+00:00'))

        # Shift UTC to Tucson time (MST is UTC-7)
        dt_local = dt_utc - timedelta(hours=7)

        # Only evaluate the upcoming afternoon (2 PM to 6 PM) for TODAY
        if dt_local.date() == today_local and 14 <= dt_local.hour <= 18:
            temp = float(block.get('temperature', 0.0))
            humidity = float(block.get('humidity', 0.0))

            if temp > max_predicted_temp:
                max_predicted_temp = temp
            if humidity > peak_humidity:
                peak_humidity = humidity

    # If we still have 0.0, we didn't find the window
    if max_predicted_temp == 0.0:
        print("⚠️ Advisor could not find a matching 2pm-6pm window for today.")
        return None, None

    # --- THE FIX: PUBLISH TO MEMORY ---
    # Save the forecasted max so the AI Q-Table can use it during morning blocks!
    APP_STATE["forecast_max_temp"] = max_predicted_temp
    APP_STATE["forecast_max_humidity"] = peak_humidity

    # Logic decision (The Hardcoded Panic Button)
    if max_predicted_temp > 95 and peak_humidity > 35:
        print(f"📢 Advisor: Muggy peak ({max_predicted_temp}°F, {peak_humidity}%). Suggesting 4°F pre-cool.")
        return "Pre-cool 4°F", max_predicted_temp
    elif max_predicted_temp > 95:
        print(f"📢 Advisor: Dry peak heat of {max_predicted_temp}°F. Suggesting 2°F pre-cool.")
        return "Pre-cool 2°F", max_predicted_temp

    print(f"☀️ Advisor: Forecast clear. (Max predicted: {max_predicted_temp}°F)")
    return None, None

def get_current_block_name():
    """Maps the current hour to a granular block."""
    hour = datetime.now().hour

    if 0 <= hour < 5: return "Overnight"
    if 5 <= hour < 8: return "Early Morning"
    if 8 <= hour < 10: return "Late Morning"
    if 10 <= hour < 12: return "Mid-Day"
    if 12 <= hour < 14: return "Early Afternoon"
    if 14 <= hour < 16: return "Late Afternoon"
    if 16 <= hour < 19: return "Peak Hours"  # 3-hour block to match utility peaks
    if 19 <= hour < 22: return "Evening"
    return "Late Night"

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

async def master_clock():
    """Master clock that monitors everything at 5 minutes intervals"""
    print("🕰️ High-Res Master Clock started. Monitoring 5 minute intervals.")
    f_temp = 75.0  # Initial default
    f_humid = 20.0 # Initial default

    while True:
        now = datetime.now()
        current_block = get_current_block_name()
        target_temp = APP_STATE.get("locked_target", 72.0)
        chosen_action = APP_STATE.get("locked_action", "Normal")

        # --- THE 5-MINUTE TELEMETRY LOOP ---
        if now.minute % 5 == 0 and APP_STATE["last_evaluated_minute"] != now.minute:
            APP_STATE["last_evaluated_minute"] = now.minute
            try:
                # A. Fetch Sensors
                indoor_temp = await get_current_indoor_temp()

                if indoor_temp is None:
                    # SENSOR FAILED LOGIC
                    print("⚠️ Skipping cycle: Could not verify indoor temperature.")
                    indoor_temp = 72.0 # Use this only for the 'display' so the app doesn't crash
                    is_temp_valid = False
                else:
                    is_temp_valid = True

                # Use a temporary variable for the raw sensor fetch
                raw_kwh = await get_sensor_state(COOLING_ENERGY)

                # If the sensor is None, use the start_kwh as a placeholder
                # so the 'running_kwh' equals 0.0 instead of crashing.
                if raw_kwh is None:
                    print(f"⚠️ Energy sensor {COOLING_ENERGY} unavailable. Using fallback.")
                    current_kwh = float(APP_STATE.get("start_kwh", 0.0))
                else:
                    current_kwh = float(raw_kwh)

                # B. Weather Fetch (Fail-safe against the 'float' error)
                try:
                    new_f_temp = await get_sensor_state(OUTSIDE_TEMP_SENSOR)
                    new_f_humid = await get_sensor_state(OUTSIDE_HUMD_SENSOR)

                    # --- 1. Handle Temperature Independently ---
                    if new_f_temp is not None and new_f_temp > 32.0:
                        f_temp = new_f_temp # Update with fresh data
                    else:
                        # If None (reloading), only hold if we already have a past value
                        if f_temp is not None:
                            print(f"📡 Temp Sensor busy. Holding last value: {f_temp}°F")
                        else:
                            # If it's the very first boot and it fails, use a hot fallback so we don't ignore the PC!
                            print("⚠️ Temp sensor unavailable on boot. Using 86.0°F fallback.")
                            f_temp = 86.0

                    # --- 2. Handle Humidity Independently ---
                    if new_f_humid is not None:
                        f_humid = new_f_humid
                    else:
                        if f_humid is not None:
                            print(f"📡 Humid Sensor busy. Holding last value: {f_humid}%")
                        else:
                            print("⚠️ Humid sensor unavailable. Using 20.0% fallback.")
                            f_humid = 20.0

                    # --- 3. Guard the Ambient Cooling check ---
                    is_ambient_cooling = False
                    # Check that target_temp actually exists BEFORE doing math on it
                    if target_temp is not None:
                        if 40.0 < f_temp < (target_temp - 4):
                            is_ambient_cooling = True
                            print(f"🌬️ Ambient Cooling Active: Outdoor {f_temp}°F")

                except Exception as e:
                    # Catch the actual error so you can see if it's a network drop or a typo
                    print(f"🛑 Critical error fetching outdoor sensors: {e}")
                    # Only force fallbacks if we absolutely have no past data to hold onto
                    if f_temp is None: f_temp = 86.0
                    if f_humid is None: f_humid = 20.0

                # --- C. BLOCK TRANSITION & INITIALIZATION ---
                is_startup = APP_STATE["active_block"] is None
                is_new_block = APP_STATE["active_block"] != current_block

                if is_startup or is_new_block:
                    if is_startup:
                        # 1. Check if the DB has data for the CURRENT block
                        db_active_block = get_session_state("active_block")

                        if db_active_block == current_block:
                            print(f"🔄 Reboot Recovery: Resuming {current_block} metrics from DB.")
                            APP_STATE["active_block"] = current_block
                            APP_STATE["start_kwh"] = float(get_session_state("start_kwh") or current_kwh)

                            # Recover start time
                            stored_time = get_session_state("block_start_time")
                            if stored_time:
                                APP_STATE["block_start_time"] = datetime.fromisoformat(stored_time)

                            # Recover "Target Reached" time so we don't get penalized again
                            stored_reached = get_session_state("target_reached_time")
                            if stored_reached:
                                APP_STATE["target_reached_time"] = datetime.fromisoformat(stored_reached)
                        else:
                            print(f"🆕 System start: No matching session found. Starting fresh for {current_block}.")
                    if not is_startup:
                        print(f"🚀 Transitioning to {current_block}")
                        current_indoor = await get_current_indoor_temp()
                        old_target = APP_STATE.get("locked_target", 72.0)

                        # Get the exact details of the block that JUST finished
                        finished_block = APP_STATE["active_block"]
                        is_peak = (finished_block == "Peak Hours")

                        # Pull the bands/action from memory before we overwrite them for the new block
                        finished_bands = APP_STATE.get("current_band", ("<75", "20-25%"))
                        finished_temp_band = finished_bands[0]
                        finished_humid_band = finished_bands[1]
                        finished_action = APP_STATE.get("locked_action", "Normal")

                        # 1. Grade the block that just finished (Make sure we AWAIT it and capture the return!)
                        current_immediate_reward = await grade_current_block(
                            finished_block,
                            current_indoor,
                            old_target,
                            is_peak
                        )

                        # 2. CHECK THE WAITING ROOM
                        pending = APP_STATE.get("pending_grade")

                        if pending:
                            gamma = 0.65
                            realized_future_bonus = gamma * current_immediate_reward
                            final_past_reward = pending["immediate_reward"] + realized_future_bonus

                            print(f"🕰️ Delayed Grading: Passing actual future physics ({realized_future_bonus:.1f}) back to {pending['block']}")

                            update_q_score(
                                pending["block"], pending["temp"], pending["humid"],
                                pending["peak"], pending["action"], final_past_reward
                            )

                            # --- CRITICAL CLEANUP ---
                            APP_STATE["pending_grade"] = None
                            clear_waiting_room()

                        # 3. PUT THE FINISHED BLOCK IN THE WAITING ROOM
                        if APP_STATE.get("is_manual_override"):
                            print("🛑 Human intervened. AI gets no future credit. Wiping waiting room.")
                            APP_STATE["pending_grade"] = None
                            clear_waiting_room()
                            APP_STATE["is_manual_override"] = False
                        else:
                            print(f"⏳ Placing '{finished_block}' into the JSON waiting room.")

                            pending_data = {
                                "block": finished_block,
                                "temp": finished_temp_band,
                                "humid": finished_humid_band,
                                "peak": is_peak,
                                "action": finished_action,
                                "immediate_reward": current_immediate_reward
                            }

                            APP_STATE["pending_grade"] = pending_data
                            APP_STATE["is_manual_override"] = False
                            save_waiting_room(pending_data)

                    # Set up the new block's stopwatch
                    APP_STATE["block_start_time"] = datetime.now()
                    APP_STATE["target_reached_time"] = None

                    # 1. Fetch the REAL TARGET (setpoint) from the thermostat attributes
                    headers = {"Authorization": f"Bearer {HA_TOKEN}", "Content-Type": "application/json"}
                    actual_thermostat_target = 73.0 # Safe default

                    try:
                        async with httpx.AsyncClient(timeout=10) as client:
                            response = await client.get(f"{HA_URL_STATE}" + THERMOSTAT_ENTITY_ID, headers=headers)
                            if response.status_code == 200:
                                data = response.json()
                                # Pull 'temperature' from attributes, NOT 'state'
                                ha_target = data.get("attributes", {}).get("temperature")
                                if ha_target:
                                    actual_thermostat_target = float(ha_target)
                                    print(f"🌡️ Live Thermostat Target detected: {actual_thermostat_target}°F")
                    except Exception as e:
                        print(f"⚠️ Could not fetch live target, using default: {e}")

                    # --- NEW: ATTEMPT MEMORY RECOVERY ---
                        last_state = get_last_known_state()

                        # If we have a past memory, AND the thermostat hasn't been manually
                        # changed by a human while the script was rebooting...
                        if last_state and abs(last_state["target_temp"] - actual_thermostat_target) < 0.5:
                            print(f"🧠 Memory Recovered! Restoring previous action: {last_state['action_taken']}")
                            APP_STATE["locked_target"] = last_state["target_temp"]
                            APP_STATE["locked_action"] = last_state["action_taken"]
                            APP_STATE["recovered_from_reboot"] = True
                        else:
                            print("🆕 Physical target changed while offline (or first boot). Treating as Manual Override.")
                            APP_STATE["locked_target"] = actual_thermostat_target
                            APP_STATE["recovered_from_reboot"] = False

                    else:
                        print(f"🚀 Transitioning to {current_block}")

                    # Reset block variables
                    APP_STATE["active_block"] = current_block
                    APP_STATE["start_kwh"] = current_kwh
                    APP_STATE["user_override_count"] = 0
                    save_session_state("active_block", current_block)
                    save_session_state("start_kwh", current_kwh)
                    save_session_state("block_start_time", datetime.now().isoformat())
                    save_session_state("target_reached_time", "") # Clear reached time for new block

                    # 1. RUN ADVISOR & FETCH BASELINE
                    forecast_rec = None
                    peak_temp = None
                    
                    try:
                        baseline = float(get_scheduled_temp(current_block))
                    except Exception as e:
                        print(f"⚠️ DB Error fetching schedule: {e}")
                        baseline = 75.0  # Safety net

                    try:
                        # Only check the future during morning/night prep blocks
                        if current_block in ["Overnight", "Early Morning", "Late Morning"]:
                            forecast_rec, peak_temp = await evaluate_precooling()
                    except Exception as e:
                        print(f"⚠️ Advisor Error: {e}")
                        forecast_rec = None
                        peak_temp = None

                    # 2. GET CURRENT STATE
                    # Now the AI explicitly knows if a heatwave is coming!
                    APP_STATE["current_band"] = get_state_bands(f_temp, f_humid, peak_temp)

                    # 3. PICK STRATEGY
                    if is_startup and APP_STATE.get("recovered_from_reboot"):
                        # Bypass the override rule because we recovered this from the database!
                        chosen_action = APP_STATE["locked_action"]
                        target_temp = APP_STATE["locked_target"]
                        print(f"🔄 Resuming recovered strategy: {chosen_action} @ {target_temp}°F")

                    elif is_startup and APP_STATE.get("locked_target") is not None:
                        # This triggers if memory recovery failed, meaning a human actually DID change it
                        print(f"🙌 Respecting recent manual adjustment: {APP_STATE['locked_target']}°F")
                        chosen_action = "Manual/Baseline"
                        target_temp = APP_STATE["locked_target"]

                    else:
                        # Normal AI Q-Table logic...
                        is_peak = current_block == "Peak Hours"
                        peak_temp = APP_STATE.get("forecast_max_temp", 92.0)
                        chosen_action, target_temp = get_best_q_action(current_block, f_temp, f_humid, is_peak, baseline, peak_temp)

                        if current_block in ["Early Morning", "Late Morning"]:
                            # If it's morning, intercept the search and use the afternoon heat instead.
                            ai_search_temp = APP_STATE.get("forecast_max_temp", 92.0)
                            ai_search_humid = APP_STATE.get("forecast_max_humidity", 25.0)
                            print(f"🔭 Morning Foresight Active: AI prepping for {ai_search_temp}°F afternoon.")
                            chosen_action, target_temp = get_best_q_action(current_block, ai_search_temp, ai_search_humid, is_peak, baseline, peak_temp)

                        # 4. APPLY SMART ADVISOR OVERRIDE
                        temp_band, humid_band = get_state_bands(f_temp, f_humid, peak_temp)

                        # Find out what the AI thinks of 'Normal' right now
                        conn = sqlite3.connect(DB_PATH)
                        cursor = conn.cursor()
                        cursor.execute('''
                            SELECT q_score FROM q_table
                            WHERE time_block = ? AND temp_band = ? AND humidity_band = ? AND is_peak_pricing = ? AND action_taken = 'Normal'
                        ''', (current_block, temp_band, humid_band, is_peak))
                        row = cursor.fetchone()
                        conn.close()

                        ai_score_normal = row[0] if row else 0.0

                        # Ask the DB how many times the AI has seen this exact weather
                        experience_count = get_state_experience_count(current_block, temp_band, humid_band, is_peak)

                        if forecast_rec and chosen_action == "Normal":
                            # I lowered the threshold to 3. Getting 3 days of exact 95F+ weather
                            # is enough data to trust the Q-score.
                            if experience_count < 3:
                                print(f"🎓 Advisor: AI only has {experience_count} days of '{temp_band}' weather. Training wheels ON. Forcing {forecast_rec}.")
                                chosen_action = forecast_rec
                                if "2°F" in forecast_rec: target_temp = baseline - 2.0
                                if "4°F" in forecast_rec: target_temp = baseline - 4.0

                            elif ai_score_normal <= -10.0:
                                print(f"🛡️ Advisor: AI agrees 'Normal' is bad (Score: {ai_score_normal:.1f}). Vetoing and forcing {forecast_rec}.")
                                chosen_action = forecast_rec
                                if "2°F" in forecast_rec: target_temp = baseline - 2.0
                                if "4°F" in forecast_rec: target_temp = baseline - 4.0

                            else:
                                print(f"🧠 Advisor: AI is a veteran ({experience_count} hot days) and 'Normal' score is safe ({ai_score_normal:.1f}). Letting AI take the wheel!")

                    # 4. LOCK IT IN
                    APP_STATE["locked_action"] = chosen_action
                    APP_STATE["locked_target"] = target_temp
                    APP_STATE["current_band"] = get_state_bands(f_temp, f_humid, peak_temp)

                target_temp = APP_STATE.get("locked_target", 72.0)
                if target_temp is None:
                    target_temp = 75.0
                if is_temp_valid and APP_STATE["target_reached_time"] is None:
                    if indoor_temp <= target_temp:
                        reached_now = datetime.now()
                        APP_STATE["target_reached_time"] = reached_now
                        # Persist it!
                        save_session_state("target_reached_time", reached_now.isoformat())
                        print(f"⏱️ Target reached at {reached_now.strftime('%H:%M:%S')}")
                # D. EXECUTE & LOG
                running_kwh = float(current_kwh) - float(APP_STATE.get("start_kwh", 0.0))
                APP_STATE["expected_target_temp"] = float(target_temp)
                asyncio.create_task(trigger_cooling(target_temp))
                is_ambient_cooling = False
                if f_temp > 40.0 and f_temp < (target_temp - 4):
                    is_ambient_cooling = True
                    print(f"🌬️ Ambient Cooling Active: Outdoor {f_temp}°F is 4°+ below Target {target_temp}°F.")

                # Update the action name for the log so you can see it in the dashboard
                display_action = chosen_action
                if is_ambient_cooling:
                    display_action = f"{chosen_action} (Fan Only)"

                APP_STATE["last_f_temp"] = f_temp
                APP_STATE["last_f_humid"] = f_humid
                log_history(
                    current_block, indoor_temp, target_temp, f_humid,
                    display_action, max(0, running_kwh), APP_STATE.get("user_override_count", 0), 0.0
                )
                print(f"✅ 5-minute log successful. ({chosen_action} @ {target_temp}°F)")

            except Exception as e:
                print(f"❌ CRITICAL ERROR IN MASTER CLOCK: {e}")

        await asyncio.sleep(1)

async def grade_current_block(block_name, actual_temp, target_temp, is_peak: bool):
    """Calculates the block reward and returns it for delayed processing."""
    print(f"📝 Grading the {block_name} block...")

    # Initialize a default reward in case of failure
    reward = 0.0

    try:
        # 1. Fetch Sensors
        f_temp = await get_sensor_state(OUTSIDE_TEMP_SENSOR)
        if f_temp is None: f_temp = 75.0

        f_humid = await get_sensor_state(OUTSIDE_HUMD_SENSOR)
        if f_humid is None: f_humid = 20.0

        raw_current_kwh = await get_sensor_state(COOLING_ENERGY)
        start_kwh = APP_STATE.get("start_kwh", 0.0)

        # 2. Safe Math: Handle energy data
        if raw_current_kwh is None:
            print(f"⚠️ Could not grade {block_name} energy accurately.")
            actual_kwh_used = 0.0
        else:
            actual_kwh_used = float(raw_current_kwh) - float(start_kwh)

        if actual_kwh_used < 0:
            actual_kwh_used = 0.0

        # 2.5 TIME-TO-TEMPERATURE PENALTY
        max_minutes = 120.0
        time_Weight_Factor = 3.0

        block_start = APP_STATE.get("block_start_time")
        target_reached = APP_STATE.get("target_reached_time")

        if block_start and target_reached:
            time_diff = target_reached - block_start
            minutes_taken = time_diff.total_seconds() / 60.0
        else:
            minutes_taken = max_minutes
            print("⚠️ Target never reached in this block. Applying max time penalty.")

        time_penalty = (minutes_taken / max_minutes) * time_Weight_Factor
        print(f"⏱️ Time Taken: {minutes_taken:.1f} mins | Time Penalty: -{time_penalty:.2f}")
        block_duration = (datetime.now() - block_start).total_seconds() / 60.0
        if block_duration < 30:
            print("⚠️ Block too short for fair grading (Restart detected). Skipping time penalty.")
            time_penalty = 0.0

        # 3. Reward Calculation
        overrides = APP_STATE.get("user_override_count", 0)
        base_reward = calculate_reward(overrides, kwh_used=actual_kwh_used, is_peak_pricing=is_peak)

        # Final score for this 2-hour window
        reward = base_reward - time_penalty

        print(f"✅ {block_name} calculation complete. Immediate Reward: {reward:.2f} | kWh: {actual_kwh_used:.2f}")

    except Exception as e:
        print(f"❌ Error calculating grade for {block_name}: {e}")
        reward = -5.0 # Penalty for failing to provide data

    # CRITICAL: Hand the number back to the master_clock!
    return float(reward)

# --- FASTAPI SETUP ---
@asynccontextmanager
async def lifespan(fastapi_app: FastAPI): # Renamed to avoid shadowing outer 'app'
    """Manages background tasks during the application lifecycle."""
    initialize_brain()
    print("🚀 Booting up Thermostat Brain...")
    ha_listener_task = asyncio.create_task(listen_to_ha())
    clock_task = asyncio.create_task(master_clock())

    yield

    print("🛑 Shutting down. Canceling background tasks...")
    ha_listener_task.cancel()
    clock_task.cancel()

app = FastAPI(lifespan=lifespan)
templates = Jinja2Templates(directory="templates")

# --- WEB ROUTES ---
@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    """Serves the main frontend dashboard."""
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={"schedule": []}
    )

@app.post("/api/schedule")
async def update_schedule(time_block: str, target_temp: float):
    """API Endpoint to manually update the cooling schedule."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute('''
        INSERT INTO schedule (time_block, target_temp)
        VALUES (?, ?)
        ON CONFLICT(time_block) DO UPDATE SET target_temp = excluded.target_temp
    ''', (time_block, target_temp))

    conn.commit()
    conn.close()

    print(f"💾 Schedule saved: {time_block} set to {target_temp}°F")
    return {"status": "success"}

@app.get("/api/q_table")
async def get_q_table():
    """Fetches the current learned scores."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT time_block, temp_band, humidity_band, is_peak_pricing, action_taken, q_score FROM q_table ORDER BY q_score DESC")
    columns = [column[0] for column in cursor.description]
    results = [dict(zip(columns, row)) for row in cursor.fetchall()]
    conn.close()
    return {"data": results}

@app.get("/api/history")
async def get_history():
    """Fetches the recent execution history."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT date_time, time_block, actual_temp, target_temp, actual_humidity, action_taken, user_overrides, reward_granted FROM history_log ORDER BY id DESC LIMIT 700")
    columns = [column[0] for column in cursor.description]
    results = [dict(zip(columns, row)) for row in cursor.fetchall()]
    conn.close()
    return {"data": results}

@app.get("/api/logs")
def get_terminal_logs():
    """Returns the cleanly formatted terminal output from memory."""
    # Join the last 100 logs with line breaks
    return {"logs": "\n".join(terminal_buffer)}

@app.post("/api/restart")
async def restart_service():
    """Triggers a systemd restart in the background."""
    print("🔄 Restart command received from UI. Rebooting system...")
    # The 'sleep 1' gives the web response time to reach your browser
    # before the process is killed.
    cmd = "sleep 1 && sudo /usr/bin/systemctl restart thermostat.service"
    subprocess.Popen(cmd, shell=True)
    return {"message": "Restarting system... Dashboard will reconnect shortly."}

if __name__ == "__main__":
    uvicorn.run(app, host=IP, port=PORT, access_log=False)

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

# --- STATE TRACKING ---
# Replaces global variables to keep Pylint happy and state organized.
APP_STATE = {
    "expected_target_temp": None,
    "user_override_count": 0,
    "start_kwh": 0.0,
    "last_evaluated_minute": -1,
    "last_grade_run": None,
    "active_block": None,      # Tracks which time block we are in
    "locked_action": None,     # The decision we are sticking with
    "locked_target": None,     # The temperature we are maintaining
    "current_band": None       # The weather conditions when we decided
}

# --- DATABASE SETUP ---
def initialize_brain():
    """Creates the SQLite database and necessary tables if they do not exist."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

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

    # ---------------------------------------------------------
    # TABLE 3: The Manual Schedule
    # Stores the user's baseline temperature preferences.
    # ---------------------------------------------------------
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS schedule (
            time_block TEXT PRIMARY KEY,
            target_temp REAL
        )
    ''')
    conn.commit()
    conn.close()
    print("brain.db successfully initialized.")

def update_q_table(time_block, temp_band, humidity_band, is_peak_pricing, action_taken, reward):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO q_table (time_block, temp_band, humidity_band, is_peak_pricing, action_taken, q_score)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(time_block, temp_band, humidity_band, is_peak_pricing, action_taken)
        DO UPDATE SET q_score = q_score + excluded.q_score
    ''', (time_block, temp_band, humidity_band, is_peak_pricing, action_taken, reward))
    conn.commit() # <--- THIS LINE IS CRITICAL
    conn.close()
    print(f"Brain updated: {action_taken} yielded a score of {reward}")

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
    
    async with httpx.AsyncClient() as client:
        # Hit the states endpoint for your specific thermostat
        response = await client.get(f"{HA_URL_STATE}" + THERMOSTAT_ENTITY_ID, headers=headers)
        
        if response.status_code == 200:
            data = response.json()
            # Dive into the attributes dictionary to pull the exact temperature
            current_temp = data.get("attributes", {}).get("current_temperature")
            
            if current_temp is not None:
                return float(current_temp)
                
    # Fallback to 72.0 if the API fails or HA is rebooting
    print("Warning: Could not fetch indoor temp. Defaulting to 72.0°F")
    return 72.0

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
    now = datetime.now()
    
    # Determine which block to update based on the current time
    if 6 <= now.hour < 12:
        current_block = "Morning"
    elif 12 <= now.hour < 18:
        current_block = "Afternoon"
    else:
        current_block = "Evening"

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
        now = datetime.now()
        # If the change happens within 15 mins of a block start, we ignore the 'penalty'
        # and just treat it as a new scheduled preference.
        APP_STATE["expected_target_temp"] = new_temp
        is_grace_period = now.minute < 15 
        if not is_grace_period:
            APP_STATE["user_override_count"] += 1
            print(f"🚨 MANUAL OVERRIDE: Penalty applied.")
        else:
            print(f"Adjustment: Logged without penalty.")

        # 3. Sync to DB and Memory
        sync_ha_to_schedule(new_temp)
    else:
        # This was an AI-driven change, so we ignore it for the override counter
        print(f"✅ Automated change to {new_temp}°F confirmed.")

async def listen_to_ha():
    """Maintains a persistent WebSocket connection to Home Assistant."""
    uri = "ws://192.168.86.27:8123/api/websocket"
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

    cost_multiplier = 4.0 if is_peak_pricing else 1.0
    cost_penalty = (kwh_used * cost_multiplier)
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

def get_state_bands(temp, humidity):
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
                
    return t_band, h_band

def get_best_q_action(time_block: str, forecast_temp: float, forecast_humidity: float, 
                     is_peak_pricing: bool, baseline_temp: float) -> tuple[str, float]:
    
    temp_band, humidity_band = get_state_bands(forecast_temp, forecast_humidity)
    
    # FIX: Use "in" to catch 'Early Morning', 'Late Morning', etc.
    if "Morning" in time_block or "Afternoon" in time_block or "Mid-Day" in time_block:
        available_actions = ["Normal", "Pre-cool 2°F", "Pre-cool 4°F"]
    else:
        # Evening/Night/Overnight toolkit
        available_actions = ["Normal", "Night Drop 2°F", "Eco Mode +2°F"]

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
    if "2°F" in chosen_action and "Drop" in chosen_action or "Pre-cool" in chosen_action:
        target_temp = baseline_temp - 2.0
    elif "4°F" in chosen_action:
        target_temp = baseline_temp - 4.0
    elif "+2°F" in chosen_action:
        target_temp = baseline_temp + 2.0
    else:
        target_temp = baseline_temp

    return chosen_action, target_temp

    
async def evaluate_precooling():
    """Analyzes forecast with UTC-to-Local conversion."""
    print("🔍 Advisor is checking today's thermal outlook...")
    
    max_predicted_temp = 0.0
    peak_humidity = 0.0
    
    forecasts = await get_afternoon_forecast()
    if not forecasts:
        return None

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
        return None

    # Logic decision
    if max_predicted_temp > 95 and peak_humidity > 35:
        print(f"📢 Advisor: Muggy peak ({max_predicted_temp}°F, {peak_humidity}%). Suggesting 4°F pre-cool.")
        return "Pre-cool 4°F"
    elif max_predicted_temp > 95:
        print(f"📢 Advisor: Dry peak heat of {max_predicted_temp}°F. Suggesting 2°F pre-cool.")
        return "Pre-cool 2°F"
    
    print(f"☀️ Advisor: Forecast clear. (Max predicted: {max_predicted_temp}°F)")
    return None

def get_current_block_name():
    """Maps the current hour to a 2-hour granular block."""
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

async def master_clock():
    # 1. Startup Diagnostics
    print("🚀 Injecting startup log for verification...")
    log_history("Startup", 70.0, 70.0, 50.0, "System Initialization", 0.0, 0, 0.0)
    print("🕰️ High-Res Master Clock started. Monitoring 2-hour intervals.")

    while True:
        now = datetime.now()
        current_block = get_current_block_name()
        
        # --- THE 5-MINUTE TELEMETRY LOOP ---
        if now.minute % 5 == 0 and APP_STATE["last_evaluated_minute"] != now.minute:
            APP_STATE["last_evaluated_minute"] = now.minute
            try:
                # A. Fetch Sensors
                indoor_temp = await get_current_indoor_temp()
                current_kwh = await get_sensor_state(COOLING_ENERGY)
                
                # B. Weather Fetch (Fail-safe against the 'float' error)
                try:
                    f_temp = await get_sensor_state(OUTSIDE_TEMP_SENSOR)
                    f_humid = await get_sensor_state(OUTSIDE_HUMD_SENSOR)
                    
                    if f_temp is None or f_temp < 32.0 or f_humid is None:
                        print("⚠️ Outdoor sensors unavailable. Using Tucson defaults (75.0°F, 20.0%)")
                        f_temp, f_humid = 75.0, 20.0
                    # C. Guard the Ambient Cooling check
                    is_ambient_cooling = False
                    # Only active if it's actually cool but NOT freezing/glitched
                    if 40.0 < f_temp < (target_temp - 4):
                        is_ambient_cooling = True
                        print(f"🌬️ Ambient Cooling Active: Outdoor {f_temp}°F")
                except:
                    # Fallback if specific sensors fail
                    f_temp, f_humid = 72.0, 20.0

                # --- C. BLOCK TRANSITION & INITIALIZATION ---
                is_startup = (APP_STATE["active_block"] is None)
                is_new_block = (APP_STATE["active_block"] != current_block)

                if is_startup or is_new_block:
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

                    if is_startup:
                        print(f"📡 Initializing AI state for {current_block}...")
                        APP_STATE["locked_target"] = actual_thermostat_target
                    else:
                        print(f"🚀 Transitioning to {current_block}")
                        await grade_current_block(APP_STATE["active_block"], (APP_STATE["active_block"] == "Peak Hours"))

                    # Reset block variables
                    APP_STATE["active_block"] = current_block
                    APP_STATE["start_kwh"] = current_kwh
                    APP_STATE["user_override_count"] = 0
                    
                    # 1. RUN ADVISOR
                    forecast_rec = await evaluate_precooling()
                    baseline = get_scheduled_temp(current_block)

                    # 2. PICK STRATEGY
                    # Check if a manual override was ALREADY set in the last 5 mins (e.g. at 10:04)
                    if is_startup and APP_STATE.get("locked_target") is not None:
                        print(f"🙌 Respecting recent manual adjustment: {APP_STATE['locked_target']}°F")
                        action = "Manual/Baseline"
                        target = APP_STATE["locked_target"]
                    else:
                        is_peak = (current_block == "Peak Hours")
                        action, target = get_best_q_action(current_block, f_temp, f_humid, is_peak, baseline)
                        
                        # 3. APPLY ADVISOR OVERRIDE
                        if forecast_rec and action == "Normal":
                            print(f"🧠 Advisor override: Switching 'Normal' to {forecast_rec}")
                            action = forecast_rec
                            if "2°F" in forecast_rec: target = baseline - 2.0
                            if "4°F" in forecast_rec: target = baseline - 4.0

                    # 4. LOCK IT IN
                    APP_STATE["locked_action"] = action
                    APP_STATE["locked_target"] = target
                    APP_STATE["current_band"] = get_state_bands(f_temp, f_humid)

                # D. EXECUTE & LOG
                chosen_action = APP_STATE.get("locked_action", "Normal")
                target_temp = APP_STATE.get("locked_target", 72.0)
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
                
                log_history(
                    current_block, indoor_temp, target_temp, f_humid, 
                    display_action, max(0, running_kwh), APP_STATE.get("user_override_count", 0), 0.0
                )
                print(f"✅ 5-minute log successful. ({chosen_action} @ {target_temp}°F)")
                await asyncio.sleep(61)
                continue

            except Exception as e:
                print(f"❌ CRITICAL ERROR IN MASTER CLOCK: {e}")

        await asyncio.sleep(1)

async def grade_current_block(block_name: str, is_peak: bool):
    """Generic grading function for any time block."""
    print(f"📝 Grading the {block_name} block...")
    try:
        # 1. Fetch LIVE weather for the grade, don't rely on the 'locked' band
        try:
            f_temp = await get_sensor_state(OUTSIDE_TEMP_SENSOR)
            f_humid = await get_sensor_state(OUTSIDE_HUMD_SENSOR)
        except:
            f_temp, f_humid = 72.0, 20.0 # Emergency fallback

        # 2. Calculate the actual band right now
        current_t_band, current_h_band = get_state_bands(f_temp, f_humid)
        current_kwh = await get_sensor_state(COOLING_ENERGY)
        start_kwh = APP_STATE.get("start_kwh", current_kwh)
        actual_kwh_used = float(current_kwh) - float(start_kwh)
        if actual_kwh_used < 0: actual_kwh_used = 0.0

        overrides = APP_STATE.get("user_override_count", 0)
        reward = calculate_reward(overrides, kwh_used=actual_kwh_used, is_peak_pricing=is_peak)
        
        state_band = APP_STATE.get("current_band", ("90-100", "Dry (<35%)"))
        action = APP_STATE.get("locked_action", "Normal")
        
        update_q_table(block_name, current_t_band, current_h_band, is_peak, action, reward)
        print(f"✅ {block_name} Graded. Reward: {reward:.2f} | kWh: {actual_kwh_used:.2f} | Overrides: {overrides}")
        
    except Exception as e:
        print(f"❌ Error grading {block_name}: {e}")

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
    # Added target_temp to the SELECT query below
    cursor.execute("SELECT date_time, time_block, actual_temp, target_temp, actual_humidity, action_taken, user_overrides, reward_granted FROM history_log ORDER BY id DESC LIMIT 15")
    columns = [column[0] for column in cursor.description]
    results = [dict(zip(columns, row)) for row in cursor.fetchall()]
    conn.close()
    return {"data": results}

if __name__ == "__main__":
    uvicorn.run(app, host=IP, port=PORT)

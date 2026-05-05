"""
Learning Thermostat Backend
Handles scheduling, Home Assistant integration, and Reinforcement Learning.
"""

import sqlite3
import asyncio
import json
from datetime import datetime
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

# --- CONFIGURATION ---
HA_URL = "http://" + os.getenv("HA_ADD") + "/api/services/climate/set_temperature"
HA_TOKEN = os.getenv("HA_TOKEN")
HA_URL_FORECAST = "http://" + os.getenv("HA_ADD") + "/api/services/weather/get_forecasts"
HA_URL_STATE = "http://" + os.getenv("HA_ADD") + "/api/states/"
THERMOSTAT_ENTITY_ID = os.getenv("THERMOSTAT_ENTITY_ID")

# --- STATE TRACKING ---
# Replaces global variables to keep Pylint happy and state organized.
APP_STATE = {
    "expected_target_temp": None,
    "user_override_count": 0,
    "last_precool_run": None,
    "last_learning_run": None
}

# --- DATABASE SETUP ---
def initialize_brain():
    """Creates the SQLite database and necessary tables if they do not exist."""
    conn = sqlite3.connect('brain.db')
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

def update_q_table(time_block: str, temp_band: str, humidity_band: str,
                   is_peak_pricing: bool, action_taken: str, reward: float):
    """Updates the reinforcement learning score for a specific state/action combo."""
    conn = sqlite3.connect('brain.db')
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO q_table (time_block, temp_band, humidity_band, is_peak_pricing, action_taken, q_score)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(time_block, temp_band, humidity_band, is_peak_pricing, action_taken)
        DO UPDATE SET q_score = q_score + excluded.q_score
    ''', (time_block, temp_band, humidity_band, is_peak_pricing, action_taken, reward))
    conn.commit()
    conn.close()
    print(f"Brain updated: {action_taken} in {temp_band} heat yielded a score of {reward}")

def log_history(time_block: str, actual_temp: float, target_temp: float, actual_humidity: float, 
                action_taken: str, kwh_consumed: float, user_overrides: int, reward_granted: float):
    """Saves the current performance to the database for the frontend graph."""
    conn = sqlite3.connect('brain.db')
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO history_log (time_block, actual_temp, target_temp, actual_humidity, action_taken, kwh_consumed, user_overrides, reward_granted)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    ''', (time_block, actual_temp, target_temp, actual_humidity, action_taken, kwh_consumed, user_overrides, reward_granted))
    conn.commit()
    conn.close()

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
    """Fetches the current state of any HA entity."""
    headers = {
        "Authorization": f"Bearer {HA_TOKEN}",
        "Content-Type": "application/json"
    }
    async with httpx.AsyncClient() as client:
        response = await client.get(f"{HA_URL_STATE}{entity_id}", headers=headers)
        if response.status_code == 200:
            data = response.json()
            return float(data['state'])
        return 0.0

async def get_afternoon_forecast():
    """Fetches the hourly weather forecast from HA."""
    headers = {
        "Authorization": f"Bearer {HA_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "entity_id": "weather.forecast_home",
        "type": "hourly"
    }
    async with httpx.AsyncClient() as client:
        response = await client.post(HA_URL_FORECAST, headers=headers, json=payload)
        data = response.json()
        forecasts = data.get("weather.forecast_home", {}).get("forecast", [])
        return forecasts

# --- WEBSOCKET LISTENER ---
def get_override_count():
    """Returns the current override count and resets it for the next cycle."""
    current_count = APP_STATE["user_override_count"]
    APP_STATE["user_override_count"] = 0
    return current_count

async def handle_thermostat_change(state_data):
    """Parses HA state changes to detect manual user overrides."""
    old_state = state_data.get("old_state")
    new_state = state_data.get("new_state")

    if not old_state or not new_state:
        return

    old_temp = old_state.get("attributes", {}).get("temperature")
    new_temp = new_state.get("attributes", {}).get("temperature")

    if old_temp != new_temp and new_temp is not None:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Target changed: {old_temp}°F -> {new_temp}°F")

        expected = APP_STATE["expected_target_temp"]
        if expected is None or float(new_temp) != float(expected):
            print("🚨 USER OVERRIDE DETECTED!")
            APP_STATE["user_override_count"] += 1
            APP_STATE["expected_target_temp"] = float(new_temp)
        else:
            print("✅ Automated change confirmed. (App initiated)")

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
    conn = sqlite3.connect('brain.db')
    cursor = conn.cursor()
    
    # Fetch the target temp for the specific time block
    cursor.execute('SELECT target_temp FROM schedule WHERE time_block = ?', (time_block,))
    result = cursor.fetchone()
    conn.close()
    
    # If the user hasn't set a schedule for this block, default to 75F
    if result is None:
        return 72.0 
    
    return float(result[0])

def get_state_bands(forecast_temp: float, forecast_humidity: float):
    """Translates raw weather numbers into the buckets used by our Q-Table."""
    if forecast_temp < 80:
        temp_band = "<80"
    elif 80 <= forecast_temp < 90:
        temp_band = "80-90"
    elif 90 <= forecast_temp < 100:
        temp_band = "90-100"
    else:
        temp_band = "100+"

    humidity_band = "Muggy (>35%)" if forecast_humidity > 35 else "Dry (<35%)"
    return temp_band, humidity_band

def get_best_q_action(time_block: str, forecast_temp: float, forecast_humidity: float, 
                      is_peak_pricing: bool, baseline_temp: float) -> tuple[str, float]:
    """
    The Brain: Balances exploration vs. exploitation to choose the best cooling strategy.
    Returns a tuple: (Action Name, Target Temperature)
    """
    temp_band, humidity_band = get_state_bands(forecast_temp, forecast_humidity)
    
    # 1. Define our toolkit: Actions are relative to whatever schedule the user set.
    available_actions = ["Normal", "Pre-cool 2°F", "Pre-cool 4°F"]

    # 2. Check the historical cheat sheet (Q-Table)
    conn = sqlite3.connect('brain.db')
    cursor = conn.cursor()
    cursor.execute('''
        SELECT action_taken, q_score FROM q_table
        WHERE time_block = ? AND temp_band = ? AND humidity_band = ? AND is_peak_pricing = ?
    ''', (time_block, temp_band, humidity_band, is_peak_pricing))
    results = cursor.fetchall()
    conn.close()

    # Format results into a dictionary { "Normal": 10.5, "Pre-cool 2°F": 25.0 }
    q_scores = {row[0]: row[1] for row in results}

    # 3. Epsilon-Greedy Logic (15% chance to experiment)
    epsilon = 0.15 
    is_exploring = random.random() < epsilon

    if not q_scores or is_exploring:
        # Pick an action we haven't tried yet, or a random one if all have been tried
        untried = [a for a in available_actions if a not in q_scores]
        chosen_action = random.choice(untried) if untried else random.choice(available_actions)
        print(f"🧠 AI is EXPLORING: Trying '{chosen_action}' to learn its effects.")
    else:
        # Pick the action with the absolute highest historical score
        chosen_action = max(q_scores, key=q_scores.get)
        print(f"🧠 AI is EXPLOITING: Using proven strategy '{chosen_action}' (Score: {q_scores[chosen_action]:.1f})")

    # 4. Translate the chosen strategy into an actual temperature command
    if chosen_action == "Pre-cool 4°F":
        target_temp = baseline_temp - 4.0
    elif chosen_action == "Pre-cool 2°F":
        target_temp = baseline_temp - 2.0
    else:
        target_temp = baseline_temp

    return chosen_action, target_temp

async def evaluate_precooling():
    """Analyzes forecast to determine if early pre-cooling is necessary."""
    forecasts = await get_afternoon_forecast()
    today = datetime.now().date()
    peak_heat_detected = False
    high_humidity_detected = False
    max_predicted_temp = 0
    peak_humidity = 0

    for block in forecasts:
        dt = datetime.fromisoformat(block['datetime'])
        if dt.date() == today and 14 <= dt.hour <= 18:
            temp = block.get('temperature', 0)
            humidity = block.get('humidity', 0)

            if temp > max_predicted_temp:
                max_predicted_temp = temp
            if humidity > peak_humidity:
                peak_humidity = humidity

            if temp > 95:
                peak_heat_detected = True
            if humidity > 35:
                high_humidity_detected = True

    if peak_heat_detected and high_humidity_detected:
        print(f"Alert: Muggy afternoon ({max_predicted_temp}°F, {peak_humidity}% humidity).")
        await trigger_cooling(target_temp=68.0)
    elif peak_heat_detected:
        print(f"Alert: Dry peak heat of {max_predicted_temp}°F. Standard pre-cooling.")
        await trigger_cooling(target_temp=70.0)
    else:
        print(f"Forecast clear: Max {max_predicted_temp}°F with {peak_humidity}% humidity.")

async def run_afternoon_learning_cycle(chosen_action, is_peak_pricing):
    """Executes a cooling action and grades its performance over 4 hours."""
    start_kwh = await get_sensor_state("sensor.cooling_energy_usage")
    print(f"Executing action: {chosen_action}")

    await asyncio.sleep(4 * 60 * 60)

    end_kwh = await get_sensor_state("sensor.cooling_energy_usage")
    kwh_used = end_kwh - start_kwh
    print(f"Energy consumed this cycle: {kwh_used} kWh")

    user_overrides = get_override_count()
    reward = calculate_reward(user_overrides, kwh_used, is_peak_pricing)

    # In a full run, we would pass the actual state bands here
    update_q_table("Afternoon", "90-100", "Dry", is_peak_pricing, chosen_action, reward)

APP_STATE["last_evaluated_minute"] = None
APP_STATE["last_grade_run"] = None

async def master_clock():
    """Background task that manages 5-minute polling and daily grading."""
    print("🕰️ Master clock started. Evaluating every 5 minutes.")
    
    while True:
        now = datetime.now()
        
        # ==========================================
        # 1. THE 5-MINUTE EVALUATION LOOP
        # ==========================================
        # Triggers at :00, :05, :10, :15, etc.
        if now.minute % 5 == 0 and APP_STATE["last_evaluated_minute"] != now.minute:
            APP_STATE["last_evaluated_minute"] = now.minute
            
            # Dynamically determine the current Time Block based on the hour
            if 6 <= now.hour < 12:
                current_block = "Morning"
            elif 12 <= now.hour < 18:
                current_block = "Afternoon"
            else:
                current_block = "Evening"
                
            # Define Peak Pricing (e.g., 3 PM to 7 PM)
            is_peak = (15 <= now.hour < 19)
            
            print(f"\n⏰ [5-Min Check] Evaluating '{current_block}' schedule...")
            
            # Fetch user's requested baseline for this time block
            baseline_temp = get_scheduled_temp(current_block)
            
            # Fetch live weather forecast
            forecasts = await get_afternoon_forecast()
            forecast_temp = forecasts[0].get('temperature', 95.0) if forecasts else 95.0
            forecast_humidity = forecasts[0].get('humidity', 20.0) if forecasts else 20.0
            
            # Ask the AI what to do
            chosen_action, target_temp = get_best_q_action(
                time_block=current_block,
                forecast_temp=forecast_temp,
                forecast_humidity=forecast_humidity,
                is_peak_pricing=is_peak,
                baseline_temp=baseline_temp
            )
            
            # Execute the cooling command
            print(f"🤖 AI Decision: {chosen_action}. Target: {target_temp}°F")
            asyncio.create_task(trigger_cooling(target_temp))
            
            # --- NEW: Log to history so the line graph updates every 5 mins! ---
            log_history(
                time_block=current_block,
                actual_temp=forecast_temp, # (Or grab your actual indoor HA sensor here)
                target_temp=target_temp,
                actual_humidity=forecast_humidity,
                action_taken=chosen_action,
                kwh_consumed=0.0, # We calculate total energy later in the grading loop
                user_overrides=0,
                reward_granted=0.0 
            )
            
            # Save the active state so the grading function knows what we did
            APP_STATE["current_action"] = chosen_action
            APP_STATE["current_band"] = get_state_bands(forecast_temp, forecast_humidity)

        # ==========================================
        # 2. THE GRADING LOOP (End of Block)
        # ==========================================
        # For example, grade the "Afternoon" block exactly at 6:00 PM (18:00)
        if now.hour == 18 and now.minute == 0:
            if APP_STATE["last_grade_run"] != now.date():
                print("📝 6:00 PM: Grading the Afternoon block...")
                
                # In a real setup, you would have saved 'start_kwh' at 12:00 PM. 
                # For this snippet, we will pass placeholder energy data to complete the cycle.
                user_overrides = get_override_count()
                
                # Calculate the reward based on the afternoon's performance
                reward = calculate_reward(user_overrides, kwh_used=1.5, is_peak_pricing=True)
                
                # Retrieve the state bands we saved during the 5-minute loop
                temp_band, humidity_band = APP_STATE.get("current_band", ("90-100", "Dry (<35%)"))
                action = APP_STATE.get("current_action", "Normal")
                
                # Update the database
                update_q_table("Afternoon", temp_band, humidity_band, True, action, reward)
                
                APP_STATE["last_grade_run"] = now.date()

        # Sleep for 30 seconds so we don't spam the CPU, then check the clock again
        await asyncio.sleep(30)

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
    conn = sqlite3.connect('brain.db')
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
    conn = sqlite3.connect('brain.db')
    cursor = conn.cursor()
    cursor.execute("SELECT time_block, temp_band, humidity_band, is_peak_pricing, action_taken, q_score FROM q_table ORDER BY q_score DESC")
    columns = [column[0] for column in cursor.description]
    results = [dict(zip(columns, row)) for row in cursor.fetchall()]
    conn.close()
    return {"data": results}

@app.get("/api/history")
async def get_history():
    """Fetches the recent execution history."""
    conn = sqlite3.connect('brain.db')
    cursor = conn.cursor()
    # Added target_temp to the SELECT query below
    cursor.execute("SELECT date_time, time_block, actual_temp, target_temp, actual_humidity, action_taken, user_overrides, reward_granted FROM history_log ORDER BY id DESC LIMIT 15")
    columns = [column[0] for column in cursor.description]
    results = [dict(zip(columns, row)) for row in cursor.fetchall()]
    conn.close()
    return {"data": results}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)

"""
Learning Thermostat Backend
Handles scheduling, Home Assistant integration, and Reinforcement Learning.
"""

import os
import subprocess
import sys
import sqlite3
import asyncio
from collections import deque
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from dotenv import load_dotenv

# Imports from local .py files
import config
import database
import ha_api
import master_loop
import state

terminal_buffer = deque(maxlen=100)


class ConsoleInterceptor:
    """Intercepts the systemctl logs to print to index.html"""
    def __init__(self, original_stdout):
        """Initiates the console output"""
        self.original_stdout = original_stdout
        self.last_message = None
        self.repeat_count = 1

    def write(self, text):
        """Writes the text to the console output"""
        # 1. Still print to the actual machine terminal
        self.original_stdout.write(text)

        # 2. Filter out empty lines and Uvicorn API request spam
        clean_text = text.strip()
        if not clean_text or "HTTP/1.1" in clean_text or "GET /api" in clean_text:
            return

        # 3. We strip off the variable "Live Reward" part to see if core matches
        core_message = clean_text
        if "Live Reward:" in clean_text:
            core_message = clean_text.split("| Live Reward:")[0].strip()

        # 4. Check for repeats
        if self.last_message and core_message == self.last_message:
            self.repeat_count += 1
            if terminal_buffer:
                terminal_buffer.pop()
            # Keep NEWEST version (with updated reward) but add multiplier
            terminal_buffer.append(f"{self.repeat_count}x {clean_text}")
        else:
            # Genuine new event (e.g., block transition or manual override)
            self.last_message = core_message
            self.repeat_count = 1
            terminal_buffer.append(clean_text)

    def flush(self):
        """Flushes the console output"""
        self.original_stdout.flush()

    def __getattr__(self, name):
        """Passes any unknown requests to the terminal"""
        return getattr(self.original_stdout, name)


# Load the hidden variables from the .env file
load_dotenv()
if config.DEBUG_MODE_ENV is True:
    print(f"📂 Database localized to: {config.DB_PATH}")


# --- FASTAPI SETUP ---
@asynccontextmanager
async def lifespan(_fastapi_app: FastAPI):
    """Manages background tasks during the application lifecycle."""
    # SAFE HOOK ZONE: Hijack stdout after Uvicorn has completed its imports
    sys.stdout = ConsoleInterceptor(sys.stdout)

    database.initialize_brain()
    print("🚀 Booting up Thermostat Brain...")
    ha_listener_task = asyncio.create_task(ha_api.listen_to_ha())
    clock_task = asyncio.create_task(master_loop.master_clock())

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

    # 1. Try to pull from live state first
    default_temp = state.APP_STATE.get("last_written_temp")
    default_humid = state.APP_STATE.get("last_written_humid")

    # 2. If state is empty (e.g., on a fresh restart), pull the LAST written block from memory
    if not default_temp or not default_humid:
        try:
            with sqlite3.connect(config.DB_PATH) as conn:
                cursor = conn.cursor()
                # Grab the most recent state added to the AI's Q-Table memory
                cursor.execute('''
                    SELECT temp_band, humidity_band 
                    FROM q_table 
                    ORDER BY id DESC 
                    LIMIT 1
                ''')
                row = cursor.fetchone()

            if row:
                default_temp = row[0]
                default_humid = row[1]
            else:
                # Absolute fallback only if the database is 100% completely empty
                default_temp = "<75"
                default_humid = "20-25%"

        except sqlite3.Error as e:
            print(f"Error fetching fallback Q-table default: {e}")
            default_temp = "<75"
            default_humid = "20-25%"

    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "request": request,  
            "schedule": [],
            "default_temp": default_temp,
            "default_humid": default_humid,
        },
    )


@app.post("/api/schedule")
async def update_schedule(time_block: str, target_temp: float):
    """API Endpoint to manually update the cooling schedule."""
    try:
        with sqlite3.connect(config.DB_PATH) as conn:
            cursor = conn.cursor()

            cursor.execute('''
                INSERT INTO schedule (time_block, target_temp)
                VALUES (?, ?)
                ON CONFLICT(time_block) DO UPDATE SET target_temp = excluded.target_temp
            ''', (time_block, target_temp))

            conn.commit()

        if config.DEBUG_MODE_ENV is True:
            print(f"💾 Schedule saved: {time_block} set to {target_temp}°F")
        return {"status": "success"}

    except sqlite3.Error as e:
        print(f"⚠️ Database Error (update_schedule): {e}")
        return {"status": "error", "message": "Failed to update schedule."}


@app.get("/api/q_table")
async def get_q_table():
    """Fetches the current learned scores."""
    try:
        with sqlite3.connect(config.DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT time_block, temp_band, humidity_band,"
                        " is_peak_pricing, action_taken, q_score"
                        " FROM q_table ORDER BY q_score DESC")
            columns = [column[0] for column in cursor.description]
            results = [dict(zip(columns, row)) for row in cursor.fetchall()]
        return {"data": results}

    except sqlite3.Error as e:
        print(f"⚠️ Database Error in /api/q_table: {e}")
        return {"data": []}

@app.get("/api/current_state")
async def get_current_state():
    """Fetches the active weather band for the current block from RAM."""
    current_band = state.APP_STATE.get("current_band")
    
    # current_band is typically stored as a tuple: (temp_band, humid_band)
    if current_band and len(current_band) >= 2:
        return {
            "temp_band": current_band[0],
            "humid_band": current_band[1]
        }
        
    return {"temp_band": None, "humid_band": None}

@app.get("/api/history")
async def get_history():
    """Fetches the recent execution history."""
    try:
        with sqlite3.connect(config.DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT date_time, time_block, actual_temp,"
                        " target_temp, actual_humidity, action_taken, user_overrides,"
                        " reward_granted FROM history_log ORDER BY id DESC LIMIT 700")
            columns = [column[0] for column in cursor.description]
            results = [dict(zip(columns, row)) for row in cursor.fetchall()]
        return {"data": results}

    except sqlite3.Error as e:
        print(f"⚠️ Database Error in /api/history: {e}")
        return {"data": []}

@app.get("/api/logs")
def get_terminal_logs():
    """Returns the cleanly formatted terminal output from memory."""
    return {"logs": "\n".join(terminal_buffer)}


def is_docker():
    """Checks if the application is currently running inside a Docker container."""
    return os.path.exists('/.dockerenv')

@app.post("/api/restart")
async def restart_service():
    """Triggers a restart depending on the environment (Docker vs Systemd)."""
    print("🔄 Restart command received from UI. Rebooting system...")

    async def perform_restart():
        # Give the web response time to reach the browser before killing the process
        await asyncio.sleep(1)

        if is_docker():
            print("🐳 Docker environment detected. Performing in-place hot reload...")
            # 1. Attempt to pull latest updates from GitHub
            try:
                print("📥 Pulling latest code from repository...")
                result = subprocess.run(["git", "pull"], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                print(f"✅ Update successful: {result.stdout.decode('utf-8').strip()}")
            except FileNotFoundError:
                print("⚠️ 'git' is not installed inside this Docker container. Skipping update.")
            except subprocess.CalledProcessError as e:
                print(f"⚠️ Git pull failed (No internet or merge conflict): {e.stderr.decode('utf-8').strip()}")

            # 2. Instantly swap the current Python process with the newly downloaded code!
            os.execv(sys.executable, [sys.executable] + sys.argv)
        else:
            print("🖥️ Native Linux environment detected. Executing systemctl restart...")
            # Fallback to the standard service restart
            cmd = "sudo /usr/bin/systemctl restart thermostat.service"
            subprocess.Popen(cmd, shell=True)

    # Run the restart sequence as a background task so the API responds immediately
    asyncio.create_task(perform_restart())

    return {"message": "Restarting system... Dashboard will reconnect shortly."}

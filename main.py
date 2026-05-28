"""
Learning Thermostat Backend
Handles scheduling, Home Assistant integration, and Reinforcement Learning.
"""

import os
import sys
import sqlite3
import asyncio
import subprocess
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
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={"schedule": []}
    )


@app.post("/api/schedule")
async def update_schedule(time_block: str, target_temp: float):
    """API Endpoint to manually update the cooling schedule."""
    conn = sqlite3.connect(config.DB_PATH)
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
    conn = sqlite3.connect(config.DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT time_block, temp_band, humidity_band,"
                   " is_peak_pricing, action_taken, q_score"
                   " FROM q_table ORDER BY q_score DESC")
    columns = [column[0] for column in cursor.description]
    results = [dict(zip(columns, row)) for row in cursor.fetchall()]
    conn.close()
    return {"data": results}


@app.get("/api/history")
async def get_history():
    """Fetches the recent execution history."""
    conn = sqlite3.connect(config.DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT date_time, time_block, actual_temp,"
                   " target_temp, actual_humidity, action_taken, user_overrides,"
                   " reward_granted FROM history_log ORDER BY id DESC LIMIT 700")
    columns = [column[0] for column in cursor.description]
    results = [dict(zip(columns, row)) for row in cursor.fetchall()]
    conn.close()
    return {"data": results}


@app.get("/api/logs")
def get_terminal_logs():
    """Returns the cleanly formatted terminal output from memory."""
    return {"logs": "\n".join(terminal_buffer)}


@app.post("/api/restart")
async def restart_system():
    """Triggers a clean shutdown of the process for container restart."""
    print("🔄 Restart command received from UI. Rebooting system...")

    # 1. Gracefully shut down background tasks
    # (Assuming you have a cleanup routine as noted in your logs)

    # 2. Instead of calling 'sudo', we trigger a process exit.
    # If you are running in Docker, your orchestrator (Unraid/Docker)
    # should have restart_policy: unless-stopped set.
    os.kill(os.getpid(), 9)
    return {"status": "rebooting"}

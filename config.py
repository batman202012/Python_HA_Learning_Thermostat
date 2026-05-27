"""
config.py
Loads .env variables and holds all global constants and paths.
"""

import os
from dotenv import load_dotenv

# Load the hidden variables from the .env file
load_dotenv()

# --- DIRECTORIES & PATHS ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
"""The base directory for the app"""
DB_PATH = os.path.join(BASE_DIR, 'brain.db')
"""Path to brain.db"""
WAITING_ROOM_FILE = os.path.join(BASE_DIR, 'waiting_room.json')
"""Path to waiting_room.json"""

# --- HOME ASSISTANT CONFIGURATION ---
HA_ADD = os.getenv("HA_ADD")
HA_URL = f"http://{HA_ADD}/api/services/climate/set_temperature"
HA_URL_FORECAST = f"http://{HA_ADD}/api/services/weather/get_forecasts?return_response"
HA_URL_STATE = f"http://{HA_ADD}/api/states/"
HA_WS_URI = f"ws://{HA_ADD}/api/websocket"
HA_TOKEN = os.getenv("HA_TOKEN")

# --- FASTAPI SERVER ---
IP = os.getenv("IP")
PORT = int(os.getenv("PORT", "3000"))

# --- ENTITIES & SENSORS ---
THERMOSTAT_ENTITY_ID = os.getenv("THERMOSTAT_ENTITY_ID")
COOLING_ENERGY = os.getenv("COOLING_ENERGY_USAGE_SENSOR")
OUTSIDE_TEMP_SENSOR = os.getenv("OUTSIDE_TEMP_SENSOR")
OUTSIDE_HUMD_SENSOR = os.getenv("OUTSIDE_HUMD_SENSOR")
MET_IO_FORCAST = os.getenv("MET_IO_FORCAST")

# --- AI SAFETY RAILS ---
SAFETY_MIN = float(os.getenv("SAFETY_MIN", "68.0"))
SAFETY_MAX = float(os.getenv("SAFETY_MAX", "78.0"))

# --- AIR QUALITY & VENTILATION CONFIG ---
ENABLE_AQ_ENV = os.getenv("ENABLE_AQ_FEATURE", "False")
ENABLE_AQ_FEATURE = ENABLE_AQ_ENV.lower() == "true"
FAN_ENTITY_ID = os.getenv("FAN_ENTITY_ID")

# VOC Configuration
AQ_VOC_SENSOR = os.getenv("AQ_VOC_SENSOR")
AQ_VOC_THRESHOLD = float(os.getenv("AQ_VOC_THRESHOLD", "150.0"))
AQ_VOC_CLEAN_THRESHOLD = float(os.getenv("AQ_VOC_CLEAN_THRESHOLD", "100.0"))

# NOx Configuration
AQ_NOX_SENSOR = os.getenv("AQ_NOX_SENSOR")
AQ_NOX_THRESHOLD = float(os.getenv("AQ_NOX_THRESHOLD", "120.0"))
AQ_NOX_CLEAN_THRESHOLD = float(os.getenv("AQ_NOX_CLEAN_THRESHOLD", "70.0"))

# CO2 Configuration
AQ_CO2_SENSOR = os.getenv("AQ_CO2_SENSOR")
AQ_CO2_THRESHOLD = float(os.getenv("AQ_CO2_THRESHOLD", "1100.0"))
AQ_CO2_CLEAN_THRESHOLD = float(os.getenv("AQ_CO2_CLEAN_THRESHOLD", "800.0"))

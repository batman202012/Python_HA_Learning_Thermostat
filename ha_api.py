"""
ha_api.py
Handles all external communication with Home Assistant (REST API & WebSockets).
"""

import json
import asyncio
import httpx
import websockets

import config
from state import APP_STATE
import master_loop

# Shared persistent client pool managed across the application lifecycle
_client = httpx.AsyncClient(
    timeout=httpx.Timeout(10.0, connect=5.0),
    limits=httpx.Limits(max_keepalive_connections=5, max_connections=10)
)


async def close_session():
    """Gracefully terminates the persistent HTTP connection pool on shutdown."""
    await _client.aclose()

async def trigger_cooling(target_temp: float):
    """Sends a REST API call to HA to change the thermostat temperature."""
    APP_STATE["expected_target_temp"] = target_temp

    headers = {
        "Authorization": f"Bearer {config.HA_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "entity_id": config.THERMOSTAT_ENTITY_ID,
        "temperature": target_temp
    }
    try:
        # Reuse the persistent client instance instead of creating a new pool
        response = await _client.post(
            config.HA_URL,
            headers=headers,
            json=payload
        )
        return response.status_code
    except Exception as e:
        print(f"⚠️ HA API Error (trigger_cooling): {e}")
        return None

async def get_sensor_state(entity_id: str):
    """Gets the numeric state of a specific sensor from Home Assistant."""
    headers = {
        "Authorization": f"Bearer {config.HA_TOKEN}", 
        "Content-Type": "application/json"
    }

    try:
        response = await _client.get(
            f"{config.HA_URL_STATE}{entity_id}", 
            headers=headers
        )
        if response.status_code == 200:
            data = response.json()
            state_val = data.get('state')

            # Guard against common non-numeric availability states in HA
            if state_val in [None, 'unavailable', 'unknown', 'none']:
                return None
            try:
                return float(state_val)
            except (ValueError, TypeError):
                return None
        return None
    except Exception as e:
        # Log network connection dropouts without crashing the loop
        print(f"⚠️ Network error fetching sensor {entity_id}: {e}")
        return None

async def get_current_indoor_temp() -> float:
    """Fetches the actual indoor temperature from the thermostat attributes."""
    headers = {
        "Authorization": f"Bearer {config.HA_TOKEN}",
        "Content-Type": "application/json"
    }

    try:
        async with httpx.AsyncClient() as client:
            # Hit the states endpoint for your specific thermostat
            response = await client.get(f"{config.HA_URL_STATE}" + config.THERMOSTAT_ENTITY_ID, headers=headers)

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
        "Authorization": f"Bearer {config.HA_TOKEN}",
        "Content-Type": "application/json"
    }
    # Ensure this matches your Met.no entity exactly
    payload = {
        "entity_id": config.MET_IO_FORCAST,
        "type": "hourly"
    }

    print("📡 Requesting forecast from weather.forecast_home...")
    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=10) as client:
            # The URL now contains ?return_response
                response = await client.post(config.HA_URL_FORECAST, headers=headers, json=payload)

                if response.status_code != 200:
                    print(f"❌ HA Forecast Error: {response.status_code} - {response.text}")
                    return []
                else:
                    data = response.json()
                    service_output = data.get("service_response", {})
                    entity_forecast = service_output.get(config.MET_IO_FORCAST, {})
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

async def listen_to_ha():
    """Maintains a persistent WebSocket connection to Home Assistant."""
    uri = config.HA_WS_URI
    while True:
        try:
            async with websockets.connect(uri) as websocket:
                print("Connected to HA WebSocket")

                # 1. Read the initial "auth_required" greeting from HA
                await websocket.recv()

                # 2. Send our token
                await websocket.send(json.dumps({"type": "auth", "access_token": config.HA_TOKEN}))

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
                            if entity_id == config.THERMOSTAT_ENTITY_ID:
                                await master_loop.handle_thermostat_change(event_data["data"])

        except websockets.exceptions.ConnectionClosed:
            print("Connection lost. Reconnecting in 5 seconds...")
            await asyncio.sleep(5)
        # pylint: disable=broad-exception-caught
        except Exception as e:
            print(f"WebSocket Error: {e}")
            await asyncio.sleep(5)

async def set_fan(turn_on: bool):
    """Turns the fan on or off via Home Assistant REST API."""
    if not config.ENABLE_AQ_FEATURE:
        return
    if not config.FAN_ENTITY_ID:
        return

    domain = config.FAN_ENTITY_ID.split(".")[0]
    if turn_on:
        service = "turn_on"
    else:
        service = "turn_off"

    url = f"http://{config.HA_ADD}/api/services/{domain}/{service}"

    headers = {
        "Authorization": f"Bearer {config.HA_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {"entity_id": config.FAN_ENTITY_ID}

    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(url, headers=headers, json=payload)
            if response.status_code != 200:
                print(f"⚠️ Swamp cooler service call failed: {response.text}")
        except Exception as e:
            print(f"⚠️ Failed to toggle swamp cooler via API: {e}")


async def get_all_air_quality_metrics() -> dict:
    """Fetches VOC, NOx, and CO2 values concurrently from HA."""
    if not config.ENABLE_AQ_FEATURE:
        return {"voc": None, "nox": None, "co2": None}

    if config.AQ_VOC_SENSOR:
        voc_task = get_sensor_state(config.AQ_VOC_SENSOR)
    else:
        voc_task = asyncio.sleep(0, result=None)

    if config.AQ_NOX_SENSOR:
        nox_task = get_sensor_state(config.AQ_NOX_SENSOR)
    else:
        nox_task = asyncio.sleep(0, result=None)

    if config.AQ_CO2_SENSOR:
        co2_task = get_sensor_state(config.AQ_CO2_SENSOR)
    else:
        co2_task = asyncio.sleep(0, result=None)

    voc, nox, co2 = await asyncio.gather(voc_task, nox_task, co2_task)

    return {"voc": voc, "nox": nox, "co2": co2}

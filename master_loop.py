"""
master_loop.py
Orchestrates the 5-minute clock, the smart Advisor, delayed grading, and Home Assistant callbacks.
"""

import asyncio
import sqlite3
from datetime import datetime, timedelta
import httpx

import config
import state
import database
import ha_api
import rl_agent

def get_override_count():
    """Returns the current override count and resets it for the next cycle."""
    current_count = state.APP_STATE["user_override_count"]
    state.APP_STATE["user_override_count"] = 0
    return current_count

def sync_ha_to_schedule(new_temp: float):
    """Updates the baseline schedule when a manual change is made in HA."""
    current_block = state.APP_STATE.get("active_block", "Mid-Day")

    conn = sqlite3.connect(config.DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO schedule (time_block, target_temp)
        VALUES (?, ?)
        ON CONFLICT(time_block) DO UPDATE SET target_temp = excluded.target_temp
    ''', (current_block, new_temp))
    conn.commit()
    conn.close()

    # CRITICAL: Update the live memory so the 5-minute loop doesn't overwrite this!
    state.APP_STATE["locked_target"] = new_temp
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
    expected = state.APP_STATE.get("expected_target_temp")

    # If there's no expected temp yet, or the new temp is different from the AI's last command
    if expected is None:
        print(f"📡 Initial Sync: Thermostat is at {new_temp}°F. Memory updated.")
        state.APP_STATE["expected_target_temp"] = float(new_temp)
        state.APP_STATE["locked_target"] = float(new_temp)
        return
    elif abs(new_temp - float(expected)) > 0.5:
        print(f"🚨 MANUAL OVERRIDE DETECTED: House set to {new_temp}°F")
        state.APP_STATE["is_manual_override"] = True
        now = datetime.now()
        # If the change happens within 15 mins of a block start, we ignore the 'penalty'
        # and just treat it as a new scheduled preference.
        state.APP_STATE["expected_target_temp"] = new_temp
        is_grace_period = now.minute < 15
        current_ai_action = state.APP_STATE.get("locked_action")
        if not is_grace_period:
            state.APP_STATE["user_override_count"] += 1
            # --- 1. THE Q-TABLE PENALTY ---
            # Make sure we don't punish an empty state or a state that is already manual
            if current_ai_action and current_ai_action not in ["Manual", "None"]:
                print(f"💥 WRIST SLAP: Applying a -20.0 penalty to AI strategy '{current_ai_action}'.")

                # Retrieve the environment state at the exact moment of failure
                # (Adjust these variable fetches to match how your script tracks them)
                time_block = state.APP_STATE.get("active_block", "Mid-Day")
                if time_block == "Peak Hours":
                    is_peak = 1
                else:
                    is_peak = 0
                f_temp = state.APP_STATE.get("last_f_temp", 75.0)
                f_humid = state.APP_STATE.get("last_f_humid", 20.0)

                # Fetch the peak temp from memory to accurately penalize the exact state
                peak_temp = state.APP_STATE.get("forecast_max_temp", None)
                temp_band, humid_band = rl_agent.get_state_bands(f_temp, f_humid, peak_temp)

                # Deliver the instant Bellman update
                database.update_q_score(time_block, temp_band, humid_band, is_peak, current_ai_action, -20.0)
        else:
            print("Adjustment: Logged without penalty.")

        # 3. Sync to DB and Memory
        sync_ha_to_schedule(new_temp)
    else:
        # This was an AI-driven change, so we ignore it for the override counter
        print(f"✅ Automated change to {new_temp}°F confirmed.")

async def evaluate_precooling():
    """Analyzes forecast with UTC-to-Local conversion."""
    print("🔍 Advisor is checking today's thermal outlook...")

    max_predicted_temp = 0.0
    peak_humidity = 0.0

    forecasts = await ha_api.get_afternoon_forecast()
    if not forecasts:
        return None, None

    # Get today's date in local time
    today_local = datetime.now().date()

    for block in forecasts:
        dt_str = block.get('datetime', '')
        if not dt_str:
            continue

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
    state.APP_STATE["forecast_max_temp"] = max_predicted_temp
    state.APP_STATE["forecast_max_humidity"] = peak_humidity

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

    if 0 <= hour < 5:
        return "Overnight"
    if 5 <= hour < 8:
        return "Early Morning"
    if 8 <= hour < 10:
        return "Late Morning"
    if 10 <= hour < 12:
        return "Mid-Day"
    if 12 <= hour < 14:
        return "Early Afternoon"
    if 14 <= hour < 16:
        return "Late Afternoon"
    if 16 <= hour < 19:
        return "Peak Hours"  # 3-hour block to match utility peaks
    if 19 <= hour < 22:
        return "Evening"
    return "Late Night"

async def grade_current_block(block_name, is_peak: bool):
    """Calculates the block reward and returns it for delayed processing."""
    print(f"📝 Grading the {block_name} block...")

    # Initialize a default reward in case of failure
    reward = 0.0

    try:
        # 1. Fetch Sensors
        f_temp = await ha_api.get_sensor_state(config.OUTSIDE_TEMP_SENSOR)
        if f_temp is None:
            f_temp = 75.0

        f_humid = await ha_api.get_sensor_state(config.OUTSIDE_HUMD_SENSOR)
        if f_humid is None:
            f_humid = 20.0

        raw_current_kwh = await ha_api.get_sensor_state(config.COOLING_ENERGY)
        start_kwh = state.APP_STATE.get("start_kwh", 0.0)

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
        time_weight_factor = 3.0

        block_start = state.APP_STATE.get("block_start_time")
        target_reached = state.APP_STATE.get("target_reached_time")

        if block_start and target_reached:
            time_diff = target_reached - block_start
            minutes_taken = time_diff.total_seconds() / 60.0
        else:
            minutes_taken = max_minutes
            print("⚠️ Target never reached in this block. Applying max time penalty.")

        time_penalty = (minutes_taken / max_minutes) * time_weight_factor
        print(f"⏱️ Time Taken: {minutes_taken:.1f} mins | Time Penalty: -{time_penalty:.2f}")
        block_duration = (datetime.now() - block_start).total_seconds() / 60.0
        if block_duration < 30:
            print("⚠️ Block too short for fair grading (Restart detected). Skipping time penalty.")
            time_penalty = 0.0

        # 3. Reward Calculation
        overrides = state.APP_STATE.get("user_override_count", 0)
        base_reward = rl_agent.calculate_reward(overrides, kwh_used=actual_kwh_used, is_peak_pricing=is_peak)

        # Final score for this 2-hour window
        reward = base_reward - time_penalty

        print(f"✅ {block_name} calculation complete. Immediate Reward: {reward:.2f} | kWh: {actual_kwh_used:.2f}")

    except Exception as e:
        print(f"❌ Error calculating grade for {block_name}: {e}")
        reward = -5.0 # Penalty for failing to provide data

    # CRITICAL: Hand the number back to the master_clock!
    return float(reward)

async def master_clock():
    """Master clock that monitors everything at 5 minutes intervals"""
    print("🕰️ High-Res Master Clock started. Monitoring 5 minute intervals.")
    f_temp = 75.0  # Initial default
    f_humid = 20.0 # Initial default

    while True:
        #runs every 1 s

        now = datetime.now()
        current_block = get_current_block_name()
        target_temp = state.APP_STATE.get("locked_target", 72.0)
        chosen_action = state.APP_STATE.get("locked_action", "Normal")

        # --- THE 5-MINUTE TELEMETRY LOOP ---
        if now.minute % 5 == 0 and state.APP_STATE["last_evaluated_minute"] != now.minute:
            state.APP_STATE["last_evaluated_minute"] = now.minute
            try:
                # A. Fetch Sensors
                indoor_temp = await ha_api.get_current_indoor_temp()

                if indoor_temp is None:
                    # SENSOR FAILED LOGIC
                    print("⚠️ Skipping cycle: Could not verify indoor temperature.")
                    indoor_temp = 72.0 # Use this only for the 'display' so the app doesn't crash
                    is_temp_valid = False
                else:
                    is_temp_valid = True

                # Use a temporary variable for the raw sensor fetch
                raw_kwh = await ha_api.get_sensor_state(config.COOLING_ENERGY)

                # If the sensor is None, use the start_kwh as a placeholder
                # so the 'running_kwh' equals 0.0 instead of crashing.
                if raw_kwh is None:
                    print(f"⚠️ Energy sensor {config.COOLING_ENERGY} unavailable. Using fallback.")
                    current_kwh = float(state.APP_STATE.get("start_kwh", 0.0))
                else:
                    current_kwh = float(raw_kwh)

                # B. Weather Fetch (Fail-safe against the 'float' error)
                try:
                    new_f_temp = await ha_api.get_sensor_state(config.OUTSIDE_TEMP_SENSOR)
                    new_f_humid = await ha_api.get_sensor_state(config.OUTSIDE_HUMD_SENSOR)

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

                except Exception as e:
                    # Catch the actual error so you can see if it's a network drop or a typo
                    print(f"🛑 Critical error fetching outdoor sensors: {e}")
                    # Only force fallbacks if we absolutely have no past data to hold onto
                    if f_temp is None:
                        f_temp = 86.0
                    if f_humid is None:
                        f_humid = 20.0

                # --- C. BLOCK TRANSITION & INITIALIZATION ---
                is_startup = state.APP_STATE["active_block"] is None
                is_new_block = state.APP_STATE["active_block"] != current_block

                # 1. First, handle database recovery if it's a reboot
                is_recovery_successful = False
                if is_startup:
                    db_active_block = database.get_session_state("active_block")
                    if db_active_block == current_block:
                        print(f"🔄 Reboot Recovery: Resuming {current_block} metrics from DB.")
                        state.APP_STATE["active_block"] = current_block
                        state.APP_STATE["start_kwh"] = float(database.get_session_state("start_kwh") or current_kwh)

                        stored_time = database.get_session_state("block_start_time")
                        if stored_time:
                            state.APP_STATE["block_start_time"] = datetime.fromisoformat(stored_time)

                        stored_reached = database.get_session_state("target_reached_time")
                        if stored_reached:
                            state.APP_STATE["target_reached_time"] = datetime.fromisoformat(stored_reached)
                            print("🧠 Stopwatch Recovered: Target was previously reached at "
                                  f"{state.APP_STATE['target_reached_time'].strftime('%H:%M:%S')}")

                        is_recovery_successful = True
                    else:
                        print(f"🆕 System start: No matching session found. Starting fresh for {current_block}.")

                # 2. Process delayed grading if a block JUST finished
                if is_new_block and not is_startup:
                    print(f"🚀 Transitioning to {current_block}")
                    finished_block = state.APP_STATE["active_block"]
                    is_peak = finished_block == "Peak Hours"
                    finished_bands = state.APP_STATE.get("current_band", ("<75", "20-25%"))
                    finished_temp_band = finished_bands[0]
                    finished_humid_band = finished_bands[1]
                    finished_action = state.APP_STATE.get("locked_action", "Normal")
                    current_immediate_reward = await grade_current_block(
                        finished_block, is_peak
                    )
                    pending = state.APP_STATE.get("pending_grade")
                    if pending:
                        gamma = 0.65
                        realized_future_bonus = gamma * current_immediate_reward
                        final_past_reward = pending["immediate_reward"] + realized_future_bonus
                        print("🕰️ Delayed Grading: Passing actual future physics "
                            f"({realized_future_bonus:.1f}) back to {pending['block']}")
                        database.update_q_score(
                            pending["block"], pending["temp"], pending["humid"],
                            pending["peak"], pending["action"], final_past_reward
                        )
                        state.APP_STATE["pending_grade"] = None
                        state.clear_waiting_room()

                    if state.APP_STATE.get("is_manual_override"):
                        print("🛑 Human intervened. AI gets no future credit. Wiping waiting room.")
                        state.APP_STATE["pending_grade"] = None
                        state.clear_waiting_room()
                        state.APP_STATE["is_manual_override"] = False
                    else:
                        print(f"⏳ Placing '{finished_block}' into the JSON waiting room.")
                        pending_data = {
                            "block": finished_block, "temp": finished_temp_band,
                            "humid": finished_humid_band, "peak": is_peak,
                            "action": finished_action, "immediate_reward": current_immediate_reward
                        }
                        state.APP_STATE["pending_grade"] = pending_data
                        state.APP_STATE["is_manual_override"] = False
                        state.save_waiting_room(pending_data)

                # 3. Apply "Clean Slate" WIPES (Only if starting fresh!)
                if (is_new_block and not is_startup) or (is_startup and not is_recovery_successful):
                    state.APP_STATE["active_block"] = current_block
                    state.APP_STATE["start_kwh"] = current_kwh
                    state.APP_STATE["user_override_count"] = 0
                    state.APP_STATE["block_start_time"] = datetime.now()
                    state.APP_STATE["target_reached_time"] = None

                    database.save_session_state("active_block", current_block)
                    database.save_session_state("start_kwh", current_kwh)
                    database.save_session_state("block_start_time", state.APP_STATE["block_start_time"].isoformat())
                    database.save_session_state("target_reached_time", "")

                # 4. Fetch REAL TARGET for Memory Recovery (Only needed on startup)
                if is_startup:
                    headers = {"Authorization": f"Bearer {config.HA_TOKEN}", "Content-Type": "application/json"}
                    actual_thermostat_target = 73.0
                    try:
                        async with httpx.AsyncClient(timeout=10) as client:
                            response = await client.get(f"{config.HA_URL_STATE}{config.THERMOSTAT_ENTITY_ID}",
                                                         headers=headers)
                            if response.status_code == 200:
                                data = response.json()
                                ha_target = data.get("attributes", {}).get("temperature")
                                if ha_target:
                                    actual_thermostat_target = float(ha_target)
                                    print(f"🌡️ Live Thermostat Target detected: {actual_thermostat_target}°F")
                    except Exception as e:
                        print(f"⚠️ Could not fetch live target, using default: {e}")

                    last_state = database.get_last_known_state()
                    if last_state and abs(last_state["target_temp"] - actual_thermostat_target) < 0.5:
                        print(f"🧠 Strategy Recovered! Restoring previous action: {last_state['action_taken']}")
                        state.APP_STATE["locked_target"] = last_state["target_temp"]
                        state.APP_STATE["locked_action"] = last_state["action_taken"]
                        state.APP_STATE["recovered_from_reboot"] = True
                    else:
                        print("🆕 Physical target changed while offline (or first boot). Treating as Manual Override.")
                        state.APP_STATE["locked_target"] = actual_thermostat_target
                        state.APP_STATE["recovered_from_reboot"] = False

                if is_startup or is_new_block:
                    # 1. RUN ADVISOR & FETCH BASELINE
                    forecast_rec = None
                    peak_temp = None

                    try:
                        baseline = float(database.get_scheduled_temp(current_block))
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

                    if peak_temp is None:
                        peak_temp = state.APP_STATE.get("forecast_max_temp")

                        # The .get() Trap Safety Net & Reboot Recovery
                        if peak_temp is None:
                            db_peak = database.get_session_state("forecast_max_temp")
                            peak_temp = float(db_peak) if db_peak else 92.0

                    # Constantly back up the threat level to SQLite so it survives reboots
                    state.APP_STATE["forecast_max_temp"] = peak_temp
                    database.save_session_state("forecast_max_temp", peak_temp)

                    # 2. GET CURRENT STATE
                    # Now the AI explicitly knows if a heatwave is coming!
                    state.APP_STATE["current_band"] = rl_agent.get_state_bands(f_temp, f_humid, peak_temp)

                    # 3. PICK STRATEGY
                    if is_startup and state.APP_STATE.get("recovered_from_reboot"):
                        # Bypass the override rule because we recovered this from the database!
                        chosen_action = state.APP_STATE["locked_action"]
                        target_temp = state.APP_STATE["locked_target"]
                        print(f"🔄 Resuming recovered strategy: {chosen_action} @ {target_temp}°F")

                    elif is_startup and state.APP_STATE.get("locked_target") is not None:
                        # This triggers if memory recovery failed, meaning a human actually DID change it
                        print(f"🙌 Respecting recent manual adjustment: {state.APP_STATE['locked_target']}°F")
                        chosen_action = "Manual/Baseline"
                        target_temp = state.APP_STATE["locked_target"]

                    else:
                        # Normal AI Q-Table logic...
                        is_peak = current_block == "Peak Hours"
                        chosen_action, target_temp = rl_agent.get_best_q_action(current_block, f_temp,
                                                                                 f_humid, is_peak, baseline, peak_temp)

                        # 4. APPLY SMART ADVISOR OVERRIDE
                        temp_band, humid_band = rl_agent.get_state_bands(f_temp, f_humid, peak_temp)

                        # Find out what the AI thinks of 'Normal' right now
                        conn = sqlite3.connect(config.DB_PATH)
                        cursor = conn.cursor()
                        cursor.execute('''
                            SELECT q_score FROM q_table
                            WHERE time_block = ? AND temp_band = ? AND humidity_band = ? AND is_peak_pricing = ? AND action_taken = 'Normal'
                        ''', (current_block, temp_band, humid_band, is_peak))
                        row = cursor.fetchone()
                        conn.close()

                        ai_score_normal = row[0] if row else 0.0

                        # Ask the DB how many times the AI has seen this exact weather
                        experience_count = database.get_state_experience_count(current_block,
                                                                                temp_band, humid_band, is_peak)

                        if forecast_rec and chosen_action == "Normal":
                            # I lowered the threshold to 3. Getting 3 days of exact 95F+ weather
                            # is enough data to trust the Q-score.
                            if experience_count < 3:
                                print(f"🎓 Advisor: AI only has {experience_count} days of "
                                      f"'{temp_band}' weather. Training wheels ON. Forcing {forecast_rec}.")
                                chosen_action = forecast_rec
                                if "2°F" in forecast_rec:
                                    target_temp = baseline - 2.0
                                if "4°F" in forecast_rec:
                                    target_temp = baseline - 4.0

                            elif ai_score_normal <= -10.0:
                                print("🛡️ Advisor: AI agrees 'Normal' is bad (Score: "
                                      f"{ai_score_normal:.1f}). Vetoing and forcing {forecast_rec}.")
                                chosen_action = forecast_rec
                                if "2°F" in forecast_rec:
                                    target_temp = baseline - 2.0
                                if "4°F" in forecast_rec:
                                    target_temp = baseline - 4.0

                            else:
                                print(f"🧠 Advisor: AI is a veteran ({experience_count} hot days) "
                                      f"and 'Normal' score is safe ({ai_score_normal:.1f}). Letting AI take the wheel!")

                    # 4. LOCK IT IN
                    state.APP_STATE["locked_action"] = chosen_action
                    state.APP_STATE["locked_target"] = target_temp
                    state.APP_STATE["current_band"] = rl_agent.get_state_bands(f_temp, f_humid, peak_temp)

                target_temp = state.APP_STATE.get("locked_target", 72.0)
                if target_temp is None:
                    target_temp = 75.0
                if is_temp_valid and state.APP_STATE["target_reached_time"] is None:
                    if indoor_temp <= target_temp:
                        reached_now = datetime.now()
                        state.APP_STATE["target_reached_time"] = reached_now
                        # Persist it!
                        database.save_session_state("target_reached_time", reached_now.isoformat())
                        print(f"⏱️ Target reached at {reached_now.strftime('%H:%M:%S')}")

                # D. EXECUTE & LOG
                running_kwh = float(current_kwh) - float(state.APP_STATE.get("start_kwh", 0.0))
                state.APP_STATE["expected_target_temp"] = float(target_temp)
                asyncio.create_task(ha_api.trigger_cooling(target_temp))
                is_ambient_cooling = False
                if f_temp > 40.0 and f_temp < (target_temp - 4):
                    is_ambient_cooling = True
                    print(f"🌬️ Ambient Cooling Active: Outdoor {f_temp}°F is 4°+ below Target {target_temp}°F.")

                # Update the action name for the log so you can see it in the dashboard
                display_action = chosen_action
                if is_ambient_cooling:
                    display_action = f"{chosen_action} (Fan Only)"

                state.APP_STATE["last_f_temp"] = f_temp
                state.APP_STATE["last_f_humid"] = f_humid
                database.log_history(
                    current_block, indoor_temp, target_temp, f_humid,
                    display_action, max(0, running_kwh), state.APP_STATE.get("user_override_count", 0), 0.0
                )
                print(f"✅ 5-minute log successful. ({chosen_action} @ {target_temp}°F)")

            except Exception as e:
                print(f"❌ CRITICAL ERROR IN MASTER CLOCK: {e}")

        await asyncio.sleep(1)

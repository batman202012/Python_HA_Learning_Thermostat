"""
rl_agent.py
Contains the Reinforcement Learning math, POMDP state logic, and epsilon-greedy action selection.
"""

import random
import sqlite3
import config

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
                if val == 45:
                    h_band = "30-45%"
                elif val == 60:
                    h_band = "45-60%"
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
    print(f"🔍 DEBUG X-RAY: Searching DB for -> Block: '{time_block}', Temp: '{temp_band}'"
          f", Humid: '{humidity_band}', Peak: {is_peak_pricing}")


    # 2. Check the historical cheat sheet (Q-Table)
    conn = sqlite3.connect(config.DB_PATH)
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

    # This line ensures target_temp is NEVER lower than 68 and NEVER higher than 78
    target_temp = max(min(raw_target, config.SAFETY_MAX), config.SAFETY_MIN)

    # Log it if the safety kicked in so you know why it's not hitting the math
    if target_temp != raw_target:
        print(f"⚠️ Safety Clamp active: Adjusted {raw_target}°F to {target_temp}°F")

    return chosen_action, target_temp

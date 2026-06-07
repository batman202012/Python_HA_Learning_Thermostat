"""
rl_agent.py
Contains the Reinforcement Learning math, POMDP state logic, and epsilon-greedy action selection.
"""

import random
import sqlite3
import config

def calculate_reward(
    user_overrides: int,
    kwh_used: float,
    is_peak_pricing: bool,
    block_had_aq_venting: bool = False,
    venting_msg: bool = False
):
    """Calculates the success score with AQ forgiveness logic."""
    reward = 0

    if user_overrides == 0:
        reward += 10
    else:
        if config.ENABLE_AQ_FEATURE and block_had_aq_venting:
            reward -= (2 * user_overrides)
            if venting_msg is True:
                print("🍃 Reward Note: Override penalty minimized due to venting.")
        else:
            reward -= (20 * user_overrides)

    cost_multiplier = 4.0 if is_peak_pricing else 2.5
    cost_penalty = kwh_used * cost_multiplier
    reward -= cost_penalty
    return reward


def get_state_bands(temp, humidity, peak_temp=None):
    """Converts the outside temp and humidity into standardized strings."""
    # Temperature Bounds
    t_list = [75, 80, 85, 90, 95, 100, 105, 110]
    t_band = "110+"

    if temp < 75:
        t_band = "<75"
    else:
        for i, val in enumerate(t_list):
            if temp < val:
                # Use the index 'i' to reference the previous boundary
                t_lower_bound = t_list[i-1] if i > 0 else 0
                t_band = f"{t_lower_bound}-{val}"
                break

    # Humidity Bounds
    h_list = [5, 10, 15, 20, 25, 30, 45, 60]
    h_band = "60%+"

    if humidity < 5:
        h_band = "<5%"
    else:
        for i, val in enumerate(h_list):
            if humidity < val:
                h_lower_bound = h_list[i-1] if i > 0 else 0
                h_band = f"{h_lower_bound}-{val}%"
                break

    # Weather Threat Assessment
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

    # Combine data structures safely for POMDP state compatibility
    combined_temp_state = f"{t_band} [{forecast_band}]"

    return combined_temp_state, h_band

def get_best_q_action(time_block: str, forecast_temp: float, forecast_humidity: float,
                     is_peak_pricing: bool, baseline_temp: float, peak_temp) -> tuple[str, float]:
    """Calculates the best action for the current temp and humidity"""

    temp_band, humidity_band = get_state_bands(forecast_temp, forecast_humidity, peak_temp)

    if "Morning" in time_block or "Afternoon" in time_block or "Mid-Day" in time_block:
        available_actions = ["Normal", "Pre-cool 2°F", "Pre-cool 4°F"]
    else:
        available_actions = ["Normal", "Night Drop 2°F", "Eco Mode +2°F"]

    q_scores = {}
    total_visits = 0

    try:
        # Use a context manager to handle connections safely
        with sqlite3.connect(config.DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT action_taken, q_score, visits FROM q_table
                WHERE time_block = ? AND temp_band = ? AND humidity_band = ? AND is_peak_pricing = ?
            ''', (time_block, temp_band, humidity_band, is_peak_pricing))
            results = cursor.fetchall()

            # Populate scores and total visits
            for row in results:
                action, score, visits = row
                q_scores[action] = float(score)
                total_visits += (int(visits) if visits else 0)

    except sqlite3.Error as e:
        print(f"⚠️ Database Error (get_best_q_action): {e}")
        return "Normal", baseline_temp

    # Map database records to action scores and track historical experience
    untried = [a for a in available_actions if a not in q_scores]

    # Ensure all available actions are in the dictionary before we pick the max!
    for action in available_actions:
        if action not in q_scores:
            q_scores[action] = 0.0

    if config.DEBUG_MODE_ENV is True:
        print(f"🔍 DEBUG X-RAY: Found in DB -> {q_scores}")

    # Calculate a decaying exploration rate based on total state visits
    base_epsilon = 0.20
    decay_rate = 0.05
    min_epsilon = 0.05
    # Apply inverse-growth decay to reduce exploration as experience grows
    epsilon = max(
        min_epsilon,
        base_epsilon / (1.0 + (total_visits * decay_rate))
    )

    is_exploring = random.random() < epsilon

    if not q_scores or is_exploring:
        chosen_action = random.choice(untried) if untried else random.choice(available_actions)
        print(f"🧠 AI is EXPLORING: Trying '{chosen_action}'")
    else:
        max_val = max(q_scores.values())
        best_actions = [k for k, v in q_scores.items() if v == max_val]
        chosen_action = random.choice(best_actions)
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

"""
seed_brain.py

A utility script to pre-populate the AI's Q-Table with sensible defaults
based on regional biomes and HVAC hardware configurations.
"""

import sqlite3
import itertools
import config

DAY_BLOCKS = [
    "Mid-Day", "Early Afternoon",
    "Peak Hours", "Late Afternoon"
]

NIGHT_BLOCKS = [
    "Late Night", "Overnight"
]

TEMP_BANDS = [
    "<75", "75-80", "80-85", "85-90", "90-95",
    "95-100", "100-105", "105-110", "110+"
]

THREAT_BANDS = [
    "Threat: <95", "Threat: 95-99",
    "Threat: 100-104", "Threat: 105+"
]

HUMID_BANDS = [
    "<5%", "5-10%", "10-15%", "15-20%", "20-25%",
    "25-30%", "30-45%", "45-60%", "60%+"
]

DAY_ACTIONS = ["Normal", "Pre-cool 2°F", "Pre-cool 4°F", "Eco Mode +2°F"]
NIGHT_ACTIONS = ["Normal", "Night Drop 2°F", "Eco Mode +2°F"]
PEAK_STATES = [True, False]


def calculate_seed_score(
    biome: str,
    hardware: list,
    time_block: str,
    t_idx: int,
    threat_idx: int,
    h_idx: int,
    is_peak: bool,
    action: str
) -> float:
    """Generates a baseline Q-score using heuristics for the specific state."""
    score = 0.0

    has_ac = any(
        ac in hardware for ac in ["Central Air", "Minisplit", "Window AC"]
    )

    # 1. Peak Pricing Gradient
    if is_peak:
        if action == "Pre-cool 4°F":
            score -= 5.0
        elif action in ["Pre-cool 2°F", "Night Drop 2°F"]:
            score -= 2.5
        elif action == "Eco Mode +2°F":
            score += 5.0

    # 2. Threat/Heatwave Gradient
    if threat_idx >= 2:
        if action == "Pre-cool 4°F":
            score += 8.0
        elif action in ["Pre-cool 2°F", "Night Drop 2°F"]:
            score += 4.0
        elif action == "Eco Mode +2°F":
            score -= 10.0

    # 3. Base Temperature Gradient
    if t_idx >= 4:
        # It is 90°F+ outside. Fighting the sun is highly inefficient.
        # Reward riding out the heat, penalize aggressive active cooling.
        if action == "Eco Mode +2°F":
            score += 4.0
        elif action == "Normal":
            score += 2.0
        elif action in ["Pre-cool 2°F", "Night Drop 2°F"]:
            score -= 2.0
        elif action == "Pre-cool 4°F":
            score -= 4.0

    elif t_idx <= 1:
        # It is <80°F outside. Highly efficient time to bank cold air!
        # Reward aggressive pre-cooling, penalize floating the temperature.
        if action == "Pre-cool 4°F":
            score += 4.0
        elif action in ["Pre-cool 2°F", "Night Drop 2°F"]:
            score += 2.0
        elif action == "Normal":
            score += 0.0
        elif action == "Eco Mode +2°F":
            score -= 2.0

    if "Swamp Cooler" in hardware:
        if h_idx >= 3:
                if not has_ac:
                    if action == "Pre-cool 4°F":
                        score -= 15.0
                    elif action in ["Pre-cool 2°F", "Night Drop 2°F"]:
                        score -= 10.0
                    elif action == "Normal":
                        score -= 5.0
                elif "Window AC" in hardware and "Central Air" not in hardware:
                    if time_block in DAY_BLOCKS:
                        if "Pre-cool" in action:
                            score -= 50.0
                        elif "Eco" in action or action == "Normal":
                            score += 10.0
                    else:
                        if "Pre-cool 4°F" in action:
                            score += 10.0
                        elif "Pre-cool 2°F" in action:
                            score += 5.0
        else:
            if action == "Pre-cool 4°F":
                score += 3.0
            elif action in ["Pre-cool 2°F", "Night Drop 2°F"]:
                score += 1.5
            elif action == "Eco Mode +2°F":
                score -= 1.5

    if "Minisplit" in hardware:
        if is_peak:
            if action == "Pre-cool 4°F":
                score += 3.0
            elif action in ["Pre-cool 2°F", "Night Drop 2°F"]:
                score += 1.5

    if "Central Air" in hardware:
        if is_peak:
            if action == "Pre-cool 4°F":
                score -= 3.0
            elif action in ["Pre-cool 2°F", "Night Drop 2°F"]:
                score -= 1.5

    if "Window AC" in hardware:
        if threat_idx >= 3:
            if "Central Air" not in hardware and "Minisplit" not in hardware:
                if action == "Pre-cool 4°F":
                    score -= 5.0
                elif action in ["Pre-cool 2°F", "Night Drop 2°F"]:
                    score -= 2.5

    # 4. Biome Gradients
    if biome == "Desert":
        if action == "Pre-cool 4°F":
            score += 4.0
        elif action in ["Pre-cool 2°F", "Night Drop 2°F"]:
            score += 2.0
        elif action == "Eco Mode +2°F":
            score -= 2.0

    elif biome == "Tropical":
        if action == "Eco Mode +2°F":
            score -= 6.0

    elif biome == "Temperate":
        if action in ["Pre-cool 4°F", "Eco Mode +2°F"]:
            score -= 2.0
        elif action in ["Pre-cool 2°F", "Night Drop 2°F"]:
            score -= 1.0

    return round(score, 2)


def generate_states():
    """Yields all possible combinations of states."""

    day_combinations = itertools.product(
        DAY_BLOCKS,
        enumerate(TEMP_BANDS),
        enumerate(THREAT_BANDS),
        enumerate(HUMID_BANDS),
        PEAK_STATES,
        DAY_ACTIONS
    )

    for state in day_combinations:
        block = state[0]
        t_idx, t_band = state[1]
        tr_idx, tr_band = state[2]
        h_idx, h_band = state[3]
        is_peak = state[4]
        action = state[5]

        combined_t = f"{t_band} [{tr_band}]"
        yield (
            block, combined_t, h_band, is_peak, action,
            t_idx, tr_idx, h_idx
        )

    night_combinations = itertools.product(
        NIGHT_BLOCKS,
        enumerate(TEMP_BANDS),
        enumerate(THREAT_BANDS),
        enumerate(HUMID_BANDS),
        PEAK_STATES,
        NIGHT_ACTIONS
    )

    for state in night_combinations:
        block = state[0]
        t_idx, t_band = state[1]
        tr_idx, tr_band = state[2]
        h_idx, h_band = state[3]
        is_peak = state[4]
        action = state[5]

        combined_t = f"{t_band} [{tr_band}]"
        yield (
            block, combined_t, h_band, is_peak, action,
            t_idx, tr_idx, h_idx
        )


def run_seeder():
    """Interactive menu to execute the seeding process."""
    print("=== AI Thermostat Brain Seeder ===")
    print("1. Desert (Hot & Dry)")
    print("2. Tropical (Hot & Humid)")
    print("3. Temperate (Moderate)")

    biome_choice = input("Select a Biome (1-3): ")
    biomes = {"1": "Desert", "2": "Tropical", "3": "Temperate"}
    biome = biomes.get(biome_choice, "Temperate")

    print("\nSelect your hardware. (Type numbers separated by spaces)")
    print("1. Central Air")
    print("2. Minisplit")
    print("3. Swamp Cooler")
    print("4. Window AC")

    hw_choice = input("Hardware choice(s): ").split()
    hw_map = {
        "1": "Central Air",
        "2": "Minisplit",
        "3": "Swamp Cooler",
        "4": "Window AC"
    }

    hardware = []
    for c in hw_choice:
        if c in hw_map:
            hardware.append(hw_map[c])

    if not hardware:
        hardware = ["Central Air"]

    print(f"\nConfiguration set: {biome} Biome with {', '.join(hardware)}")
    confirm = input("Begin seeding database? (y/n): ")

    if confirm.lower() != 'y':
        print("Aborted.")
        return

    conn = sqlite3.connect(config.DB_PATH)
    cursor = conn.cursor()

    inserted_count = 0
    skipped_count = 0

    print("\nCalculating and inserting permutations... please wait.")

    for state_data in generate_states():
        block, c_temp, h_band, is_peak, action, t_idx, tr_idx, h_idx = state_data

        q_score = calculate_seed_score(
            biome, hardware, block, t_idx, tr_idx, h_idx, is_peak, action
        )

        try:
            # Natively bypass duplicates without throwing heavy Python exceptions
            cursor.execute('''
                INSERT OR IGNORE INTO q_table 
                (time_block, temp_band, humidity_band, is_peak_pricing, action_taken, q_score, visits) 
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (block, c_temp, h_band, is_peak, action, q_score, 1))

            if cursor.rowcount > 0:
                inserted_count += 1
            else:
                skipped_count += 1

        except sqlite3.Error as e:
            print(f"Database error: {e}")
            skipped_count += 1

    conn.commit()
    conn.close()

    print("\n=== Seeding Complete ===")
    print(f"New states injected: {inserted_count}")
    print(f"Existing states preserved: {skipped_count}")


if __name__ == "__main__":
    run_seeder()

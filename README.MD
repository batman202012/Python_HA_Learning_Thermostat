# 🧠 AI Learning Thermostat for Home Assistant

A self-hosted, Reinforcement Learning (Q-Learning) backend that integrates directly with Home Assistant. This system learns your home's unique thermal dynamics, optimizing cooling strategies to balance energy efficiency (especially during peak pricing) with human comfort.

## ✨ Key Features
* **Modular Architecture:** A clean, decoupled codebase separating API routing, database management, Home Assistant web-sockets, and the reinforcement learning math.
* **Reinforcement Learning (Q-Table):** Learns the optimal cooling strategy (e.g., Normal, Pre-cool 2°F, Pre-cool 4°F) for specific outdoor temperature and humidity bands.
* **Delayed Bellman Grading:** Calculates the true cost of an action by passing the actual future energy usage back to the previous time block to refine its Q-score.
* **Weather-Aware Advisor:** Uses hourly forecasts to preemptively cool the house before afternoon heat waves, overriding the AI if it hasn't experienced the upcoming weather enough times.
* **Human Comfort Guard:** Detects manual thermostat overrides via HA WebSockets and applies instant learning penalties (-20 points) so the AI learns not to make you uncomfortable.
* **Live Console Dashboard:** A custom FastAPI interceptor hijacks the system's `stdout`, broadcasting real-time AI thoughts, transitions, and systemd logs directly to the local web UI.

---

## 📂 System Architecture

The monolithic core has been carved into highly specific, manageable modules:

* `main.py`: The FastAPI Entry Point. Boots the server, loads endpoints, and hijacks the terminal output for the dashboard.
* `master_loop.py`: The "Conductor." Orchestrates the 5-minute clock, the smart Advisor, delayed grading, and Home Assistant callbacks.
* `rl_agent.py`: The "Brain." Contains the Reinforcement Learning math, POMDP state logic, and epsilon-greedy action selection.
* `ha_api.py`: The "Nervous System." Handles all external communication with Home Assistant (REST API & WebSockets) using a clean callback pattern.
* `database.py`: The "Long-Term Memory." Handles SQLite connections, schema initialization, and historical history/Q-table queries.
* `state.py`: The "Short-Term Memory." Holds volatile application memory (RAM), terminal buffers, and JSON file I/O for reboot recovery.
* `config.py`: The "Blueprint." Loads `.env` variables and holds all global constants and file paths.

---

## 📋 Prerequisites
1. **Python 3.9+** installed on your host machine (Linux/Raspberry Pi/ProLiant recommended).
2. **Home Assistant** running on your local network.
3. A **Long-Lived Access Token** from Home Assistant (Profile -> Security -> Long-Lived Access Tokens).
4. **Required Home Assistant Entities:**
   * A Climate entity (Thermostat).
   * Outdoor Temperature and Humidity sensors.
   * An Energy usage sensor tracking your HVAC system (kWh).
   * A Weather entity supporting `get_forecasts` for hourly predictions.

---

## 🚀 Setup & Installation

1. Clone the repository and install dependencies:
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

```

2. Create a `.env` file in the root directory (use the provided `config.py` as a reference for required variables).
3. Create a `systemd` service to run the brain in the background. Note the critical `PYTHONUNBUFFERED` flag. Because `main.py` uses a custom `ConsoleInterceptor` to push terminal logs to the web dashboard, Python must be forced to flush its output immediately.

Create `/etc/systemd/system/thermostat.service`:

```ini
[Unit]
Description=AI Thermostat Control Node
After=network.target

[Service]
Type=simple
User=squad
WorkingDirectory=/home/squad/thermostat-ai
# Activate the virtual environment and run the modular entry point
ExecStart=/home/squad/thermostat-ai/venv/bin/python3 main.py
Restart=always
RestartSec=5

# CRITICAL: Forces Python to flush logs immediately for the FastAPI Web Dashboard
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target

```

4. Enable and start the service:

```bash
sudo systemctl daemon-reload
sudo systemctl enable thermostat.service
sudo systemctl start thermostat.service

```

---

### Enabling the Web UI Restart Button

The web dashboard includes a button to hard-reboot the AI system. Because the Python script runs under the standard user `squad`, it does not inherently have permission to execute `systemctl restart`.

To allow the web button to work securely without asking for a password, you must grant the `squad` user permission to restart *only* this specific service.

1. Open the sudoers file safely:

```bash
sudo visudo

```

2. Add this exact line to the bottom of the file (replace `squad` if your linux user is named differently):

```text
squad ALL=(ALL) NOPASSWD: /usr/bin/systemctl restart thermostat.service

```

3. Save and exit. The "Restart System" button on the webpage will now seamlessly bounce the `systemd` service.

---

## 🧠 How the AI Learns

The AI breaks the day into granular time blocks (e.g., Early Afternoon, Peak Hours, Overnight).

* **Every 5 minutes:** It checks sensors, fetches weather, checks the smart Advisor, and ensures the target temperature is being met.
* **At block transitions:** It "grades" the previous block's performance.
* **Rewards:** Generated by keeping the human comfortable (+10 base score).
* **Penalties:** Energy usage (kWh) and Time-to-Target are subtracted from the score. During Peak Hours, energy penalties are heavily multiplied. Manual overrides apply a massive instant penalty.

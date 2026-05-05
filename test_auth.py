import asyncio
import websockets
import json

# PASTE YOUR BRAND NEW TOKEN HERE
HA_TOKEN = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiIyMDBjZDAyM2Y4M2Q0Y2YzYWMwNWY2MTAyNzQxNjZlNCIsImlhdCI6MTc3NzkzNzYxMSwiZXhwIjoyMDkzMjk3NjExfQ.2BSKdQfUD10o6wsb6sZxiiFCQLHCS3RmQo_onj21Zs8"

# Ensure this IP matches your Home Assistant IP
URI = "ws://192.168.86.27:8123/api/websocket"

async def test_connection():
    try:
        print("1. Connecting to Home Assistant...")
        async with websockets.connect(URI) as websocket:
            print("2. Connected! Sending ID badge...")
            
            # Send Auth
            await websocket.send(json.dumps({
                "type": "auth", 
                "access_token": HA_TOKEN
            }))
            
            # Get Response
            response = await websocket.recv()
            data = json.loads(response)
            
            if data.get("type") == "auth_ok":
                print("✅ AUTHENTICATION SUCCESSFUL! The token is perfectly valid.")
            else:
                print(f"❌ AUTH FAILED. Home Assistant says: {data}")
                
    except Exception as e:
        print(f"Connection error: {e}")

asyncio.run(test_connection())
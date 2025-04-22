import json
import websocket
import threading

wallet_address = "0x09864079acf6b8ebe2bcDd8304c4C76EE1E48c24".lower()

def on_message(ws, message):
    try:
        data = json.loads(message)
        channel = data.get("channel")
        print(f"\n📨 Received message on channel: {channel}")
        print(json.dumps(data, indent=2))
    except Exception as e:
        print(f"⚠️ Failed to parse message: {e}")

def on_open(ws):
    print("✅ WebSocket connection established.")
    sub_msg = {
        "method": "subscribe",
        "subscription": {
            "type": "notification",
            "user": wallet_address
        }
    }
    ws.send(json.dumps(sub_msg))
    print(f"📡 Subscribed to notification for wallet: {wallet_address}")

def run_ws():
    ws = websocket.WebSocketApp(
        "wss://api.hyperliquid.xyz/ws",
        on_open=on_open,
        on_message=on_message
    )
    ws.run_forever()

if __name__ == "__main__":
    print("🔄 Listening for notifications...")
    threading.Thread(target=run_ws).start()

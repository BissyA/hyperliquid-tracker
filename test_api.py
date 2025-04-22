from hyperliquid.info import Info
from hyperliquid.utils import constants

wallet = "0x09864079acf6b8ebe2bcDd8304c4C76EE1E48c24"  # Replace this with your actual wallet

info = Info(constants.MAINNET_API_URL, skip_ws=True)
user_data = info.user_state(wallet)

print("User state:", user_data)

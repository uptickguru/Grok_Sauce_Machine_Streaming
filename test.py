print("Starting Grok Sauce Test...")
import os
from dotenv import load_dotenv

load_dotenv()
print(f"Username: {os.getenv('TASTY_USERNAME')}")
print("Test complete!")
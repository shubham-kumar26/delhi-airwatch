import os
import requests
from dotenv import load_dotenv

load_dotenv()
headers = {"X-API-Key": os.getenv("OPENAQ_API_KEY")}

r = requests.get(
    "https://api.openaq.org/v3/locations",
    headers=headers,
    params={"coordinates": "28.6139,77.2090", "radius": 25000, "limit": 100},
)
print("Status:", r.status_code)
r.raise_for_status()

for loc in r.json()["results"]:
    sensors = [s["parameter"]["name"] for s in loc.get("sensors", [])]
    print(loc["id"], "|", loc["name"], "|", sensors)

import requests
import pandas as pd

url = "https://www.sec.gov/files/company_tickers.json"

# Add User-Agent (SEC requires this)
headers = {
    "User-Agent": "Akshaj Chandwani (akshaj.chandwani@gmail.com)"
}

resp = requests.get(url, headers=headers)

# Make sure request succeeded
if resp.status_code != 200:
    raise Exception(f"Failed to fetch data. Status: {resp.status_code}")

# Parse JSON
data = resp.json()

records = []
for _, entry in data.items():
    records.append({
        "CIK": str(entry["cik_str"]).zfill(10),
        "Ticker": entry["ticker"],
        "Company Name": entry["title"]
    })

df = pd.DataFrame(records)
df.to_csv("sec_company_tickers.csv", index=False)

print(f"✅ Saved sec_company_tickers.csv with {len(df)} companies")

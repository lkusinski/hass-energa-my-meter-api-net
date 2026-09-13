#!/usr/bin/env python3
"""Fetch raw API response from Energa Mój Licznik."""
import asyncio
import aiohttp
import json
import sys

BASE_URL = "https://api-mojlicznik.energa-operator.pl/dp"
LOGIN_ENDPOINT = "/apihelper/UserLogin"
SESSION_ENDPOINT = "/apihelper/SessionStatus"
DATA_ENDPOINT = "/resources/user/data"

HEADERS = {
    "User-Agent": "Energa/3.1.2 (pl.energa-operator.mojlicznik; build:1; iOS 16.6.1) Alamofire/5.6.4",
    "Accept": "application/json",
    "Accept-Language": "pl-PL;q=1.0, en-PL;q=0.9",
    "Content-Type": "application/json",
}


async def main():
    if len(sys.argv) < 3:
        print("Usage: python test_api_response.py <email> <password>")
        sys.exit(1)

    username = sys.argv[1].strip().lower()
    password = sys.argv[2]

    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False)) as session:
        # Session init
        async with session.get(f"{BASE_URL}{SESSION_ENDPOINT}", headers=HEADERS) as resp:
            print(f"Session: {resp.status}")

        # Login
        params = {
            "clientOS": "ios",
            "notifyService": "APNs",
            "username": username,
            "password": password,
            "token": "test-token-12345",
        }
        async with session.get(
            f"{BASE_URL}{LOGIN_ENDPOINT}", headers=HEADERS, params=params
        ) as resp:
            data = await resp.json()
            print(f"Login success: {data.get('success')}")

        # Fetch user data
        async with session.get(
            f"{BASE_URL}{DATA_ENDPOINT}", headers=HEADERS
        ) as resp:
            raw = await resp.text()
            print(f"Data status: {resp.status}, length: {len(raw)}")

        try:
            data = json.loads(raw)
            # Save full response
            with open("api_response.json", "w") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            print("Full response saved to api_response.json")

            # Print summary
            resp_data = data.get("response", {})
            mps = resp_data.get("meterPoints", [])
            print(f"\n=== Summary ===")
            print(f"meterPoints: {len(mps)}")
            for mp in mps:
                print(f"\n  meterPoint id={mp.get('id')}, dev={mp.get('dev')}, tariff={mp.get('tariff')}")
                print(f"  name={mp.get('name')}")

                # agreementPoints
                ags = resp_data.get("agreementPoints", [])
                nested_ags = mp.get("agreementPoints", [])
                print(f"  nested agreementPoints: {len(nested_ags)}")
                for na in nested_ags:
                    print(f"    code={na.get('code')}, address={na.get('address')}")
                    dealer = na.get("dealer", {})
                    print(f"    dealer.start={dealer.get('start')}")

                # lastMeasurements
                lms = mp.get("lastMeasurements", [])
                print(f"  lastMeasurements: {len(lms)}")
                for lm in lms:
                    print(f"    zone={lm.get('zone')}, value={lm.get('value')}, tm={lm.get('tm')}")

                # meterObjects
                mos = mp.get("meterObjects", [])
                print(f"  meterObjects: {len(mos)}")
                for mo in mos:
                    print(f"    obis={mo.get('obis')}, name={mo.get('name')}")

                # Check for other interesting fields
                for key in ["prosumerType", "prosumerCoefficient", "prosumer_coefficient",
                            "activationDate", "coefficient", "phase", "tariffCode",
                            "status", "type", "flags", "obis"]:
                    if key in mp:
                        print(f"  {key}={mp[key]}")

                # Check agreementPoints at top level for this meter
                for ag in ags:
                    if ag.get("id") == mp.get("id"):
                        print(f"  top-level agreementPoint: code={ag.get('code')}")
                        dealer = ag.get("dealer", {})
                        print(f"  dealer.start={dealer.get('start')}")
                        for k in ["prosumerType", "prosumerCoefficient", "coefficient",
                                  "activationDate", "type", "phase"]:
                            if k in ag:
                                print(f"  ag.{k}={ag[k]}")

        except json.JSONDecodeError:
            print(f"Raw response (first 2000): {raw[:2000]}")


if __name__ == "__main__":
    asyncio.run(main())

import os
import csv
import argparse
from pathlib import Path

import requests

API_KEY = os.environ["FUGLE_API_KEY"]

BASE_URL = (
    "https://api.fugle.tw/marketdata/v1.0/"
    "stock/historical/candles"
)


def fetch_1m(symbol, date):
    url = f"{BASE_URL}/{symbol}"

    params = {
        "from": date,
        "to": date,
        "timeframe": "1",
        "fields": "open,high,low,close,volume,average",
        "sort": "asc",
    }

    headers = {
        "X-API-KEY": API_KEY,
        "Accept": "application/json",
    }

    response = requests.get(
        url,
        params=params,
        headers=headers,
        timeout=30,
    )

    response.raise_for_status()

    return response.json()


def save_csv(symbol, date, payload):
    out_dir = Path("data") / date
    out_dir.mkdir(parents=True, exist_ok=True)

    out_file = out_dir / f"{symbol}.csv"

    rows = payload.get("data", [])

    with out_file.open(
        "w",
        newline="",
        encoding="utf-8-sig"
    ) as f:

        writer = csv.writer(f)

        writer.writerow([
            "datetime",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "average",
        ])

        for row in rows:
            writer.writerow([
                row.get("date"),
                row.get("open"),
                row.get("high"),
                row.get("low"),
                row.get("close"),
                row.get("volume"),
                row.get("average"),
            ])

    print(
        f"{symbol} {date}: "
        f"{len(rows)} bars -> {out_file}"
    )


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--date",
        required=True
    )

    parser.add_argument(
        "--symbols",
        required=True
    )

    args = parser.parse_args()

    symbols = [
        s.strip()
        for s in args.symbols.split(",")
        if s.strip()
    ]

    for symbol in symbols:
        payload = fetch_1m(
            symbol,
            args.date
        )

        save_csv(
            symbol,
            args.date,
            payload
        )


if __name__ == "__main__":
    main()

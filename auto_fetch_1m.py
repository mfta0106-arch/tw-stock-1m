 import os
import re
import csv
import io
import time
import zipfile
import argparse
from pathlib import Path
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_FLOOR

import requests


# =========================================================
# 設定
# =========================================================

API_KEY = os.environ["FUGLE_API_KEY"]

FUGLE_URL = (
    "https://api.fugle.tw/marketdata/v1.0/"
    "stock/historical/candles"
)

DAILY_DATA_URL = (
    "https://github.com/"
    "yukishirotsubasa/tw-stock-data-release/"
    "releases/download/daily-close-csv/"
)

MAX_PRICE = Decimal("100")

GAP_MIN = Decimal("-1")
GAP_MAX = Decimal("2")

COMMON_STOCK = re.compile(r"^[1-9][0-9]{3}$")

HEADERS = {
    "User-Agent": "Mozilla/5.0 GitHub-Actions-Stock-Scanner/1.0"
}


# =========================================================
# 數字
# =========================================================

def dec(value):
    if value is None:
        return None

    text = str(value).strip().replace(",", "")

    if not text or text in {"--", "---"}:
        return None

    try:
        return Decimal(text)
    except Exception:
        return None


# =========================================================
# 台股跳動單位
# =========================================================

def tick_size(price):
    price = Decimal(price)

    if price < Decimal("10"):
        return Decimal("0.01")

    if price < Decimal("50"):
        return Decimal("0.05")

    if price < Decimal("100"):
        return Decimal("0.1")

    if price < Decimal("500"):
        return Decimal("0.5")

    if price < Decimal("1000"):
        return Decimal("1")

    return Decimal("5")


# =========================================================
# 正式漲停價
# =========================================================

def calc_limit_up(reference_price):
    reference_price = Decimal(reference_price)

    raw = reference_price * Decimal("1.10")

    tick = tick_size(raw)

    units = (
        raw / tick
    ).to_integral_value(
        rounding=ROUND_FLOOR
    )

    return units * tick


# =========================================================
# GitHub 每週日K資料網址
# =========================================================

def get_week_url(day):
    iso = day.isocalendar()

    filename = (
        f"weekly_{iso.year}_W{iso.week:02d}.zip"
    )

    return (
        DAILY_DATA_URL
        + filename
    )


# =========================================================
# 下載一週資料
# =========================================================

def download_week(day):
    url = get_week_url(day)

    print(f"[DAILY] download: {url}")

    response = requests.get(
        url,
        headers=HEADERS,
        timeout=60,
        allow_redirects=True,
    )

    if response.status_code == 404:
        print("[DAILY] weekly file not found")
        return []

    response.raise_for_status()

    with zipfile.ZipFile(
        io.BytesIO(response.content)
    ) as z:

        csv_files = [
            name
            for name in z.namelist()
            if name.lower().endswith(".csv")
        ]

        if not csv_files:
            return []

        raw = z.read(
            csv_files[0]
        ).decode(
            "utf-8-sig"
        )

    reader = csv.DictReader(
        io.StringIO(raw)
    )

    rows = []

    for row in reader:

        code = str(
            row.get("code", "")
        ).strip()

        # 只保留一般4碼股票
        if not COMMON_STOCK.fullmatch(code):
            continue

        open_price = dec(
            row.get("open")
        )

        close_price = dec(
            row.get("close")
        )

        if (
            open_price is None
            or close_price is None
        ):
            continue

        rows.append({
            "date":
                str(row.get("date", "")).strip(),

            "code":
                code,

            "name":
                str(row.get("name", "")).strip(),

            "open":
                open_price,

            "high":
                dec(row.get("high")),

            "low":
                dec(row.get("low")),

            "close":
                close_price,

            "volume":
                str(row.get("volume", "")).strip(),
        })

    return rows


# =========================================================
# 取得足夠的日K資料
# 最多往前抓6週，處理連假
# =========================================================

def load_history(target_day):
    all_rows = {}

    for weeks_back in range(0, 6):

        probe = (
            target_day
            - timedelta(
                days=7 * weeks_back
            )
        )

        try:
            rows = download_week(probe)

        except Exception as e:
            print(
                f"[DAILY] week failed: {e}"
            )
            continue

        for row in rows:

            key = (
                row["date"],
                row["code"]
            )

            all_rows[key] = row

        trading_dates = sorted({
            key[0]
            for key in all_rows
            if key[0]
            <= target_day.strftime("%Y%m%d")
        })

        # target + T-1 + T-2
        if (
            target_day.strftime("%Y%m%d")
            in trading_dates
            and len(trading_dates) >= 3
        ):
            break

    return list(
        all_rows.values()
    )


# =========================================================
# 自動找候選
# =========================================================

def find_candidates(
    rows,
    target_day
):

    target_str = (
        target_day.strftime("%Y%m%d")
    )

    trading_dates = sorted({
        row["date"]
        for row in rows
        if row["date"] <= target_str
    })

    if target_str not in trading_dates:
        raise RuntimeError(
            f"找不到 {target_str} 日K資料。"
            "可能該週資料尚未發布，"
            "或日期不是交易日。"
        )

    index = trading_dates.index(
        target_str
    )

    if index < 2:
        raise RuntimeError(
            "沒有足夠的前兩個交易日資料"
        )

    prev_date = trading_dates[
        index - 1
    ]

    prev2_date = trading_dates[
        index - 2
    ]

    print(
        f"[DATE] T={target_str}, "
        f"T-1={prev_date}, "
        f"T-2={prev2_date}"
    )

    by_date = {}

    for row in rows:

        by_date.setdefault(
            row["date"],
            {}
        )[row["code"]] = row


    today = by_date.get(
        target_str,
        {}
    )

    yesterday = by_date.get(
        prev_date,
        {}
    )

    day_before = by_date.get(
        prev2_date,
        {}
    )

    candidates = []


    for code, today_row in today.items():

        if code not in yesterday:
            continue

        if code not in day_before:
            continue


        y = yesterday[code]

        y2 = day_before[code]


        # T-1 正式漲停價
        limit_up = calc_limit_up(
            y2["close"]
        )


        # T-1 必須收在漲停
        if y["close"] != limit_up:
            continue


        target_open = (
            today_row["open"]
        )


        # 股價上限 100
        if target_open > MAX_PRICE:
            continue


        if target_open <= 0:
            continue


        # 今日開盤相對昨日漲停收盤
        gap_pct = (
            (
                target_open
                / y["close"]
            )
            - Decimal("1")
        ) * Decimal("100")


        # -1% ~ +2%
        if gap_pct < GAP_MIN:
            continue

        if gap_pct > GAP_MAX:
            continue


        if (
            target_open
            >= Decimal("50")
        ):
            bucket = "50-100"
        elif (
            target_open
            >= Decimal("30")
        ):
            bucket = "30-50"
        else:
            bucket = "<30"


        candidates.append({

            "date":
                target_str,

            "code":
                code,

            "name":
                today_row["name"],

            "prev2_date":
                prev2_date,

            "prev2_close":
                y2["close"],

            "prev_date":
                prev_date,

            "prev_close":
                y["close"],

            "limit_up_price":
                limit_up,

            "open":
                target_open,

            "gap_pct":
                gap_pct,

            "price_bucket":
                bucket,
        })


    candidates.sort(
        key=lambda x: (
            x["price_bucket"] != "50-100",
            -float(x["gap_pct"])
        )
    )


    return candidates


# =========================================================
# Fugle 1分K
# =========================================================

def fetch_1m(
    symbol,
    target_date
):

    url = (
        f"{FUGLE_URL}/{symbol}"
    )

    params = {
        "from":
            target_date,

        "to":
            target_date,

        "timeframe":
            "1",

        "fields":
            "open,high,low,close,volume,average",

        "sort":
            "asc",
    }


    headers = {
        "X-API-KEY":
            API_KEY,

        "Accept":
            "application/json",
    }


    response = requests.get(
        url,
        params=params,
        headers=headers,
        timeout=60,
    )


    if response.status_code == 404:
        return []


    response.raise_for_status()


    payload = response.json()


    wanted = []


    for row in payload.get(
        "data",
        []
    ):

        dt = str(
            row.get(
                "date",
                ""
            )
        )


        # 即使Fugle回傳近30日，
        # 也只留下指定日期
        if not dt.startswith(
            target_date
        ):
            continue


        wanted.append(row)


    return wanted


# =========================================================
# 儲存 candidates.csv
# =========================================================

def save_candidates(
    candidates,
    target_date
):

    out_dir = (
        Path("data")
        / target_date
    )

    out_dir.mkdir(
        parents=True,
        exist_ok=True
    )


    path = (
        out_dir
        / "candidates.csv"
    )


    columns = [
        "date",
        "code",
        "name",
        "prev2_date",
        "prev2_close",
        "prev_date",
        "prev_close",
        "limit_up_price",
        "open",
        "gap_pct",
        "price_bucket",
    ]


    with path.open(
        "w",
        newline="",
        encoding="utf-8-sig"
    ) as f:

        writer = csv.DictWriter(
            f,
            fieldnames=columns
        )

        writer.writeheader()


        for row in candidates:

            output = dict(row)

            output["gap_pct"] = (
                f"{row['gap_pct']:.4f}"
            )

            writer.writerow(output)


    return path


# =========================================================
# 儲存個股1分K
# =========================================================

def save_1m(
    symbol,
    target_date,
    rows
):

    out_dir = (
        Path("data")
        / target_date
    )

    out_dir.mkdir(
        parents=True,
        exist_ok=True
    )


    path = (
        out_dir
        / f"{symbol}.csv"
    )


    with path.open(
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


    return path


# =========================================================
# 主程式
# =========================================================

def main():

    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--date",
        required=True,
        help="YYYY-MM-DD"
    )

    args = parser.parse_args()


    target_day = datetime.strptime(
        args.date,
        "%Y-%m-%d"
    ).date()


    # -------------------------
    # 1. 日K
    # -------------------------

    history = load_history(
        target_day
    )


    # -------------------------
    # 2. 找股票
    # -------------------------

    candidates = find_candidates(
        history,
        target_day
    )


    print(
        f"[CANDIDATES] "
        f"{len(candidates)} stocks"
    )


    for c in candidates:

        print(
            f"  {c['code']} "
            f"{c['name']} "
            f"open={c['open']} "
            f"gap={c['gap_pct']:.2f}% "
            f"{c['price_bucket']}"
        )


    save_candidates(
        candidates,
        args.date
    )


    # -------------------------
    # 3. 自動抓1分K
    # -------------------------

    failed = []


    for index, c in enumerate(
        candidates,
        start=1
    ):

        symbol = c["code"]

        print(
            f"[1M] "
            f"{index}/"
            f"{len(candidates)} "
            f"{symbol}"
        )


        try:

            rows = fetch_1m(
                symbol,
                args.date
            )


            save_1m(
                symbol,
                args.date,
                rows
            )


            print(
                f"[1M] "
                f"{symbol}: "
                f"{len(rows)} bars"
            )


        except Exception as e:

            print(
                f"[FAILED] "
                f"{symbol}: {e}"
            )

            failed.append(
                symbol
            )


        # 避免短時間大量API要求
        time.sleep(0.25)


    if failed:

        print(
            "[WARNING] failed: "
            + ",".join(failed)
        )


    print(
        "[DONE] auto scan complete"
    )


if __name__ == "__main__":
    main()

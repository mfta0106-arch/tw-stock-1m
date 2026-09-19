import os
import csv
import io
import time
import argparse
from pathlib import Path
from datetime import datetime, timedelta
from decimal import Decimal

import requests

from auto_fetch_1m import (
    download_week,
    calc_limit_up,
    save_1m,
)

API_KEY = os.environ["FUGLE_API_KEY"]

FUGLE_URL = (
    "https://api.fugle.tw/marketdata/v1.0/"
    "stock/historical/candles"
)

MAX_PRICE = Decimal("100")

# 5000 張 = 5,000,000 股
MIN_VOLUME_SHARES = Decimal("5000000")


def D(value):
    try:
        return Decimal(
            str(value)
            .replace(",", "")
            .strip()
        )
    except Exception:
        return None


# =========================================================
# 抓整段需要的日K
# =========================================================

def load_daily_history(
    start_day,
    end_day
):
    rows_map = {}
    downloaded_weeks = set()

    # 多抓兩週，才能判斷第一天的前一交易日漲停
    probe = start_day - timedelta(days=14)

    while probe <= end_day:

        iso = probe.isocalendar()

        week_key = (
            iso.year,
            iso.week
        )

        if week_key not in downloaded_weeks:

            downloaded_weeks.add(
                week_key
            )

            try:
                rows = download_week(
                    probe
                )

                for row in rows:

                    key = (
                        row["date"],
                        row["code"]
                    )

                    rows_map[key] = row

                print(
                    f"[DAILY] "
                    f"{week_key}: "
                    f"{len(rows)} rows"
                )

            except Exception as e:

                print(
                    f"[DAILY ERROR] "
                    f"{week_key}: {e}"
                )

        probe += timedelta(days=7)

    return list(
        rows_map.values()
    )


# =========================================================
# 廣義候選：
# 昨日漲停
# 今日開盤 <=100
# 昨日成交量 >=5000張
# 不限制今日開盤缺口
# =========================================================

def find_candidates(
    history,
    start_day,
    end_day
):

    by_date = {}

    for row in history:

        by_date.setdefault(
            row["date"],
            {}
        )[row["code"]] = row


    trading_dates = sorted(
        by_date.keys()
    )

    results = []


    for i in range(
        2,
        len(trading_dates)
    ):

        date = trading_dates[i]

        current_day = (
            datetime.strptime(
                date,
                "%Y%m%d"
            ).date()
        )

        if current_day < start_day:
            continue

        if current_day > end_day:
            continue


        prev_date = (
            trading_dates[i - 1]
        )

        prev2_date = (
            trading_dates[i - 2]
        )


        today = by_date[date]

        yesterday = (
            by_date[prev_date]
        )

        day_before = (
            by_date[prev2_date]
        )


        for code, t in today.items():

            if code not in yesterday:
                continue

            if code not in day_before:
                continue


            y = yesterday[code]

            y2 = day_before[code]


            prev_close = D(
                y["close"]
            )

            prev2_close = D(
                y2["close"]
            )

            today_open = D(
                t["open"]
            )

            prev_volume = D(
                y["volume"]
            )


            if (
                prev_close is None
                or prev2_close is None
                or today_open is None
                or prev_volume is None
            ):
                continue


            # -------------------------
            # 昨日正式收漲停
            # -------------------------

            limit_up = calc_limit_up(
                prev2_close
            )

            if prev_close != limit_up:
                continue


            # -------------------------
            # 今日開盤 <=100
            # -------------------------

            if today_open <= 0:
                continue

            if today_open > MAX_PRICE:
                continue


            # -------------------------
            # 昨日成交至少5000張
            # -------------------------

            if (
                prev_volume
                < MIN_VOLUME_SHARES
            ):
                continue


            gap_pct = (
                (
                    today_open
                    / prev_close
                )
                - Decimal("1")
            ) * Decimal("100")


            if today_open < Decimal("30"):
                price_bucket = "<30"

            elif today_open < Decimal("50"):
                price_bucket = "30-50"

            elif today_open < Decimal("70"):
                price_bucket = "50-70"

            else:
                price_bucket = "70-100"


            results.append({

                "date":
                    current_day.strftime(
                        "%Y-%m-%d"
                    ),

                "code":
                    code,

                "name":
                    t["name"],

                "prev_date":
                    prev_date,

                "prev_close":
                    prev_close,

                "prev_volume_lots":
                    (
                        prev_volume
                        / Decimal("1000")
                    ),

                "open":
                    today_open,

                "open_gap_pct":
                    gap_pct,

                "price_bucket":
                    price_bucket,
            })


    results.sort(
        key=lambda x: (
            x["date"],
            x["code"]
        )
    )

    return results


# =========================================================
# Fugle：
# 一檔股票只呼叫一次
# 取得 API 可提供的全部近30日1分K
# =========================================================

def fetch_symbol_30d(
    symbol
):

    url = (
        f"{FUGLE_URL}/{symbol}"
    )

    params = {
        "timeframe": "1",
        "fields":
            "open,high,low,close,volume,average",
        "sort": "asc",
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

    return payload.get(
        "data",
        []
    )


# =========================================================
# 按日期切 Fugle 回傳資料
# =========================================================

def split_rows_by_date(
    rows
):

    result = {}

    for row in rows:

        dt = str(
            row.get(
                "date",
                ""
            )
        )

        if len(dt) < 10:
            continue

        date = dt[:10]

        result.setdefault(
            date,
            []
        ).append(row)

    return result


# =========================================================
# 每日 candidates.csv
# =========================================================

def save_candidates_by_date(
    candidates
):

    grouped = {}

    for row in candidates:

        grouped.setdefault(
            row["date"],
            []
        ).append(row)


    columns = [
        "date",
        "code",
        "name",
        "prev_date",
        "prev_close",
        "prev_volume_lots",
        "open",
        "open_gap_pct",
        "price_bucket",
    ]


    for date, rows in grouped.items():

        out_dir = (
            Path("data")
            / date
        )

        out_dir.mkdir(
            parents=True,
            exist_ok=True
        )


        path = (
            out_dir
            / "broad_candidates.csv"
        )


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


            for row in rows:

                out = dict(row)

                out["open_gap_pct"] = (
                    f"{row['open_gap_pct']:.4f}"
                )

                writer.writerow(out)


# =========================================================
# 總清單
# =========================================================

def save_master_candidates(
    candidates,
    start,
    end
):

    out_dir = Path(
        "backfills"
    )

    out_dir.mkdir(
        parents=True,
        exist_ok=True
    )


    path = (
        out_dir
        / (
            f"candidates_"
            f"{start}_{end}.csv"
        )
    )


    columns = [
        "date",
        "code",
        "name",
        "prev_date",
        "prev_close",
        "prev_volume_lots",
        "open",
        "open_gap_pct",
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

            out = dict(row)

            out["open_gap_pct"] = (
                f"{row['open_gap_pct']:.4f}"
            )

            writer.writerow(out)


    return path


# =========================================================
# Backfill summary
# =========================================================

def save_summary(
    start,
    end,
    candidate_count,
    unique_symbols,
    queried_symbols,
    files_created,
    no_data_count
):

    out_dir = Path(
        "backfills"
    )

    out_dir.mkdir(
        parents=True,
        exist_ok=True
    )


    path = (
        out_dir
        / (
            f"summary_"
            f"{start}_{end}.csv"
        )
    )


    rows = [
        (
            "candidate_count",
            candidate_count
        ),
        (
            "unique_symbols",
            unique_symbols
        ),
        (
            "queried_symbols",
            queried_symbols
        ),
        (
            "files_created",
            files_created
        ),
        (
            "no_data_count",
            no_data_count
        ),
    ]


    with path.open(
        "w",
        newline="",
        encoding="utf-8-sig"
    ) as f:

        writer = csv.writer(f)

        writer.writerow([
            "metric",
            "value"
        ])

        writer.writerows(
            rows
        )


# =========================================================
# 主程式
# =========================================================

def main():

    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--start",
        required=True,
        help="YYYY-MM-DD"
    )

    parser.add_argument(
        "--end",
        required=True,
        help="YYYY-MM-DD"
    )

    args = parser.parse_args()


    start_day = (
        datetime.strptime(
            args.start,
            "%Y-%m-%d"
        ).date()
    )

    end_day = (
        datetime.strptime(
            args.end,
            "%Y-%m-%d"
        ).date()
    )


    if start_day > end_day:

        raise SystemExit(
            "start 不可晚於 end"
        )


    # -------------------------
    # 1. 日K候選池
    # -------------------------

    history = load_daily_history(
        start_day,
        end_day
    )


    candidates = find_candidates(
        history,
        start_day,
        end_day
    )


    print(
        f"[CANDIDATES] "
        f"{len(candidates)}"
    )


    save_candidates_by_date(
        candidates
    )


    save_master_candidates(
        candidates,
        args.start,
        args.end
    )


    # -------------------------
    # 2. 用股票代號分組
    # -------------------------

    by_symbol = {}

    for candidate in candidates:

        by_symbol.setdefault(
            candidate["code"],
            []
        ).append(
            candidate
        )


    print(
        f"[UNIQUE SYMBOLS] "
        f"{len(by_symbol)}"
    )


    queried_symbols = 0
    files_created = 0
    no_data_count = 0


    # -------------------------
    # 3. 每檔股票只抓一次Fugle
    # -------------------------

    for index, (
        symbol,
        candidate_rows
    ) in enumerate(
        sorted(
            by_symbol.items()
        ),
        start=1
    ):

        # 先檢查是不是所有日期都已有檔案
        missing_dates = []

        for candidate in candidate_rows:

            date = candidate["date"]

            path = (
                Path("data")
                / date
                / f"{symbol}.csv"
            )

            if not path.exists():

                missing_dates.append(
                    date
                )


        if not missing_dates:

            print(
                f"[SKIP] "
                f"{symbol}: "
                f"all cached"
            )

            continue


        print(
            f"[FUGLE] "
            f"{index}/"
            f"{len(by_symbol)} "
            f"{symbol} "
            f"dates={missing_dates}"
        )


        try:

            all_rows = (
                fetch_symbol_30d(
                    symbol
                )
            )

            queried_symbols += 1


            rows_by_date = (
                split_rows_by_date(
                    all_rows
                )
            )


            for date in missing_dates:

                rows = (
                    rows_by_date.get(
                        date,
                        []
                    )
                )


                if not rows:

                    print(
                        f"[NO DATA] "
                        f"{symbol} "
                        f"{date}"
                    )

                    no_data_count += 1

                    continue


                save_1m(
                    symbol,
                    date,
                    rows
                )


                files_created += 1


                print(
                    f"[SAVED] "
                    f"{date}/"
                    f"{symbol}.csv "
                    f"{len(rows)} bars"
                )


        except Exception as e:

            print(
                f"[ERROR] "
                f"{symbol}: {e}"
            )


        # 避免API要求過快
        time.sleep(0.35)


    # -------------------------
    # 4. Summary
    # -------------------------

    save_summary(
        args.start,
        args.end,
        len(candidates),
        len(by_symbol),
        queried_symbols,
        files_created,
        no_data_count
    )


    print(
        "[DONE] recent 1m backfill finished"
    )


if __name__ == "__main__":
    main()

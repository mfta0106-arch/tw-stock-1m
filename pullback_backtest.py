import csv
import time
import argparse
from pathlib import Path
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_FLOOR

from auto_fetch_1m import (
    download_week,
    calc_limit_up,
    fetch_1m,
    save_1m,
    tick_size,
)

MAX_PRICE = Decimal("100")
MIN_VOLUME_SHARES = 5_000_000       # 5000張
OPEN_GAP_MIN = Decimal("2")         # 開盤必須 > +2%
ENTRY_ZONE_TOP = Decimal("2")       # 跌回 +2% 承接
TAKE_PROFIT = Decimal("2")          # +2%
STOP_LOSS = Decimal("3")            # -3%


def D(value):
    try:
        return Decimal(str(value).replace(",", "").strip())
    except Exception:
        return None


def floor_to_tick(price):
    price = D(price)
    tick = tick_size(price)

    units = (
        price / tick
    ).to_integral_value(
        rounding=ROUND_FLOOR
    )

    return units * tick


def load_history(start_day, end_day):
    rows_map = {}
    downloaded = set()

    probe = start_day - timedelta(days=14)

    while probe <= end_day:

        iso = probe.isocalendar()
        key = (iso.year, iso.week)

        if key not in downloaded:
            downloaded.add(key)

            try:
                rows = download_week(probe)

                for row in rows:
                    rows_map[
                        (row["date"], row["code"])
                    ] = row

            except Exception as e:
                print(
                    f"[DAILY ERROR] "
                    f"{key}: {e}"
                )

        probe += timedelta(days=7)

    return list(rows_map.values())


def load_cached_1m(path):
    rows = []

    with path.open(
        "r",
        encoding="utf-8-sig"
    ) as f:

        reader = csv.DictReader(f)

        for row in reader:
            rows.append({
                "date": row["datetime"],
                "open": D(row["open"]),
                "high": D(row["high"]),
                "low": D(row["low"]),
                "close": D(row["close"]),
                "volume": D(row["volume"]),
                "average": D(row["average"]),
            })

    return rows


def get_1m(symbol, date):
    path = (
        Path("data")
        / date
        / f"{symbol}.csv"
    )

    if path.exists():
        rows = load_cached_1m(path)

        if rows:
            print(
                f"[CACHE] {symbol} "
                f"{date}: {len(rows)}"
            )
            return rows

    rows = fetch_1m(
        symbol,
        date
    )

    if rows:
        save_1m(
            symbol,
            date,
            rows
        )

    return rows


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

        target_day = datetime.strptime(
            date,
            "%Y%m%d"
        ).date()

        if target_day < start_day:
            continue

        if target_day > end_day:
            continue

        prev_date = trading_dates[i - 1]
        prev2_date = trading_dates[i - 2]

        today = by_date[date]
        yesterday = by_date[prev_date]
        day_before = by_date[prev2_date]

        for code, t in today.items():

            if code not in yesterday:
                continue

            if code not in day_before:
                continue

            y = yesterday[code]
            y2 = day_before[code]

            # 前日是否正式漲停
            limit_up = calc_limit_up(
                y2["close"]
            )

            if y["close"] != limit_up:
                continue

            today_open = D(t["open"])
            prev_close = D(y["close"])

            if (
                today_open is None
                or prev_close is None
            ):
                continue

            # 開盤價 <= 100
            if today_open > MAX_PRICE:
                continue

            volume = D(y["volume"])

            # 前一天至少5000張
            if (
                volume is None
                or volume
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

            # 開盤必須 > +2%
            if gap_pct <= OPEN_GAP_MIN:
                continue

            results.append({
                "date":
                    target_day.strftime(
                        "%Y-%m-%d"
                    ),

                "code":
                    code,

                "name":
                    t["name"],

                "prev_close":
                    prev_close,

                "prev_volume_lots":
                    volume
                    / Decimal("1000"),

                "open":
                    today_open,

                "open_gap_pct":
                    gap_pct,
            })

    return results


def evaluate_trade(
    candidate,
    rows
):

    prev_close = candidate[
        "prev_close"
    ]

    # +2%上緣作為承接價
    entry = floor_to_tick(
        prev_close
        * Decimal("1.02")
    )

    tp = (
        entry
        * Decimal("1.02")
    )

    sl = (
        entry
        * Decimal("0.97")
    )

    entry_index = None

    # 找第一次跌回承接價
    for i, row in enumerate(rows):

        if (
            row["low"] is not None
            and row["low"] <= entry
        ):
            entry_index = i
            break

    if entry_index is None:

        return {
            **candidate,
            "entry_price": entry,
            "entry_time": "",
            "tp_price": tp,
            "sl_price": sl,
            "result": "NO_ENTRY",
            "event_time": "",
        }

    entry_row = rows[
        entry_index
    ]

    entry_time = entry_row[
        "date"
    ]

    # -----------------------------
    # 從成交那根1分K開始判定
    # -----------------------------

    for i in range(
        entry_index,
        len(rows)
    ):

        row = rows[i]

        high = row["high"]
        low = row["low"]
        bar_open = row["open"]

        if (
            high is None
            or low is None
        ):
            continue

        hit_tp = high >= tp
        hit_sl = low <= sl

        # 特殊處理成交當分鐘：
        # 如果該分鐘開盤仍高於承接價，
        # 代表是分鐘內下跌才成交。
        # 此時該分鐘的高點可能在成交以前，
        # 不可把高點直接算成停利。
        if (
            i == entry_index
            and bar_open is not None
            and bar_open > entry
        ):

            if hit_sl:
                return {
                    **candidate,
                    "entry_price": entry,
                    "entry_time": entry_time,
                    "tp_price": tp,
                    "sl_price": sl,
                    "result": "STOP_FIRST",
                    "event_time": row["date"],
                }

            continue

        # 同一分鐘同時碰到
        if hit_tp and hit_sl:

            return {
                **candidate,
                "entry_price": entry,
                "entry_time": entry_time,
                "tp_price": tp,
                "sl_price": sl,
                "result": "AMBIGUOUS",
                "event_time": row["date"],
            }

        if hit_tp:

            return {
                **candidate,
                "entry_price": entry,
                "entry_time": entry_time,
                "tp_price": tp,
                "sl_price": sl,
                "result": "TP_FIRST",
                "event_time": row["date"],
            }

        if hit_sl:

            return {
                **candidate,
                "entry_price": entry,
                "entry_time": entry_time,
                "tp_price": tp,
                "sl_price": sl,
                "result": "STOP_FIRST",
                "event_time": row["date"],
            }

    return {
        **candidate,
        "entry_price": entry,
        "entry_time": entry_time,
        "tp_price": tp,
        "sl_price": sl,
        "result": "UNRESOLVED",
        "event_time": "",
    }


def save_results(
    results,
    start,
    end
):

    out_dir = (
        Path("backtests")
        / f"pullback_{start}_{end}"
    )

    out_dir.mkdir(
        parents=True,
        exist_ok=True
    )

    details_path = (
        out_dir
        / "details.csv"
    )

    columns = [
        "date",
        "code",
        "name",
        "prev_close",
        "prev_volume_lots",
        "open",
        "open_gap_pct",
        "entry_price",
        "entry_time",
        "tp_price",
        "sl_price",
        "result",
        "event_time",
    ]

    with details_path.open(
        "w",
        newline="",
        encoding="utf-8-sig"
    ) as f:

        writer = csv.DictWriter(
            f,
            fieldnames=columns
        )

        writer.writeheader()

        for row in results:

            out = dict(row)

            out["open_gap_pct"] = (
                f"{row['open_gap_pct']:.4f}"
            )

            writer.writerow(out)

    counts = {}

    for row in results:
        counts[row["result"]] = (
            counts.get(
                row["result"],
                0
            )
            + 1
        )

    wins = counts.get(
        "TP_FIRST",
        0
    )

    losses = counts.get(
        "STOP_FIRST",
        0
    )

    resolved = (
        wins
        + losses
    )

    win_rate = (
        wins / resolved * 100
        if resolved
        else 0
    )

    entered = sum(
        1
        for row in results
        if row["result"]
        != "NO_ENTRY"
    )

    summary_path = (
        out_dir
        / "summary.csv"
    )

    summary = [
        ("candidate_count", len(results)),
        ("entry_count", entered),
        (
            "entry_rate_pct",
            entered / len(results) * 100
            if results
            else 0
        ),
        ("tp_first", wins),
        ("stop_first", losses),
        (
            "ambiguous",
            counts.get(
                "AMBIGUOUS",
                0
            )
        ),
        (
            "unresolved",
            counts.get(
                "UNRESOLVED",
                0
            )
        ),
        (
            "no_entry",
            counts.get(
                "NO_ENTRY",
                0
            )
        ),
        (
            "resolved_win_rate_pct",
            win_rate
        ),
    ]

    with summary_path.open(
        "w",
        newline="",
        encoding="utf-8-sig"
    ) as f:

        writer = csv.writer(f)

        writer.writerow([
            "metric",
            "value"
        ])

        for key, value in summary:
            writer.writerow([
                key,
                value
            ])

    print(
        f"[RESULT] {details_path}"
    )

    print(
        f"[SUMMARY] {summary_path}"
    )

    print(
        f"[WIN RATE] "
        f"{win_rate:.2f}% "
        f"({wins}/{resolved})"
    )


def main():

    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--start",
        required=True
    )

    parser.add_argument(
        "--end",
        required=True
    )

    args = parser.parse_args()

    start_day = datetime.strptime(
        args.start,
        "%Y-%m-%d"
    ).date()

    end_day = datetime.strptime(
        args.end,
        "%Y-%m-%d"
    ).date()

    history = load_history(
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

    results = []

    for i, candidate in enumerate(
        candidates,
        start=1
    ):

        print(
            f"[{i}/{len(candidates)}] "
            f"{candidate['date']} "
            f"{candidate['code']} "
            f"{candidate['name']}"
        )

        try:

            rows = get_1m_for_candidate(
                candidate
            )

            if not rows:

                results.append({
                    **candidate,
                    "entry_price": "",
                    "entry_time": "",
                    "tp_price": "",
                    "sl_price": "",
                    "result": "NO_DATA",
                    "event_time": "",
                })

                continue

            result = evaluate_trade(
                candidate,
                rows
            )

            results.append(
                result
            )

        except Exception as e:

            print(
                f"[ERROR] "
                f"{candidate['code']}: "
                f"{e}"
            )

        time.sleep(0.25)

    save_results(
        results,
        args.start,
        args.end
    )


def get_1m_for_candidate(
    candidate
):

    date = candidate["date"]
    symbol = candidate["code"]

    path = (
        Path("data")
        / date
        / f"{symbol}.csv"
    )

    if path.exists():

        rows = load_cached_1m(
            path
        )

        if rows:
            return rows

    rows = fetch_1m(
        symbol,
        date
    )

    if rows:
        save_1m(
            symbol,
            date,
            rows
        )

    return rows


if __name__ == "__main__":
    main()

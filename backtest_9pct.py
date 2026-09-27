import os
import requests
import pandas as pd
from datetime import datetime

# ==========================================
# 設定
# ==========================================

API_KEY = os.environ.get("FUGLE_API_KEY")

FROM_DATE = "2026-09-01"
TO_DATE   = "2026-09-24"

# 先用這些股票測試
SYMBOLS = [
    "3016",
    "6207",
    "1809",
]

MAX_PRICE = 200

TP_LIST = [2, 3, 4, 5]
SL_LIST = [2, 3, 4]


if not API_KEY:
    raise RuntimeError("找不到 FUGLE_API_KEY")


# ==========================================
# Fugle K線
# ==========================================

def get_k(symbol, timeframe, date_from, date_to):

    url = (
        "https://api.fugle.tw/"
        "marketdata/v1.0/stock/"
        f"historical/candles/{symbol}"
    )

    params = {
        "timeframe": timeframe,
        "from": date_from,
        "to": date_to,
        "sort": "asc",
    }

    headers = {
        "X-API-KEY": API_KEY
    }

    r = requests.get(
        url,
        params=params,
        headers=headers,
        timeout=30
    )

    r.raise_for_status()

    data = r.json().get("data", [])

    return data


# ==========================================
# TP / SL 回測
# ==========================================

def run_trade(
    bars,
    start_index,
    entry,
    tp_pct,
    sl_pct
):

    tp_price = entry * (1 + tp_pct / 100)
    sl_price = entry * (1 - sl_pct / 100)

    for i in range(start_index, len(bars)):

        high = float(bars[i]["high"])
        low  = float(bars[i]["low"])

        # 同一根同時碰 TP / SL
        # 保守算 SL
        if low <= sl_price:
            return {
                "result": "SL",
                "return_pct": -sl_pct
            }

        if high >= tp_price:
            return {
                "result": "TP",
                "return_pct": tp_pct
            }

    # 都沒碰 → 收盤出
    close = float(bars[-1]["close"])

    ret = (
        close / entry - 1
    ) * 100

    return {
        "result": "CLOSE",
        "return_pct": ret
    }


# ==========================================
# 單一股票回測
# ==========================================

def backtest_symbol(symbol):

    print(f"\n處理 {symbol} ...")

    daily = get_k(
        symbol,
        "D",
        FROM_DATE,
        TO_DATE
    )

    if len(daily) < 3:
        return []

    daily.sort(
        key=lambda x: x["date"]
    )

    events = []

    # ======================================
    # T-2 → T-1 → T
    # ======================================

    for i in range(2, len(daily)):

        t2 = daily[i - 2]
        t1 = daily[i - 1]
        t  = daily[i]

        t2_close = float(t2["close"])
        t1_close = float(t1["close"])

        # ==================================
        # 前一天漲幅
        # ==================================

        rise = (
            t1_close / t2_close - 1
        ) * 100

        # 前一天至少 +9%
        if rise < 9:
            continue

        trade_date = str(
            t["date"]
        )[:10]

        print(
            f"  候選 {trade_date} "
            f"前日漲幅 {rise:.2f}%"
        )

        # ==================================
        # T日 1分K
        # ==================================

        try:

            bars = get_k(
                symbol,
                "1",
                trade_date,
                trade_date
            )

        except Exception as e:

            print(
                "  1分K取得失敗:",
                e
            )

            continue

        if not bars:
            continue

        bars.sort(
            key=lambda x: x["date"]
        )

        open_price = float(
            bars[0]["open"]
        )

        first_low = float(
            bars[0]["low"]
        )

        # ==================================
        # >200 不交易
        # ==================================

        if open_price > MAX_PRICE:

            print(
                f"  排除：開盤 "
                f"{open_price} > 200"
            )

            continue

        # ==================================
        # 找進場
        #
        # 第一根1分K之後：
        # 1. 不破第一根低點
        # 2. 重新站回開盤價
        # ==================================

        entry_index = None

        for k in range(
            1,
            len(bars)
        ):

            low = float(
                bars[k]["low"]
            )

            high = float(
                bars[k]["high"]
            )

            close = float(
                bars[k]["close"]
            )

            # 先破第一根低
            # 今天取消
            if low < first_low:
                break

            # 重新站回開盤價
            if (
                high >= open_price
                and
                close >= open_price
            ):

                entry_index = k
                break

        if entry_index is None:

            print(
                "  未觸發進場"
            )

            continue

        entry_time = bars[
            entry_index
        ]["date"]

        # ==================================
        # MFE / MAE
        # ==================================

        after_entry = bars[
            entry_index:
        ]

        max_high = max(
            float(x["high"])
            for x in after_entry
        )

        min_low = min(
            float(x["low"])
            for x in after_entry
        )

        mfe = (
            max_high /
            open_price -
            1
        ) * 100

        mae = (
            min_low /
            open_price -
            1
        ) * 100

        # ==================================
        # 每種 TP / SL
        # ==================================

        for tp in TP_LIST:

            for sl in SL_LIST:

                result = run_trade(
                    bars,
                    entry_index,
                    open_price,
                    tp,
                    sl
                )

                events.append({
                    "symbol":
                        symbol,

                    "date":
                        trade_date,

                    "prev_rise_pct":
                        round(rise, 2),

                    "entry":
                        open_price,

                    "entry_time":
                        entry_time,

                    "TP":
                        tp,

                    "SL":
                        sl,

                    "result":
                        result["result"],

                    "return_pct":
                        round(
                            result[
                                "return_pct"
                            ],
                            2
                        ),

                    "MFE":
                        round(mfe, 2),

                    "MAE":
                        round(mae, 2),
                })

    return events


# ==========================================
# 開始
# ==========================================

all_events = []

for symbol in SYMBOLS:

    try:

        rows = backtest_symbol(
            symbol
        )

        all_events.extend(rows)

    except Exception as e:

        print(
            symbol,
            "錯誤:",
            e
        )


# ==========================================
# 沒有交易
# ==========================================

if not all_events:

    print("\n沒有符合條件的交易")

    raise SystemExit


df = pd.DataFrame(
    all_events
)


# ==========================================
# 統計
# ==========================================

summary = []

for tp in TP_LIST:

    for sl in SL_LIST:

        x = df[
            (df["TP"] == tp)
            &
            (df["SL"] == sl)
        ]

        if len(x) == 0:
            continue

        win_rate = (
            x["return_pct"] > 0
        ).mean() * 100

        tp_rate = (
            x["result"] == "TP"
        ).mean() * 100

        sl_rate = (
            x["result"] == "SL"
        ).mean() * 100

        avg_return = (
            x["return_pct"]
            .mean()
        )

        summary.append({
            "TP":
                tp,

            "SL":
                sl,

            "trades":
                len(x),

            "win_rate":
                round(
                    win_rate,
                    2
                ),

            "tp_rate":
                round(
                    tp_rate,
                    2
                ),

            "sl_rate":
                round(
                    sl_rate,
                    2
                ),

            "avg_return_pct":
                round(
                    avg_return,
                    2
                ),

            "avg_MFE":
                round(
                    x["MFE"].mean(),
                    2
                ),

            "avg_MAE":
                round(
                    x["MAE"].mean(),
                    2
                ),
        })


summary_df = pd.DataFrame(
    summary
)


# ==========================================
# 顯示結果
# ==========================================

print("\n==============================")
print("回測完成")
print("==============================")

print(
    summary_df.to_string(
        index=False
    )
)


# ==========================================
# 儲存 CSV
# ==========================================

df.to_csv(
    "backtest_9pct_events.csv",
    index=False,
    encoding="utf-8-sig"
)

summary_df.to_csv(
    "backtest_9pct_summary.csv",
    index=False,
    encoding="utf-8-sig"
)

print("\n已產生：")
print("backtest_9pct_events.csv")
print("backtest_9pct_summary.csv")

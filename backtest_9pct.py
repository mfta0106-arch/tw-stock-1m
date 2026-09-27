import os
import glob
import requests
import pandas as pd

# ============================================================
# 設定
# ============================================================

API_KEY = os.environ.get("FUGLE_API_KEY")

FROM_DATE = "2026-05-01"
TO_DATE = "2026-09-24"

MAX_PRICE = 200
MIN_RISE = 9.0

TP_LIST = [2, 3, 4, 5]
SL_LIST = [2, 3, 4]

# 每天最多交易 10 檔
MAX_STOCKS_PER_DAY = 10

# 每檔 1 張
SHARES = 1000

# 每日總資金上限
MAX_CAPITAL = 500000

if not API_KEY:
    raise RuntimeError("找不到 FUGLE_API_KEY")


# ============================================================
# Fugle K線
# ============================================================

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

    return r.json().get("data", [])


# ============================================================
# 從 repo data 目錄找股票代號
# ============================================================

def find_symbols():

    symbols = set()

    files = glob.glob(
        "data/**/*.csv",
        recursive=True
    )

    print(f"找到 {len(files)} 個 CSV")

    for file in files:

        try:
            df = pd.read_csv(
                file,
                dtype=str,
                low_memory=False
            )
        except Exception:
            continue

        # 常見股票代號欄位
        possible_columns = [
            "symbol",
            "stock_id",
            "code",
            "ticker",
        ]

        for col in possible_columns:

            if col not in df.columns:
                continue

            values = (
                df[col]
                .dropna()
                .astype(str)
                .str.strip()
            )

            for value in values:

                # 台股普通股票代號
                if (
                    len(value) == 4
                    and
                    value.isdigit()
                ):
                    symbols.add(value)

    symbols = sorted(symbols)

    print(
        f"取得股票代號：{len(symbols)} 檔"
    )

    if len(symbols) == 0:
        raise RuntimeError(
            "data 目錄找不到股票代號。"
            "需要先建立全市場股票清單。"
        )

    return symbols


# ============================================================
# TP / SL
# ============================================================

def run_trade(
    bars,
    start_index,
    entry,
    tp_pct,
    sl_pct
):

    tp_price = entry * (
        1 + tp_pct / 100
    )

    sl_price = entry * (
        1 - sl_pct / 100
    )

    for i in range(
        start_index,
        len(bars)
    ):

        high = float(
            bars[i]["high"]
        )

        low = float(
            bars[i]["low"]
        )

        # 同一根同時碰到
        # 保守視為 SL 先發生
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

    close = float(
        bars[-1]["close"]
    )

    ret = (
        close / entry - 1
    ) * 100

    return {
        "result": "CLOSE",
        "return_pct": ret
    }


# ============================================================
# 找單一股票 +9% 事件
# ============================================================

def find_events(symbol):

    print(
        f"掃描 {symbol}"
    )

    try:

        daily = get_k(
            symbol,
            "D",
            FROM_DATE,
            TO_DATE
        )

    except Exception as e:

        print(
            symbol,
            "日K失敗:",
            e
        )

        return []

    if len(daily) < 2:
        return []

    daily.sort(
        key=lambda x: x["date"]
    )

    events = []

    for i in range(
        1,
        len(daily)
    ):

        yesterday = daily[i - 1]
        today = daily[i]

        prev_close = float(
            yesterday["close"]
        )

        today_close = float(
            today["close"]
        )

        rise = (
            today_close /
            prev_close -
            1
        ) * 100

        # 當天漲幅 >=9%
        if rise < MIN_RISE:
            continue

        # 收盤價 >200 排除
        if today_close > MAX_PRICE:
            continue

        events.append({
            "symbol": symbol,
            "signal_date":
                str(today["date"])[:10],
            "signal_close":
                today_close,
            "rise_pct":
                rise,
        })

    return events


# ============================================================
# 取得下一個交易日
# ============================================================

def get_next_trade_day(
    symbol,
    signal_date
):

    # 多抓幾天
    start = pd.Timestamp(
        signal_date
    ) + pd.Timedelta(days=1)

    end = start + pd.Timedelta(
        days=7
    )

    try:

        data = get_k(
            symbol,
            "D",
            start.strftime("%Y-%m-%d"),
            end.strftime("%Y-%m-%d")
        )

    except Exception:
        return None

    if not data:
        return None

    data.sort(
        key=lambda x: x["date"]
    )

    return str(
        data[0]["date"]
    )[:10]


# ============================================================
# 1分K進場判斷
# ============================================================

def backtest_event(event):

    symbol = event["symbol"]

    trade_date = get_next_trade_day(
        symbol,
        event["signal_date"]
    )

    if trade_date is None:
        return []

    print(
        "回測",
        symbol,
        event["signal_date"],
        "→",
        trade_date
    )

    try:

        bars = get_k(
            symbol,
            "1",
            trade_date,
            trade_date
        )

    except Exception as e:

        print(
            "1分K失敗:",
            symbol,
            e
        )

        return []

    if len(bars) < 2:
        return []

    bars.sort(
        key=lambda x: x["date"]
    )

    open_price = float(
        bars[0]["open"]
    )

    first_low = float(
        bars[0]["low"]
    )

    # 使用者限制
    # 開盤超過 200 不交易
    if open_price > MAX_PRICE:
        return []

    # ========================================================
    # 第一根1分K後
    #
    # 不破第一根低點
    # 並重新站回開盤價
    # ========================================================

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

        # 只要破第一根低點
        # 當天取消
        if low < first_low:
            break

        if (
            high >= open_price
            and
            close >= open_price
        ):

            entry_index = k
            break

    if entry_index is None:
        return []

    # 保守：
    # 重新站回開盤價後
    # 用該根收盤價作為成交價
    #
    # 避免偷看未來且比直接用 open 成交保守
    entry = max(
        open_price,
        float(
            bars[entry_index]["close"]
        )
    )

    # 超過 200 不買
    if entry > MAX_PRICE:
        return []

    entry_time = bars[
        entry_index
    ]["date"]

    after = bars[
        entry_index:
    ]

    max_high = max(
        float(x["high"])
        for x in after
    )

    min_low = min(
        float(x["low"])
        for x in after
    )

    mfe = (
        max_high /
        entry -
        1
    ) * 100

    mae = (
        min_low /
        entry -
        1
    ) * 100

    rows = []

    for tp in TP_LIST:

        for sl in SL_LIST:

            result = run_trade(
                bars,
                entry_index,
                entry,
                tp,
                sl
            )

            rows.append({

                "symbol":
                    symbol,

                "signal_date":
                    event[
                        "signal_date"
                    ],

                "trade_date":
                    trade_date,

                "prev_rise_pct":
                    round(
                        event[
                            "rise_pct"
                        ],
                        2
                    ),

                "signal_close":
                    event[
                        "signal_close"
                    ],

                "open":
                    open_price,

                "entry":
                    entry,

                "entry_time":
                    entry_time,

                "TP":
                    tp,

                "SL":
                    sl,

                "result":
                    result[
                        "result"
                    ],

                "return_pct":
                    round(
                        result[
                            "return_pct"
                        ],
                        4
                    ),

                "MFE":
                    round(
                        mfe,
                        4
                    ),

                "MAE":
                    round(
                        mae,
                        4
                    ),

                "capital":
                    entry *
                    SHARES,

                "profit_nt":
                    round(
                        entry *
                        SHARES *
                        result[
                            "return_pct"
                        ] /
                        100,
                        0
                    ),
            })

    return rows


# ============================================================
# 主程式
# ============================================================

print(
    "================================="
)

print(
    "全市場 +9% 股票回測"
)

print(
    FROM_DATE,
    "→",
    TO_DATE
)

print(
    "股價限制 <=",
    MAX_PRICE
)

print(
    "================================="
)


# ============================================================
# 1. 股票清單
# ============================================================

SYMBOLS = find_symbols()


# ============================================================
# 2. 找全部 +9% 事件
# ============================================================

signals = []

for index, symbol in enumerate(
    SYMBOLS,
    1
):

    print(
        f"[{index}/{len(SYMBOLS)}]",
        symbol
    )

    rows = find_events(
        symbol
    )

    signals.extend(
        rows
    )


if not signals:

    raise RuntimeError(
        "沒有找到 +9% 股票"
    )


signal_df = pd.DataFrame(
    signals
)

signal_df.to_csv(
    "backtest_9pct_signals.csv",
    index=False,
    encoding="utf-8-sig"
)

print()
print(
    "找到 +9% 事件：",
    len(signal_df)
)


# ============================================================
# 3. 每天排序
#
# 不使用隔日資料排序
# 只用訊號日漲幅
# ============================================================

signal_df = (
    signal_df
    .sort_values(
        [
            "signal_date",
            "rise_pct",
        ],
        ascending=[
            True,
            False,
        ]
    )
)


# ============================================================
# 每天最多 10 檔
# ============================================================

selected = (
    signal_df
    .groupby(
        "signal_date",
        group_keys=False
    )
    .head(
        MAX_STOCKS_PER_DAY
    )
)


# ============================================================
# 4. 跑1分K
# ============================================================

all_events = []

for _, event in selected.iterrows():

    rows = backtest_event(
        event
    )

    all_events.extend(
        rows
    )


if not all_events:

    raise RuntimeError(
        "沒有符合1分K進場條件的交易"
    )


df = pd.DataFrame(
    all_events
)


# ============================================================
# 5. 加入每日50萬資金限制
# ============================================================

keep_indices = []

for trade_date, day in df.groupby(
    "trade_date"
):

    # 每個 TP/SL 組合分開處理
    for tp in TP_LIST:

        for sl in SL_LIST:

            group = day[
                (day["TP"] == tp)
                &
                (day["SL"] == sl)
            ]

            used = 0

            count = 0

            for idx, row in group.iterrows():

                capital = float(
                    row["capital"]
                )

                if count >= MAX_STOCKS_PER_DAY:
                    break

                if (
                    used + capital
                    >
                    MAX_CAPITAL
                ):
                    continue

                keep_indices.append(
                    idx
                )

                used += capital
                count += 1


df = df.loc[
    sorted(
        set(
            keep_indices
        )
    )
].copy()


# ============================================================
# 6. 統計
# ============================================================

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

        summary.append({

            "TP":
                tp,

            "SL":
                sl,

            "trades":
                len(x),

            "win_rate":
                round(
                    (
                        x[
                            "return_pct"
                        ] > 0
                    ).mean()
                    * 100,
                    2
                ),

            "tp_rate":
                round(
                    (
                        x[
                            "result"
                        ] == "TP"
                    ).mean()
                    * 100,
                    2
                ),

            "sl_rate":
                round(
                    (
                        x[
                            "result"
                        ] == "SL"
                    ).mean()
                    * 100,
                    2
                ),

            "avg_return_pct":
                round(
                    x[
                        "return_pct"
                    ].mean(),
                    3
                ),

            "median_return_pct":
                round(
                    x[
                        "return_pct"
                    ].median(),
                    3
                ),

            "gross_profit_nt":
                round(
                    x[
                        "profit_nt"
                    ].sum(),
                    0
                ),

            "avg_MFE":
                round(
                    x[
                        "MFE"
                    ].mean(),
                    3
                ),

            "avg_MAE":
                round(
                    x[
                        "MAE"
                    ].mean(),
                    3
                ),
        })


summary_df = pd.DataFrame(
    summary
)


# ============================================================
# 7. 儲存
# ============================================================

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


print()
print(
    "================================="
)

print(
    "回測完成"
)

print(
    "================================="
)

print(
    summary_df.to_string(
        index=False
    )
)

print()
print(
    "已產生："
)

print(
    "backtest_9pct_signals.csv"
)

print(
    "backtest_9pct_events.csv"
)

print(
    "backtest_9pct_summary.csv"
        )

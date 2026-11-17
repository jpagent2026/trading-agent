# Data check (free Alpaca plan)

Run 2026-11-17 01:22 ET. Feed `sip`, adjustment `all`, range 2016-01-01 to 2026-11-17 01:06 ET (end is always at least 15 minutes ago).

- SIP allowed: **yes** - OK (2764 SPY 1-min bars in the last 5 days ending 2026-11-17 01:06 ET)
- Corporate actions: AAPL 2020-08-26 -> 2020-09-02 close ratio: raw 0.26, adjusted 1.04 (split adjusted OK)

## SPY

- Earliest daily bar: 2016-01-04; earliest RTH 1-min bar: 2016-01-04 09:30:00-05:00
- Sessions with RTH bars (2016-01-01 to now): 2734
- Early closes (13:00 ET) by calendar rule in range: 21; checked 2016-11-25: 210 bars to 13:00, 132 bars after 13:00 (volume 34.3% of the 09:30-13:00 volume, mostly the 13:00 closing-auction print); 2018-07-03: 210 bars to 13:00, 135 bars after 13:00 (volume 27.7% of the 09:30-13:00 volume, mostly the 13:00 closing-auction print); 2019-07-03: 210 bars to 13:00, 134 bars after 13:00 (volume 39.3% of the 09:30-13:00 volume, mostly the 13:00 closing-auction print); 2020-11-27: 210 bars to 13:00, 142 bars after 13:00 (volume 20.4% of the 09:30-13:00 volume, mostly the 13:00 closing-auction print); 2022-11-25: 210 bars to 13:00, 120 bars after 13:00 (volume 24.8% of the 09:30-13:00 volume, mostly the 13:00 closing-auction print); 2024-07-03: 210 bars to 13:00, 138 bars after 13:00 (volume 24.8% of the 09:30-13:00 volume, mostly the 13:00 closing-auction print). The RTH filter drops the post-13:00 bars.
- 1-min sample: 41 sessions in 9 windows; bars/day min 210, median 390, max 390 (a full session is 390)
- Sample sessions with < 390 bars: 2016-11-25 (210, last 12:59:00), 2018-12-24 (210, last 12:59:00), 2019-07-03 (210, last 12:59:00), 2020-03-09 (376, last 15:59:00), 2020-03-12 (376, last 15:59:00), 2024-11-29 (210, last 12:59:00)

## QQQ

- Earliest daily bar: 2016-01-04; earliest RTH 1-min bar: 2016-01-04 09:30:00-05:00
- Sessions with RTH bars (2016-01-01 to now): 2734
- Early closes (13:00 ET) by calendar rule in range: 21; checked 2016-11-25: 210 bars to 13:00, 37 bars after 13:00 (volume 9.4% of the 09:30-13:00 volume, mostly the 13:00 closing-auction print); 2018-07-03: 210 bars to 13:00, 78 bars after 13:00 (volume 10.5% of the 09:30-13:00 volume, mostly the 13:00 closing-auction print); 2019-07-03: 210 bars to 13:00, 81 bars after 13:00 (volume 10.9% of the 09:30-13:00 volume, mostly the 13:00 closing-auction print); 2020-11-27: 210 bars to 13:00, 107 bars after 13:00 (volume 21.4% of the 09:30-13:00 volume, mostly the 13:00 closing-auction print); 2022-11-25: 210 bars to 13:00, 82 bars after 13:00 (volume 9.2% of the 09:30-13:00 volume, mostly the 13:00 closing-auction print); 2024-07-03: 210 bars to 13:00, 137 bars after 13:00 (volume 7.8% of the 09:30-13:00 volume, mostly the 13:00 closing-auction print). The RTH filter drops the post-13:00 bars.
- 1-min sample: 41 sessions in 9 windows; bars/day min 210, median 390, max 390 (a full session is 390)
- Sample sessions with < 390 bars: 2016-11-25 (210, last 12:59:00), 2018-12-24 (210, last 12:59:00), 2019-07-03 (210, last 12:59:00), 2020-03-09 (376, last 15:59:00), 2020-03-12 (376, last 15:59:00), 2024-11-29 (210, last 12:59:00)

Note: 2020-03-09 and 2020-03-12 (and 03-16, 03-18) had 15-minute market-wide circuit-breaker halts, so ~376 one-minute bars is correct for those days.

## Gaps

- Weekdays with no bars for any symbol (holidays): 103 (about 9-10 per year expected)
- SPY: sessions missing while another symbol traded: 0
- QQQ: sessions missing while another symbol traded: 0

## Survivorship (delisted names)

Alpaca's asset list lives on the trading API, which research code never calls. Instead we ask the market-data API for bars of names that died after 2016:

| symbol | event | daily bars 2016-2024 | first | last |
|---|---|---|---|---|
| TWTR | taken private Oct 2022 | 1718 | 2016-01-04 | 2022-10-27 |
| ATVI | acquired by Microsoft Oct 2023 | 1959 | 2016-01-04 | 2023-10-13 |
| XLNX | acquired by AMD Feb 2022 | 1540 | 2016-01-04 | 2022-02-11 |
| CTXS | taken private Sep 2022 | 1698 | 2016-01-04 | 2022-09-29 |
| SIVB | bank failure Mar 2023 | 1808 | 2016-01-04 | 2023-03-09 |
| FRC | bank failure May 2023 | 1843 | 2016-01-04 | 2023-04-28 |
| CELG | acquired by BMS Nov 2019 | 979 | 2016-01-04 | 2019-11-20 |
| FB | renamed to META Jun 2022 (ticker change, not a delisting) | 1620 | 2016-01-04 | 2022-06-08 |
| META | new ticker of FB: does its history start in 2016? | 2263 | 2016-01-04 | 2024-12-30 |

If dead names return bars, a point-in-time universe is possible, but we still need a list of which names existed/qualified on each date; the free plan has no such list (survivorship gap).

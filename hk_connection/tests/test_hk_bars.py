from hk_connection.bars import BarAggregator, SessionState, minutes_since_open, in_regular_session
from hk_connection.tests.conftest import et, mbar


def feed(agg, n, start_h=9, start_m=30, base=100.0):
    done_all = []
    for i in range(n):
        h, m = divmod(start_m + i, 60)
        px = base + i
        done_all += agg.add(mbar(start_h + h, m, px, px + 0.5, px - 0.5, px + 0.25))
    return done_all


def test_one_minute_bars_build_5_15_60_aligned_to_open():
    agg = BarAggregator()
    done = feed(agg, 60)
    assert [b.minutes for b in done].count(5) == 12
    assert [b.minutes for b in done].count(15) == 4
    assert [b.minutes for b in done].count(60) == 1
    first5 = agg.completed[5][0]
    assert first5.start == et(9, 30) and first5.open == 100.0 and first5.close == 104.25
    assert first5.high == 104.5 and first5.low == 99.5
    h = agg.completed[60][0]
    assert h.start == et(9, 30) and h.open == 100.0 and h.close == 159.25 and h.volume == 60_000


def test_current_and_previous_bars():
    agg = BarAggregator()
    feed(agg, 7)  # 9:30..9:36 -> one completed 5-min bar, one forming with 2 minutes
    assert agg.current(5).start == et(9, 35) and agg.current(5).minutes == 5
    assert agg.previous(5).start == et(9, 30)
    assert agg.current(1).start == et(9, 36) and agg.previous(1).start == et(9, 35)
    feed(agg, 3, start_m=37, base=107)  # completes 9:35 bucket exactly at 9:39
    assert agg.forming[5] is None
    assert agg.current(5).start == et(9, 35) and agg.previous(5).start == et(9, 30)


def test_session_state_tracks_lod_hod_and_session_helpers():
    st = SessionState("ABC")
    st.update(mbar(9, 30, 10, 10.2, 9.9, 10.1))
    nl, nh, _ = st.update(mbar(9, 31, 10.1, 10.15, 9.8, 9.9))
    assert nl and not nh and st.lod == 9.8 and st.hod == 10.2 and st.open == 10
    assert minutes_since_open(et(9, 45)) == 15
    assert in_regular_session(et(9, 30)) and not in_regular_session(et(9, 29)) and not in_regular_session(et(16, 0))

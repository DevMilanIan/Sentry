# Calendar coverage and limits

The embedded NYSE equity session schedule was verified on September 3, 2026
against the [NYSE holiday and trading-hours calendar](https://www.nyse.com/trade/hours-calendars)
and its [2026–2028 schedule announcement](https://ir.theice.com/press/news-details/2025/NYSE-Group-Announces-2026-2027-and-2028-Holiday-and-Early-Closings-Calendar/).
The schedule version is `nyse-scheduled-2026-2028-verified-2026-09-03`.

`regular_session_close(day)` returns an aware New York datetime, or `None` on a
scheduled closed day. Equity early closes are 13:00 Eastern on November 27 and
December 24, 2026; November 26, 2027; and July 3 and November 24, 2028. Other
scheduled regular sessions close at 16:00. Reporting uses this close and the
optional position end-of-day exit retains at least a 15-minute buffer before it.
An earlier configured exit cutoff remains earlier.

Saturday New Year's Day does not close the preceding Friday: December 31,
2027 remains a regular session. Juneteenth starts in the nominal holiday rules
in 2022, consistent with NYSE's [2022–2024 announcement](https://ir.theice.com/press/news-details/2021/NYSE-Group-Announces-2022-2023-and-2024-Holiday-and-Early-Closings-Calendar/default.aspx)
and [2021 rule filing](https://www.nyse.com/publicdocs/nyse/markets/nyse/rule-filings/filings/2021/SR-NYSE-2021-56.pdf).

Important boundaries:

- Close and phase queries outside 2026–2028 raise `CalendarCoverageError`.
  There is no silently guessed future or historical close. Refresh and test the
  official schedule before operating outside that range.
- `holidays()` and `is_regular_session()` expose nominal holiday-rule arithmetic,
  not a complete historical calendar or evidence that a market is currently open.
- Unscheduled closures, trading halts, and later exchange schedule amendments
  require fresh provider/operator evidence. The embedded schedule is not a live
  exchange-status feed.
- This is an equity calendar, not a universal options-hours or expiration rule.
  NYSE lists 13:15 early closes for eligible options; contract eligibility and
  broker cutoffs must be independently verified. Equity post-market labels also
  do not grant any options execution authority.
- Offline replay remains driven by its fixture and virtual clock. A calendar
  result does not relabel replay as live market data or qualify a real session.

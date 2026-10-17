# Long-term market research

Do not implement until the user says to. Iris suggests. She does not trade, and she does not call the trading bot.

## Sequence

1. **Pass 1.** US listings, then a price and volume cut. Not an opinion.
2. **Pass 2.** Drop names that fail the active profile. No rank. No cut to 10.
3. **Pass 3.** Rank every passer by strength. Save the top 50 non-held names as advanced, for every profile. Keep the rest.
4. **Pass 4.** Headlines for each profile's 50. Reorder that profile. Mark its provisional top 10. Do not drop the other 40.
5. **Report.** Read the saved top 10 for the active profile. Do not recompute. Fewer than 10 shows fewer.

The 6:00 local job runs passes 1-4 for all three profiles. It saves each profile's top 10 and the fields the report shows: rank, symbol, company name, description, industry, and headline title, link, and theme. One record per profile per day. A later run on the same day replaces that day. Older days stay, so a streak or a headline that changed can be read later. The report shows how many consecutive days the name has been on that profile's top 10. If an earlier saved headline mentioned a patent or an expected quarter, and a later headline says it fell through, was registered, or missed, that note is in the details. A daytime `/market run` only reads the latest save. It refetches only when that profile's gates, or the shared price and volume limits, have changed since the save.

Passes 1-4 do not render. A count such as 200 is however many passed, not a target.

## Sources

- Copy the bot's `get_tradable_symbols` and `get_daily_bars_multi` into `core/market/`. Do not import the bot, do not read its `.env`, and do not call `passes_first_pass`. No ATR, momentum, RSI, or 3-bar gate.
- Alpaca keys go through `SecretStore`. Request 200 trading days. The 5-day volume average is the last 5 of those bars. The 20-day return and the 50-day and 200-day averages come from the same bars.
- Fundamentals come from Yahoo quote endpoints through `httpx`. No `yfinance`. No pandas. Cache each symbol 24 hours under `Data/Market/quotes/`.
- `debtToEquity` is a percent; divide by 100. `dividendYield` and `payoutRatio` are fractions; if a value is greater than 1, divide by 100. `revenueGrowth`, `returnOnEquity`, and `profitMargins` are fractions. A required field that is missing is unchecked, not a failure. Do not invent a quote.
- Headlines come from the existing SearXNG news category, last 7 days, at most 5 hits per advanced symbol. Do not call `web_research`. Do not read article bodies.
- E*TRADE is read-only, production only, positions only. No order path. Until OAuth succeeds, read `Data/Configuration/holdings.json` if it exists. Research still runs with no holdings. A partial account read does not produce trims. Say which source was used. Do not fall through to a stale file silently.

Normalize symbols before any match. `BRK.B` and `BRK-B` are one name.

## Pass 1

Shared limits, changeable in chat: last close at least $2 and at most $400, and 5-day average share volume at least 700,000. Holdings stay on the working list even if they miss the cut, so a later sell or trim is not skipped. Cache the last good list. An empty or tiny listings payload does not replace it.

## Pass 2

One active profile per run. Default `compounder`. The user names another, or Iris keeps the last one. Switching profiles changes buy and sell gates. It does not change pass 1 or trims.

A common stock that fails a required gate is off the candidate list. A missing required field is unchecked. Dismissed names are off the list. The rest are saved in symbol order.

Classify with Yahoo `quoteType` when cached (`EQUITY` is a stock, `ETF` and `MUTUALFUND` are funds). Otherwise use the broker type. Unknown stays unchecked. Only common stocks can be gate-sells. Funds, options, bonds, and cash are never sold for a failed or missing gate.

| Profile | Required gates |
|---|---|
| `compounder` | Trailing earnings positive, revenue growth at least 8%, ROE at least 15%, profit margin at least 10%, debt/equity at most 1.0, free cash flow positive, trailing P/E at most 35, price above the 200-day average. No dividend gate. |
| `dividend` | Trailing yield at least 2.5%, payout ratio at most 60%, free cash flow positive, profitable, debt/equity at most 1.5, price no more than 20% below the 200-day average. No revenue-growth or ROE gate. Missing yield fails. Missing dividend-growth history is unchecked, not a pass, and is not scored. |
| `short_term` | Profitable, revenue growth at least 15%, price above the 50-day average, 20-day return at least 8%. No dividend gate. No 200-day gate. Weeks to a few months, not an intraday screen. |

## Pass 3

Strength is the mean of that profile's required clearances. Pass 1 price and volume do not affect order. Tie-break by symbol. Held names are ranked and marked held. They do not take an advance slot. `advance_n` is 50. Changing it does not reopen dismissals.

Clearance is 0 at the gate and 1 at the better point:

- Compounder: revenue growth 25%, ROE 30%, margin 25%, debt/equity 0, trailing P/E 15, price 15% above the 200-day average. Earnings and free cash flow are 1 if they pass.
- Dividend: yield 6%, payout 30%, debt/equity 0, price at or above the 200-day average. Earnings and free cash flow are 1 if they pass.
- Short-term: revenue growth 40%, 20-day return 25%, price 15% above the 50-day average. Earnings are 1 if they pass.

Nine passers advance nine. Do not pad to 50.

## Pass 4

Start from pass-3 strength. Tag each saved title `reported_result`, `company_forecast`, `product`, or `patent`, and save the title and URL. These tags are not a profit forecast.

Each theme counts once per name, not once per headline. Reported beat or raised guidance +0.05. Company forecast +0.02. Product or new technology +0.02. Patent +0.01. The total bump cannot exceed +0.08.

A headline that reports fraud, an SEC charge, bankruptcy, a restatement, or a criminal case sets strength to 0, ignores the bump, and sorts the name last. It stays in the saved 50 and cannot be in the provisional top 10. The same rule applies to an excluded symbol with no headline. No severe headline means clear of those terms, not reputable. Missing headlines add nothing, stay unchecked, and do not advance. Do not invent a headline.

Profile gates are the only positive test. No review-site or complaint-feed score. No balance-sheet floor beyond the profile gates. Forward profit is not knowable here.

## Account rules

Saved, not shown on the report.

- Weight is broker market value divided by combined net account value across every linked account. Not a Yahoo price. The same symbol in two accounts is one combined weight. Show the account on the saved row.
- Suggest a sale when a held common stock fails the active profile. A held stock with missing facts is unchecked, not a sell.
- Suggest a trim to 10% when any position, including a fund, is above `max_weight` (0.10). A negative quantity is unchecked. A holding with no market value cannot be a trim.
- A dismissal is per profile, symbol, and kind (`buy`, `sell`, `trim`). A Compounder dismissal does not hide that symbol on Dividend. It lasts until a gate in that profile changes. Trim dismissals are account-level and last until `max_weight` changes.

## Report

`/market run` renders only the saved provisional top 10. It does not print pass 1, pass 2, or the advanced 50.

- Chat: one line per name, rank, symbol, company name, how many consecutive days it has been on that profile's top 10, and the other profiles that also list it. Rank is 1 through 10 from the pass-4 order, not the raw strength. Company name is Yahoo `longName`, else `shortName`. If both are missing, show the symbol. Do not invent a name.
- Details: one short block per name, same order. At most the first two sentences of Yahoo `longBusinessSummary`, Yahoo `industry`, and up to 5 saved headline titles with links. A missing field is shown as missing.
- No chart. No expand control.

## Commands

Wire `/market` like `core/assistant/schedule_command.py`. Register actions from `core/actions/bootstrap.py`.

- `/market run` — render the saved top 10. Refetch only if that profile's save is stale.
- `/market profile` — show or switch the active profile.
- `/market criteria` — show the active profile's gates and the shared price and volume limits.
- `/market criteria set <gate> <value>` — write a new version and repeat the new set. `requires_confirmation=True`, same as `update_config`. Chat may set only a key the user named in that turn. A rejected confirmation does not bump the version.
- `/market dismiss <buy|sell|trim> <symbol> [reason]`
- `/market exclude <symbol>` — add or remove an exclusion. An excluded symbol sorts last and cannot be provisional top 10.
- `/market connect` — E*TRADE authorize URL, then the verifier. Secrets: `etrade:consumer_key`, `etrade:consumer_secret`, `etrade:access_token`, `etrade:access_secret`. Alpaca keys use the same store.

A criteria, profile, or dismiss command may echo the setting it just wrote. That echo is not the research screen.

`/market run` reads the saved profile. It does not fetch unless that save is stale. The 6:00 job `market-morning` (`0 6 * * *`) refreshes every profile with no symbol cap. It logs a count. It does not toast names.

## Boundary

`core/market/` is a standalone tool. Iris calls it. The tool does not import the assistant, the UI, the command handler, or the trading bot.

One public entry: `MarketResearch.run(request) -> MarketReport`. The `/market` command and the registered action are thin adapters. They parse arguments, call `run`, and render `MarketReport`. They do not screen, rank, or fetch.

Dependencies are constructor arguments, not globals: listings, bars, quotes, news, holdings, and a store. Each is a small protocol. Yahoo, Alpaca, SearXNG, and E*TRADE are adapters behind those protocols. Tests pass fakes. Adding a profile adds a profile object. It does not edit the ranker. A gate change does not edit the store.

No comments in any new file. No docstrings used as comments. Names carry the meaning.

## Files

New package `core/market/`:

- `criteria.py` — one versioned document per profile, plus shared pass-1 limits. A gate change bumps only that profile's dismissal version. `max_weight` bumps trim dismissals. `advance_n` bumps nothing.
- `universe.py` — Alpaca listings, then the price and volume cut.
- `quotes.py` — Yahoo client behind a protocol.
- `broker.py` — `HoldingsSource`. `EtradeHoldings` uses OAuth 1.0a (`oauthlib`) against `https://api.etrade.com/v1/accounts/list` and the portfolio endpoint. `FileHoldings` reads `symbol`, `quantity`, `market_value`, and `security_class`.
- `news.py` — SearXNG news hits for the advanced 50.
- `research.py` — screen, rank, headlines, snapshot.
- `store.py` — criteria, dismissals, exclusions, and append-only run files under `Data/Market/`. No writes to `knowledge.db`.

## Out of scope

Trading-bot package reuse, intraday screens, article-body research, Yelp, Google reviews, Glassdoor, CFPB, NHTSA, FDA feeds, sector targets, tax lots, wash sales, any E*TRADE order path, charts, and expandable detail.

## Failure

- Yahoo or Alpaca down: use cache. A symbol with no cache is unchecked. Do not invent a quote.
- E*TRADE down or partial: research still returns. Do not trim from a partial total.
- A file holding with no `security_class` or no `market_value` is unchecked.
- A run that cannot score at least one name records the failure. It does not write an empty top 10.
- Criteria writes are all-or-nothing.

## Tests

Fake quote, news, and holdings clients. No live Yahoo, Alpaca, SearXNG, or E*TRADE calls.

- A $1.50 name and a $500 name are not buys. A holding outside the band that fails a quality gate is still a sell.
- A missing required field excludes a name from buys and marks a held stock unchecked, not a sell.
- An ETF with no earnings is not a sell. The same ETF at 12% of combined net value is a trim.
- Pass 2 saves every passer in symbol order. Pass 3 keeps all 60 of a 60-name list and advances 50 non-held names. Nine passers advance nine.
- A Compounder dismissal does not hide that symbol on Dividend. A gate change in that profile shows it again. An `advance_n` change does not.
- A 2% yield fails Dividend. An 8% 20-day return with 10% revenue growth fails Short-term. A name above its 50-day average and below its 200-day average can pass Short-term and fail Compounder.
- An "SEC charges" headline sets pass-4 strength to 0 and keeps the name out of the provisional top 10, even with a patent headline. The name remains in the saved 50. An excluded symbol does the same. A name with no headlines does not advance.
- Five patent headlines add 0.01, not 0.05. Every positive theme adds 0.08, not more. A 0.90 pass-3 name with no news stays ahead of a 0.80 name with the full bump.
- Two accounts holding 6% each of one symbol produce one 12% trim. A failed account suppresses trims.
- The broker client has no order method. The history request asks for 200 trading days, not 5.
- The report chat lines are rank, symbol, and name only. Details contain description, industry, and headline links. No chart markup.

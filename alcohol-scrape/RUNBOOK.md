# Overnight sweep runbook

## Start everything

```bash
./start_sweep.sh
```
Starts the collector, `caffeinate`, and the progress watcher together. Restarting
the collector on its own would orphan `caffeinate` (it is tied to the collector's
pid), so always use this script.

## Verify it is working (do this before walking away)

Two minutes after starting the sweep:

```bash
wc -l data/out/extension_reviews.jsonl     # must be non-zero
```

If it is empty, open DevTools on the sweep tab (Cmd-Opt-J) and look for `[ADC]`
lines in the console:
- `[ADC] sweep page DM_xxxxx` — harvesting correctly
- `[ADC] sweep page failed: ...` — the error is printed there
- `[ADC] sweep landed off-product` — the tab is not on a product page
- nothing at all — the content script is not injected; reload the tab

## Per-product review caps

Set in `scrapers/build_queue.py` (`review_cap`):

| advertised reviews | collected |
|---|---|
| 200 or more | 50 (most recent) |
| 21 - 199 | 20 |
| 20 or fewer | all |

~10,800 reviews over ~2,800 page turns, roughly **3.4 hours** (down from 8.3h
uncapped). Reviews are sorted newest-first, so a cap takes a recent slice.

## Previously running
- `collector.py` (pid 37536) — serves the queue, ingests batches
- `caffeinate -dimsu -w 37536` (pid 37591) — keeps the Mac awake; exits by itself
  when the collector stops, so nothing is left running afterwards
- `watch_sweep.py` — appends progress to `logs/sweep.log` every 5 minutes

## Before you walk away
- **Leave the lid open.** `caffeinate` stops sleep, but closing the lid on a
  MacBook still suspends unless an external display is attached.
- Leave the sweep's Chrome window open; don't browse danmurphys.com.au in another
  tab, since the sweep drives its tab by navigation.
- Leave the terminal running `collector.py` open. Closing it kills the collector,
  and the sweep stops with it.

## In the morning
```bash
tail -30 logs/sweep.log            # per-5-min progress, flags stalls
python3 scrapers/resolve.py        # rebuild catalogue.json
```

## If it stalled overnight
Nothing is lost. Reviews are flushed every 25 pages and deduped by `review_key`,
and a product is only marked done when its final chunk arrives. Restart the
collector, reload the extension, press "Start review sweep" again — it resumes
from the queue position recorded in `data/out/review_sweep_state.json`.

## Expected pace
~45,700 reviews over 606 products, roughly 8 hours. Front-loaded: the top 100
products are 70% of all reviews. A healthy rate is roughly 90-100 reviews/min
early on (the queue is ordered heaviest-first).

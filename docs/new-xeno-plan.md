# New cadence model ("new-xeno")

Replaces per-item Zeno nagging with a **random list-digest** daytime cadence plus
**burst alerts** at expiry and on overnight misses. Branch: `new-xeno`.

## Why

Today, every nag item escalates individually on a Zeno curve toward its deadline,
coalesced behind a global 5-minute gate. Two gaps motivated the redesign:

- **Stuck recurring items.** There is no cycle-end/expiry logic anymore
  (`nag_until`/`max_duration_minutes` were dropped in `migrations.py`). An
  un-checked-off daily stays `active_since`-set forever, so the next cron cycle
  never starts a fresh instance — it just grinds at the 5-min floor on the old
  cycle. Nothing is silently lost, but nothing cleanly rolls over either.
- **No safety net for overnight misses.** An item not done by end of day just
  keeps nagging; there's no deliberate "here's what you missed" moment.

## Decisions (locked)

| Question | Decision |
|---|---|
| Daytime cadence | **Random list digests** — one SMS listing all open items + expire times. Replaces per-item Zeno. |
| Digest frequency | **Random gap 45–120 min** during waking hours (~6–10/day). Tunable via config. |
| Due burst timing | **T−3 / T−2 / T−1**, one message per minute, leading up to each item's expire time. |
| Missed-overnight burst | **With the morning briefing** — 3-message, 1/min burst listing yesterday's misses. |
| Check-off | **Unchanged** — silences the item for the rest of the day. |
| Zeno | **Retired** as the cadence driver. `_compute_deadline_interval` becomes dead code. |

> Note: "random digests" means per-item Zeno escalation goes away. The only
> per-item escalation you feel is the T−3 burst at each expiry. This consciously
> contradicts the earlier "daily recurring just nags using Zeno" assumption.

## Target model

Every item still has an **expire time** = `deadline_at` (one-shots default 11 PM;
recurring = cron-start + `deadline_offset_minutes`). `deadline_at` now drives
*display* and *burst timing* only — not send cadence.

### 1. Daytime = random list digests
- New `app_state` key `next_digest_at`.
- Each tick: if `now ≥ next_digest_at` and not quiet hours → build one SMS listing
  every open today item + its expire time → send → set
  `next_digest_at = now + random(DIGEST_MIN_GAP … DIGEST_MAX_GAP)`.
- Items no longer individually drive sends; `next_nag_at`/Zeno stops being the cadence.

### 2. Due burst = T−3 / T−2 / T−1 (gate-exempt)
- Each tick: find items with `deadline_at` within the next 3 minutes, not done.
- Send one message at each of the −3/−2/−1 marks, tracked per item so exactly 3 fire.
- Multiple items expiring the same minute coalesce into one burst message.
- **Exempt from the global 5-min gate** (the point of the burst). Still respects quiet hours.
- After expiry, an unacknowledged item stops bursting and simply keeps appearing in
  digests (flagged overdue) until done or caught by the morning rollover.

### 3. Missed-overnight = burst with the morning briefing
- At briefing time: items whose expire time was *yesterday* and never checked off = "missed."
- Send a 3-message, 1/min burst listing them, alongside the briefing.
- Then roll over (also fixes the stuck-recurring bug):
  - **Recurring dailies:** force-close yesterday's stale cycle and start today's fresh.
  - **Missed one-shots:** carry onto today's list, re-dated to today, so digests keep surfacing them.

### 4. Check-off (unchanged)
- UI (`/nag/done/{id}`) and "DONE" both run `execute_acknowledge`.
- Removes the item from digests and cancels any pending burst for the day.

## Implementation sketch

- **`app/scheduler.py`**
  - Rewrite `fire_due_nags` → split into `fire_digests` (random list digest) and
    `fire_due_bursts` (T−3/−2/−1, gate-exempt). Wire both into the tick in `main()`.
  - Add overnight rollover + missed-items burst into / next to `fire_morning_briefing`.
  - Retire `_compute_deadline_interval` (dead code) and the Zeno path in firing.
- **`app/config.py`** — add `DIGEST_MIN_GAP` (45), `DIGEST_MAX_GAP` (120) minutes.
- **`app/models.py` / `app/migrations.py`** — per-item burst tracking (e.g. a
  `burst_count` or `last_burst_at` column) + migration; `next_digest_at` lives in `app_state`.
- **`app/openai_client.py`** — digest message generator (list + expire times) and
  missed-items burst copy.
- **`app/context_engine.py`** — reuse `today_items`; add a "missed yesterday" query.

## Open items to confirm before/while coding

- Digest message format (GPT-written vs plain numbered list + times).
- Exact per-item burst-tracking column shape.
- Whether missed one-shots re-date to 11 PM today or keep an "overdue" marker.

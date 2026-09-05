#!/usr/bin/env python3
"""Repair statistics anomalies, drops, and candles in Home Assistant database."""

import sqlite3
import datetime
import shutil
import sys
import os

def repair_db(db_path="/config/home-assistant_v2.db", dry_run=True):
    if not os.path.exists(db_path):
        print(f"Error: Database {db_path} does not exist!")
        return 1

    print(f"==================================================")
    print(f"DATABASE REPAIR TOOL: {db_path}")
    print(f"Mode: {'DRY RUN (no changes)' if dry_run else 'APPLY (modifying database)'}")
    print(f"==================================================")

    if not dry_run:
        bak_path = f"{db_path}.bak_spikerepair_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}"
        print(f"Creating backup: {bak_path} ...")
        shutil.copy2(db_path, bak_path)
        print("Backup created successfully.")

    conn = sqlite3.connect(db_path)
    c = conn.cursor()

    # 1. Clean up MTD financial components & bank balance levels from statistics tables
    # (MTD metrics and bank balances are indicators/levels, not accumulative meters)
    c.execute("""
        SELECT id, statistic_id 
        FROM statistics_meta 
        WHERE statistic_id LIKE '%mtd%'
           OR statistic_id LIKE '%bank_kwh%'
           OR statistic_id LIKE '%bank_pln%'
           OR statistic_id LIKE '%bank_wirtualny%'
    """)
    indicator_metas = dict(c.fetchall())
    print(f"\n[1] Periodic indicators / level sensors to remove from statistics: {len(indicator_metas)}")
    for mid, sid in indicator_metas.items():
        c.execute("SELECT COUNT(*) FROM statistics WHERE metadata_id=?", (mid,))
        cnt = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM statistics_short_term WHERE metadata_id=?", (mid,))
        cnt_st = c.fetchone()[0]
        print(f"    - {sid} (id={mid}): {cnt} stats, {cnt_st} short-term")
        if not dry_run:
            c.execute("DELETE FROM statistics WHERE metadata_id=?", (mid,))
            c.execute("DELETE FROM statistics_short_term WHERE metadata_id=?", (mid,))
            c.execute("DELETE FROM statistics_meta WHERE id=?", (mid,))

    # 2. Clean up daily reset sensors from statistics tables
    # (zuzycie_dzis, produkcja_dzis reset at midnight; they are live display sensors, not panel statistics)
    c.execute("""
        SELECT id, statistic_id 
        FROM statistics_meta 
        WHERE statistic_id LIKE '%zuzycie_dzis%' 
           OR statistic_id LIKE '%produkcja_dzis%'
           OR statistic_id LIKE '%daily_%'
    """)
    daily_metas = dict(c.fetchall())
    print(f"\n[2] Daily reset sensors to remove from statistics: {len(daily_metas)}")
    for mid, sid in daily_metas.items():
        c.execute("SELECT COUNT(*) FROM statistics WHERE metadata_id=?", (mid,))
        cnt = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM statistics_short_term WHERE metadata_id=?", (mid,))
        cnt_st = c.fetchone()[0]
        print(f"    - {sid} (id={mid}): {cnt} stats, {cnt_st} short-term")
        if not dry_run:
            c.execute("DELETE FROM statistics WHERE metadata_id=?", (mid,))
            c.execute("DELETE FROM statistics_short_term WHERE metadata_id=?", (mid,))
            c.execute("DELETE FROM statistics_meta WHERE id=?", (mid,))

    # 3. Clean up panel_energia_* and bank_* spikes / collapses
    c.execute("""
        SELECT id, statistic_id 
        FROM statistics_meta 
        WHERE (statistic_id LIKE '%panel_energia%' OR statistic_id LIKE '%bank_ladowanie%' OR statistic_id LIKE '%bank_rozladowanie%')
    """)
    energy_metas = dict(c.fetchall())
    print(f"\n[3] Checking Energy Dashboard statistics: {len(energy_metas)} entities")

    deleted_rows = 0

    for mid, sid in energy_metas.items():
        c.execute("""
            SELECT id, start_ts, state, sum 
            FROM statistics 
            WHERE metadata_id=? 
            ORDER BY start_ts ASC
        """, (mid,))
        rows = c.fetchall()
        if not rows:
            continue

        to_delete_ids = []
        prev_sum = None
        prev_start_ts = None

        for idx, (row_id, start_ts, state, s) in enumerate(rows):
            is_bogus = False
            dt = datetime.datetime.fromtimestamp(start_ts, tz=datetime.timezone.utc)
            hours = max(1.0, (start_ts - prev_start_ts) / 3600.0) if prev_start_ts else 1.0

            # Check for bogus state (cumulative sensor value leaked into hourly state)
            state_limit = 50.0 if "cost" in sid else 25.0
            if state is not None and (state > state_limit or state < -0.01) and ("stan_licznika" not in sid):
                print(f"    [!] BOGUS STATE in {sid} at {dt.isoformat()}: state={state} (id={row_id})")
                is_bogus = True
            elif prev_sum is not None and s is not None:
                diff = s - prev_sum
                hourly_rate = diff / hours
                rate_limit = 50.0 if "cost" in sid else 25.0
                if diff < -0.05 or hourly_rate > rate_limit:
                    print(f"    [!] BOGUS DELTA in {sid} at {dt.isoformat()}: prev_sum={prev_sum:.3f} -> sum={s:.3f} (diff={diff:.3f}, rate={hourly_rate:.3f}/h) (id={row_id})")
                    is_bogus = True

            if is_bogus:
                to_delete_ids.append(row_id)
            else:
                if s is not None:
                    prev_sum = s
                    prev_start_ts = start_ts

        if to_delete_ids:
            print(f"    -> Deleting {len(to_delete_ids)} bogus rows for {sid}")
            deleted_rows += len(to_delete_ids)
            if not dry_run:
                placeholders = ",".join("?" for _ in to_delete_ids)
                c.execute(f"DELETE FROM statistics WHERE id IN ({placeholders})", to_delete_ids)
                # Also clean short-term stats for this entity
                c.execute(f"DELETE FROM statistics_short_term WHERE metadata_id=?", (mid,))

    if not dry_run:
        conn.commit()
        print("\nDatabase changes committed successfully.")
    else:
        print(f"\nDRY RUN complete. Total rows to delete: {deleted_rows}")

    conn.close()
    return 0

if __name__ == "__main__":
    db_path = "/config/home-assistant_v2.db"
    dry_run = True
    for arg in sys.argv[1:]:
        if arg == "--apply":
            dry_run = False
        elif not arg.startswith("-"):
            db_path = arg
    sys.exit(repair_db(db_path, dry_run=dry_run))

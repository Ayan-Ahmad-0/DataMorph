import duckdb
import time
from pathlib import Path

def generate_unclean_csv(target_path: Path, target_size_mb: int):
    target_path = Path(target_path).resolve()
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_sql = str(target_path).replace("\\", "/")
    
    # Estimate rows needed: ~75 bytes per row
    # 50 MB -> ~700,000 rows
    # 100 MB -> ~1,400,000 rows
    # 200 MB -> ~2,800,000 rows
    approx_rows = int(target_size_mb * 1024 * 1024 / 72)
    
    print(f"Generating ~{target_size_mb} MB ({approx_rows:,} rows) into {target_path.name}...")
    t0 = time.perf_counter()
    
    conn = duckdb.connect()
    
    query = f"""
    COPY (
        WITH raw_events AS (
            SELECT
                range AS id_num,
                -- 1. event_id (with 3% duplicates)
                CASE 
                    WHEN random() < 0.03 THEN 'evt_' || ((range * 0.9)::BIGINT + 10000000)::VARCHAR
                    ELSE 'evt_' || (range + 10000000)::VARCHAR
                END AS event_id,
                
                -- 2. user_id (with whitespace and null tokens)
                CASE
                    WHEN random() < 0.05 THEN (ARRAY['N/A', 'null', 'none', '-', '?', ''])[1 + (random() * 5)::INT]
                    WHEN random() < 0.10 THEN '  usr_' || (1000 + (random() * 49000)::INT)::VARCHAR || '  '
                    ELSE 'usr_' || (1000 + (random() * 49000)::INT)::VARCHAR
                END AS user_id,
                
                -- 3. event_type (mixed casing, whitespace, and null tokens)
                CASE
                    WHEN random() < 0.04 THEN (ARRAY['n/a', 'NULL', 'None', '-'])[1 + (random() * 3)::INT]
                    WHEN random() < 0.08 THEN '  click '
                    WHEN random() < 0.12 THEN 'CHECKOUT'
                    WHEN random() < 0.16 THEN 'SignUp'
                    WHEN random() < 0.20 THEN 'page_view  '
                    ELSE (ARRAY['page_view', 'click', 'signup', 'checkout', 'search', 'logout'])[1 + (random() * 5)::INT]
                END AS event_type,
                
                -- 4. timestamp (mixed date formats, UTC Z, leading whitespace)
                CASE
                    WHEN random() < 0.03 THEN 'N/A'
                    WHEN random() < 0.25 THEN strftime(TIMESTAMP '2025-01-01 00:00:00' + INTERVAL (random() * 31536000) SECOND, '%d/%m/%Y %H:%M:%S')
                    WHEN random() < 0.40 THEN strftime(TIMESTAMP '2025-01-01 00:00:00' + INTERVAL (random() * 31536000) SECOND, '%m/%d/%Y %H:%M:%S')
                    WHEN random() < 0.55 THEN strftime(TIMESTAMP '2025-01-01 00:00:00' + INTERVAL (random() * 31536000) SECOND, '%Y-%m-%dT%H:%M:%SZ')
                    WHEN random() < 0.65 THEN '  ' || strftime(TIMESTAMP '2025-01-01 00:00:00' + INTERVAL (random() * 31536000) SECOND, '%Y-%m-%d %H:%M:%S') || ' '
                    ELSE strftime(TIMESTAMP '2025-01-01 00:00:00' + INTERVAL (random() * 31536000) SECOND, '%Y-%m-%d %H:%M:%S')
                END AS timestamp,
                
                -- 5. platform (mixed casing and null tokens)
                CASE
                    WHEN random() < 0.04 THEN '?'
                    WHEN random() < 0.08 THEN 'IOS'
                    WHEN random() < 0.12 THEN '  windows'
                    WHEN random() < 0.16 THEN 'Android'
                    WHEN random() < 0.20 THEN 'MACOS'
                    ELSE (ARRAY['web', 'ios', 'android', 'macos', 'windows'])[1 + (random() * 4)::INT]
                END AS platform,
                
                -- 6. session_duration_sec (negative outliers, null tokens, empty strings)
                CASE
                    WHEN random() < 0.03 THEN '-99'
                    WHEN random() < 0.05 THEN (-(10 + (random() * 500)::INT))::VARCHAR
                    WHEN random() < 0.08 THEN 'N/A'
                    WHEN random() < 0.10 THEN ''
                    ELSE (1 + (random() * 3599)::INT)::VARCHAR
                END AS session_duration_sec,
                
                -- 7. revenue (currency symbols, negative amounts, null tokens)
                CASE
                    WHEN random() < 0.05 THEN 'null'
                    WHEN random() < 0.08 THEN '-' || round(random() * 50, 2)::VARCHAR
                    WHEN random() < 0.15 THEN '$' || round(10 + (random() * 990), 2)::VARCHAR
                    ELSE round(random() * 500, 2)::VARCHAR
                END AS revenue
                
            FROM range({approx_rows})
        )
        SELECT event_id, user_id, event_type, timestamp, platform, session_duration_sec, revenue
        FROM raw_events
    ) TO '{target_sql}' (HEADER, DELIMITER ',');
    """
    
    conn.execute(query)
    conn.close()
    
    actual_size_mb = target_path.stat().st_size / (1024 * 1024)
    elapsed = time.perf_counter() - t0
    print(f"Generated {target_path.name}: {actual_size_mb:.2f} MB ({actual_size_mb * 1024 * 1024:,.0f} bytes) in {elapsed:.2f}s")
    return actual_size_mb

if __name__ == "__main__":
    b_dir = Path("data/benchmarks")
    generate_unclean_csv(b_dir / "events_unclean_50mb.csv", 50)
    generate_unclean_csv(b_dir / "events_unclean_100mb.csv", 100)
    generate_unclean_csv(b_dir / "events_unclean_200mb.csv", 200)

"""Admin-only aggregate Search Console snapshots, never visitor keyword guesses."""
import csv
import io
import json
import math
import re
from datetime import date, datetime
from pathlib import Path
import growth
import store
from admin_time import PACIFIC

PROPERTY = 'https://www.tuitionping.com/'
LINK = 'https://search.google.com/search-console/performance/search-analytics?resource_id=https%3A%2F%2Fwww.tuitionping.com%2F'

def ensure_table():
    growth.ensure_tables()
    with store.db() as conn:
        conn.execute(store.pg_ddl('CREATE TABLE IF NOT EXISTS search_query_imports (id INTEGER PRIMARY KEY AUTOINCREMENT, imported_at TEXT NOT NULL, start_date TEXT NOT NULL, end_date TEXT NOT NULL, queries_json TEXT NOT NULL, submitted_by INTEGER)'))

def parse_export(raw):
    if len(raw) > 524288: raise ValueError('Export exceeds 512 KB.')
    try:
        text = raw.decode('utf-8-sig')
    except UnicodeError:
        raise ValueError('Use the UTF-8 CSV export from Search Console.')
    reader = csv.DictReader(io.StringIO(text))
    fields = reader.fieldnames or []
    query_key = next((f for f in fields if f.strip().lower() in {'top queries','query','queries'}), None)
    names = {f.strip().lower(): f for f in fields}
    if not query_key or not {'clicks','impressions','position'} <= names.keys():
        raise ValueError('Upload Queries.csv with Top queries, Clicks, Impressions and Position columns.')
    rows, seen = [], set()
    for i,row in enumerate(reader):
        if i >= 1000: raise ValueError('Export is limited to 1,000 query rows.')
        query = (row.get(query_key) or '').strip()
        if not query: continue
        if len(query)>200 or re.search(r'[^\s]+@[^\s]+|\d[\d ()+.-]{8,}\d',query):
            raise ValueError('Remove queries containing personal contact information or over 200 characters.')
        if query in seen: raise ValueError('Duplicate query rows are not supported.')
        try:
            clicks = int(row[names['clicks']].replace(',',''))
            impressions = int(row[names['impressions']].replace(',',''))
            position = float(row[names['position']])
            if not 0 <= clicks <= impressions <= 100000000 or not math.isfinite(position) or not 0 <= position <= 1000:
                raise ValueError()
        except (ValueError,TypeError,KeyError,AttributeError):
            raise ValueError('Query metrics must be valid nonnegative Search Console numbers.')
        rows.append({'query':query,'clicks':clicks,'impressions':impressions,
                     'ctr':100*clicks/impressions if impressions else 0,'position':position})
        seen.add(query)
    if not rows: raise ValueError('The export has no query rows. Keep the current snapshot until Google provides data.')
    return sorted(rows,key=lambda r:(-r['clicks'],-r['impressions'],r['query']))

def save(raw,start,end,provider_id):
    try:
        start_day,end_day = date.fromisoformat(start),date.fromisoformat(end)
        if start_day>end_day or end_day>datetime.now(PACIFIC).date(): raise ValueError()
    except (ValueError,TypeError):
        raise ValueError('Enter a valid export date range ending no later than today.')
    rows = parse_export(raw)
    ensure_table()
    with store.db() as conn:
        conn.execute('INSERT INTO search_query_imports (imported_at,start_date,end_date,queries_json,submitted_by) VALUES (?,?,?,?,?)',
                     (store.now_iso(),start,end,json.dumps(rows),provider_id))

def report():
    ensure_table()
    with store.db() as conn:
        saved = conn.execute('SELECT * FROM search_query_imports ORDER BY id DESC LIMIT 1').fetchone()
    if saved:
        result = {'property':PROPERTY,'fetched_at':saved['imported_at'],'start_date':saved['start_date'],
                  'end_date':saved['end_date'],'queries':json.loads(saved['queries_json']),'source':'Admin-uploaded Search Console export'}
    else:
        result = json.loads((Path(__file__).parent/'content/search-console-baseline.json').read_text())
    result.update(link=LINK,clicks=sum(r['clicks'] for r in result['queries']),
                  impressions=sum(r['impressions'] for r in result['queries']))
    return result

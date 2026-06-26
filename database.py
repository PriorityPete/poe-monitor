import sqlite3
import datetime
import re
import os

DB_FILE = '/data/sales.db'

def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS sales (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_name TEXT NOT NULL,
            price_raw TEXT,
            amount REAL,
            currency TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            signature TEXT,
            ignored INTEGER DEFAULT 0,
            item_json TEXT
        )
    ''')
    try:
        c.execute('ALTER TABLE sales ADD COLUMN ignored INTEGER DEFAULT 0')
    except sqlite3.OperationalError:
        pass
    try:
        c.execute('ALTER TABLE sales ADD COLUMN item_json TEXT')
    except sqlite3.OperationalError:
        pass
    c.execute('''
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    ''')
    conn.commit()
    conn.close()

def get_last_check():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT value FROM settings WHERE key = 'last_check'")
    row = c.fetchone()
    conn.close()
    return row[0] if row else None

def set_last_check():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('last_check', ?)", (now,))
    conn.commit()
    conn.close()

def get_last_sale_batch():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT value FROM settings WHERE key = 'last_sale_batch'")
    row = c.fetchone()
    conn.close()
    return row[0] if row else None

def set_last_sale_batch(ts_str):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('last_sale_batch', ?)", (ts_str,))
    conn.commit()
    conn.close()

def parse_price(price_str):
    # Format usually: "25x Chaos Orb" or "1x Divine Orb"
    # Sometimes just "Chaos Orb" (implies 1x?) -> Usually trade site says "1x" check?
    # Let's assume standard format "Nx Currency"
    
    # Regex for "123.5x Currency Name" or "123x Currency Name"
    match = re.search(r'([\d\.]+)[xX]\s+(.+)', price_str)
    if match:
        try:
            amount = float(match.group(1))
            currency = match.group(2).strip()
            return amount, currency
        except:
            pass
    
    return 0.0, price_str

def add_sale(item_name, price_raw, timestamp=None, ignored=0, item_json=None):
    amount, currency = parse_price(price_raw)
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    
    # Create signature
    signature = f"{item_name}|{price_raw}"
    
    if not timestamp:
        timestamp = datetime.datetime.now()
    
    c.execute('''
        INSERT INTO sales (item_name, price_raw, amount, currency, signature, timestamp, ignored, item_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    ''', (item_name, price_raw, amount, currency, signature, timestamp, ignored, item_json))
    
    rowid = c.lastrowid
    conn.commit()
    conn.close()
    return rowid

def get_recent_sales(limit=50, currency=None, min_amount=None, start_date=None, end_date=None, 
                      sort_by='date_desc', page=1, search=None):
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    
    query = "SELECT * FROM sales WHERE ignored = 0"
    params = []
    
    if currency and currency != 'All':
        query += " AND currency LIKE ?"
        params.append(f"%{currency}%")
        
    if min_amount:
        try:
            val = float(min_amount)
            query += " AND amount >= ?"
            params.append(val)
        except ValueError:
            pass
            
    if start_date:
        query += " AND date(timestamp) >= date(?)"
        params.append(start_date)
        
    if end_date:
        query += " AND date(timestamp) <= date(?)"
        params.append(end_date)
        
    if search:
        query += " AND item_name LIKE ?"
        params.append(f"%{search}%")
    
    # Sorting
    if sort_by == 'date_asc':
        query += " ORDER BY timestamp ASC"
    elif sort_by == 'amount_desc':
        query += " ORDER BY amount DESC"
    elif sort_by == 'amount_asc':
        query += " ORDER BY amount ASC"
    else:  # default: date_desc
        query += " ORDER BY timestamp DESC"
    
    # Pagination
    offset = (page - 1) * limit
    query += " LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    
    c.execute(query, tuple(params))
    rows = c.fetchall()
    conn.close()
    return [dict(row) for row in rows]

def get_total_count():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM sales WHERE ignored = 0")
    count = c.fetchone()[0]
    conn.close()
    return count

def get_stats():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    
    # Sum predefined currencies
    # We focus on Chaos Orb, Divine Orb, Exalted Orb
    stats = {
        'Chaos Orb': 0.0,
        'Divine Orb': 0.0,
        'Exalted Orb': 0.0
    }
    
    c.execute('''
        SELECT currency, SUM(amount) 
        FROM sales 
        WHERE currency IN ('Chaos Orb', 'Divine Orb', 'Exalted Orb') AND ignored = 0
        GROUP BY currency
    ''')
    
    for row in c.fetchall():
        currency = row[0]
        total = row[1]
        if currency in stats:
            stats[currency] = total
            
    conn.close()
    return stats

def get_last_n_signatures(n=10):
    """Returns the signatures of the last n items inserted into DB"""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT signature FROM sales ORDER BY id DESC LIMIT ?', (n,))
    rows = c.fetchall()
    conn.close()
    return [row[0] for row in rows]

def get_wealth_history():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    
    # Get all sales sorted by time
    c.execute('''
        SELECT timestamp, currency, amount 
        FROM sales 
        WHERE currency IN ('Chaos Orb', 'Divine Orb', 'Exalted Orb') AND ignored = 0
        ORDER BY timestamp ASC
    ''')
    
    rows = c.fetchall()
    conn.close()

    # Process into cumulative data
    # Structure: {'labels': [time1, time2...], 'Divine Orb': [0, 1, 1...], ...}
    
    data = {
        'labels': [],
        'Divine Orb': [],
        'Exalted Orb': [],
        'Chaos Orb': []
    }
    
    current_totals = {
        'Divine Orb': 0.0,
        'Exalted Orb': 0.0,
        'Chaos Orb': 0.0
    }
    
    # We want to enable significant points. 
    # For a simple graph, just adding every sale point might be okay if N is small.
    # If N is large, we might want to group by hour/day. 
    # For now, let's just do every sale event to be accurate.
    
    for row in rows:
        ts = row[0]
        curr = row[1]
        amt = row[2]
        
        # Parse timestamp to simpler string if needed, but JS handles ISO okay.
        # Let's clean it to "YYYY-MM-DD HH:MM"
        try:
            dt = datetime.datetime.strptime(ts, "%Y-%m-%d %H:%M:%S.%f")
        except ValueError:
            try:
                dt = datetime.datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
            except:
                dt = datetime.datetime.now() # Fallback
                
        label = dt.strftime("%Y-%m-%d %H:%M")
        
        if curr in current_totals:
            current_totals[curr] += amt
            
            data['labels'].append(label)
            # Append current state of ALL currencies at this timestamp
            # This creates a stepped graph effect where every point has a value for all lines
            data['Divine Orb'].append(current_totals['Divine Orb'])
            data['Exalted Orb'].append(current_totals['Exalted Orb'])
            data['Chaos Orb'].append(current_totals['Chaos Orb'])
            
    return data

def backup_db():
    if not os.path.exists(DB_FILE):
        return None
    import shutil
    timestamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    backup_file = f"/data/sales_backup_{timestamp}.db"
    shutil.copy2(DB_FILE, backup_file)
    return backup_file

def reset_db_with_baseline(baseline_sales):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    # Clear sales table
    c.execute("DELETE FROM sales")
    # Reset last_sale_batch and last_check in settings table
    c.execute("DELETE FROM settings WHERE key IN ('last_sale_batch', 'last_check')")
    conn.commit()
    conn.close()
    
    # Insert baseline sales
    for sale in reversed(baseline_sales):
        add_sale(sale['item'], sale['price'], ignored=1)

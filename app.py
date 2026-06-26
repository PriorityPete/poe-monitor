import logging
import os

# Config early for all modules
LOG_FILE = '/data/app.log'
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("App")

from flask import Flask, render_template, redirect, url_for, jsonify
app = Flask(__name__)
import threading
import time
import schedule
import database
import monitor

def run_schedule():
    """Background thread to run schedule"""
    # Initial check delay to let web server start? No need.
    # Run once immediately? Monitor loop does that.
    logger.info("Scheduler thread started")
    
    # Define job from monitor
    check_interval = int(os.environ.get('CHECK_INTERVAL', 15))
    schedule.every(check_interval).minutes.do(monitor.check_and_update)
    
    # Run once at startup (after a slight delay to ensure DB is ready)
    time.sleep(5) 
    monitor.check_and_update()
    
    while True:
        schedule.run_pending()
        time.sleep(1)

@app.route('/')
def index():
    from flask import request
    import datetime as dt
    
    currency = request.args.get('currency')
    min_amount = request.args.get('min_amount')
    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')
    sort_by = request.args.get('sort', 'date_desc')
    page = int(request.args.get('page', 1))
    search = request.args.get('search')
    
    sales = database.get_recent_sales(
        limit=25,
        currency=currency,
        min_amount=min_amount,
        start_date=start_date,
        end_date=end_date,
        sort_by=sort_by,
        page=page,
        search=search
    )
    stats = database.get_stats()
    chart_data = database.get_wealth_history()
    total_count = database.get_total_count()
    last_refresh = database.get_last_check() or "Never"
    last_sale_batch = database.get_last_sale_batch() or ""
    
    check_interval = int(os.environ.get('CHECK_INTERVAL', 15))
    trade_search_id = os.environ.get('TRADE_SEARCH_ID', '')
    
    return render_template('index.html', 
                           sales=sales, 
                           stats=stats, 
                           chart_data=chart_data,
                           total_count=total_count,
                           last_refresh=last_refresh,
                           last_sale_batch=last_sale_batch,
                           current_page=page,
                           check_interval=check_interval,
                           trade_search_id=trade_search_id)

@app.route('/refresh', methods=['POST'])
def refresh_route():
    logger.info("Manual refresh triggered")
    monitor.check_and_update(force=True)
    return redirect(url_for('index'))

@app.route('/update_session', methods=['POST'])
def update_session():
    from flask import request
    new_session = request.form.get('session_id', '').strip()
    if new_session:
        # Save to persistent file
        with open('/data/session.txt', 'w') as f:
            f.write(new_session)
        logger.info("Session ID updated via web UI")
        # Update monitor's global variable
        import monitor as mon
        mon.POESESSID = new_session
    return redirect(url_for('index'))

@app.route('/reset', methods=['POST'])
def reset_route():
    logger.info("Database reset triggered")
    try:
        # 1. Fetch current sales from web as baseline
        current_sales = monitor.get_sales_from_web()
        logger.info(f"Fetched {len(current_sales)} sales from web to use as baseline")
        
        # 2. Backup and Reset in Database
        backup_file = database.backup_db()
        if backup_file:
            logger.info(f"Database backed up to {backup_file}")
        else:
            logger.warning("No database file found to backup, proceeding with reset")
            
        database.reset_db_with_baseline(current_sales)
        logger.info("Database reset and baseline populated successfully")
    except Exception as e:
        logger.error(f"Error during database reset: {e}")
        
    return redirect(url_for('index'))

@app.route('/api/backups')
def api_backups():
    import glob
    import os
    import datetime
    
    backup_dir = '/data'
    files = glob.glob(os.path.join(backup_dir, 'sales_backup_*.db'))
    backups = []
    for f in files:
        name = os.path.basename(f)
        size = os.path.getsize(f)
        size_str = f"{size / 1024:.1f} KB"
        
        dt_str = "Unknown"
        try:
            parts = name.replace('sales_backup_', '').replace('.db', '').split('_')
            if len(parts) == 2:
                dt = datetime.datetime.strptime(f"{parts[0]}{parts[1]}", "%Y%m%d%H%M%S")
                dt_str = dt.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            pass
            
        backups.append({
            'filename': name,
            'size': size_str,
            'date': dt_str
        })
        
    backups.sort(key=lambda x: x['filename'], reverse=True)
    return {'backups': backups}

@app.route('/restore', methods=['POST'])
def restore_route():
    import re
    from flask import request, abort
    
    filename = request.form.get('filename', '').strip()
    if not re.match(r'^sales_backup_\d{8}_\d{6}\.db$', filename):
        logger.error(f"Invalid backup filename format for restore: '{filename}'")
        abort(400, "Invalid backup filename format")
        
    backup_file = os.path.join('/data', filename)
    if not os.path.exists(backup_file):
        logger.error(f"Backup file not found for restore: {backup_file}")
        abort(404, "Backup file not found")
        
    logger.info(f"Restoring database from: {filename}")
    try:
        current_backup = database.backup_db()
        if current_backup:
            logger.info(f"Created safety backup at {current_backup} before restoring")
            
        import shutil
        shutil.copy2(backup_file, '/data/sales.db')
        logger.info("Database restored successfully")
    except Exception as e:
        logger.error(f"Failed to restore database: {e}")
        abort(500, "Failed to restore database")
        
    return redirect(url_for('index'))

@app.route('/delete_backup/<filename>', methods=['POST'])
def delete_backup_route(filename):
    import re
    from flask import abort
    
    if not re.match(r'^sales_backup_\d{8}_\d{6}\.db$', filename):
        logger.error(f"Invalid backup filename format for delete: '{filename}'")
        abort(400, "Invalid backup filename format")
        
    backup_file = os.path.join('/data', filename)
    if not os.path.exists(backup_file):
        logger.error(f"Backup file not found for delete: {backup_file}")
        abort(404, "Backup file not found")
        
    logger.info(f"Deleting backup file: {filename}")
    try:
        os.remove(backup_file)
        logger.info("Backup file deleted successfully")
    except Exception as e:
        logger.error(f"Failed to delete backup file: {e}")
        abort(500, "Failed to delete backup file")
        
    return redirect(url_for('index'))

@app.route('/get_logs')
def get_logs():
    try:
        if os.path.exists(LOG_FILE):
            with open(LOG_FILE, 'r') as f:
                lines = f.readlines()
                return "".join(lines[-100:]) # Return last 100 lines
        return "Log file not found."
    except Exception as e:
        return f"Error reading logs: {e}"

# Caching search result details globally to support fast pagination, sorting, and filtering
shop_items_cache = []
shop_items_cache_time = 0

@app.route('/api/active_listings')
def api_active_listings():
    global shop_items_cache, shop_items_cache_time
    import time
    import requests
    from urllib.parse import quote
    from flask import request
    
    page = int(request.args.get('page', 1))
    limit = int(request.args.get('limit', 20))
    search_query = request.args.get('search', '').strip()
    currency_query = request.args.get('currency', '').strip()
    sort_option = request.args.get('sort', 'default').strip()
    
    now = time.time()
    
    # We fetch the search results and details if cache is empty or older than 60 minutes
    if not shop_items_cache or (now - shop_items_cache_time) > 3600:
        query_hash = os.environ.get('TRADE_SEARCH_ID', '')
        if not query_hash:
            logger.error("TRADE_SEARCH_ID not set!")
            return jsonify({'error': 'TRADE_SEARCH_ID is not configured.'}), 400
        
        session_id = monitor.get_session_id()
        league = os.environ.get('POE_LEAGUE', 'Runes of Aldur')
        
        search_url = f"https://www.pathofexile.com/api/trade2/search/poe2/{quote(league)}/{query_hash}"
        cookies = {'POESESSID': session_id} if session_id else {}
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
            'Accept': 'application/json',
            'X-Requested-With': 'XMLHttpRequest',
            'Referer': 'https://www.pathofexile.com/trade2/history',
        }
        
        try:
            logger.info(f"Fetching active listings search from: {search_url}")
            r = requests.post(search_url, cookies=cookies, headers=headers, json={}, timeout=20)
            if r.status_code != 200:
                logger.error(f"Search API returned HTTP {r.status_code}: {r.text[:200]}")
                return jsonify({'error': f"Failed to search: HTTP {r.status_code}"}), r.status_code
                
            search_data = r.json()
            search_result_ids = search_data.get('result', [])
            search_query_id = search_data.get('id', '')
            
            logger.info(f"Retrieved {len(search_result_ids)} listing IDs. Fetching details...")
            
            # Fetch details for ALL IDs in chunks of 10 to build local cache
            fetched_listings = []
            if search_result_ids:
                for i in range(0, len(search_result_ids), 10):
                    chunk = search_result_ids[i:i+10]
                    fetch_url = f"https://www.pathofexile.com/api/trade2/fetch/{','.join(chunk)}?query={search_query_id}"
                    logger.info(f"Fetching details chunk: {i//10 + 1}")
                    
                    r_fetch = requests.get(fetch_url, cookies=cookies, headers=headers, timeout=20)
                    if r_fetch.status_code == 200:
                        fetch_data = r_fetch.json()
                        for item_wrapper in fetch_data.get('result', []):
                            item = item_wrapper.get('item', {})
                            listing = item_wrapper.get('listing', {})
                            price = listing.get('price', {})
                            
                            # Format price
                            amount = price.get('amount', 1)
                            currency = monitor.currency_text(price.get('currency', ''))
                            amount_str = str(int(amount)) if amount == int(amount) else f"{amount:.1f}".rstrip('0').rstrip('.')
                            price_str = f"{amount_str}x {currency}"
                            
                            # Build name
                            name = item.get('name', '').strip()
                            type_line = item.get('typeLine', '').strip()
                            item_name = f"{name} {type_line}".strip() if name else (type_line or 'Unknown')
                            
                            fetched_listings.append({
                                'item_name': item_name,
                                'price': price_str,
                                'currency': currency,
                                'amount': amount,
                                'item_json': item,
                                'indexed': listing.get('indexed', '')
                            })
                    else:
                        logger.error(f"Fetch API returned HTTP {r_fetch.status_code} for chunk")
                        
            shop_items_cache = fetched_listings
            shop_items_cache_time = now
            logger.info(f"Cached {len(shop_items_cache)} detailed shop items.")
            
        except Exception as e:
            logger.error(f"Error building shop items cache: {e}")
            return jsonify({'error': str(e)}), 500
            
    # Apply filtering to the cached list
    filtered_items = list(shop_items_cache)
    
    if search_query:
        search_query_lower = search_query.lower()
        filtered_items = [item for item in filtered_items if search_query_lower in item['item_name'].lower()]
        
    if currency_query:
        filtered_items = [item for item in filtered_items if currency_query.lower() in item['currency'].lower()]
        
    # Apply sorting
    if sort_option == 'price_asc':
        # Cheap first: Sort by currency priority (Exalt -> Chaos -> Divine) and then amount ascending
        def get_price_asc_key(item):
            curr = item.get('currency', '')
            amt = item.get('amount', 0.0)
            if 'Exalt' in curr:
                priority = 1
            elif 'Chaos' in curr:
                priority = 2
            elif 'Divine' in curr:
                priority = 3
            else:
                priority = 99
            return (priority, amt)
        filtered_items.sort(key=get_price_asc_key)
    elif sort_option == 'date_desc':
        filtered_items.sort(key=lambda x: x.get('indexed', ''), reverse=True)
    elif sort_option == 'date_asc':
        filtered_items.sort(key=lambda x: x.get('indexed', ''))
    else:  # 'default': Expensive first: Divine -> Chaos -> Exalted
        def get_expensive_first_key(item):
            curr = item.get('currency', '')
            amt = item.get('amount', 0.0)
            if 'Divine' in curr:
                priority = 3
            elif 'Chaos' in curr:
                priority = 2
            elif 'Exalt' in curr:
                priority = 1
            else:
                priority = 0
            return (-priority, -amt)
        filtered_items.sort(key=get_expensive_first_key)
        
    # Apply pagination slicing
    offset = (page - 1) * limit
    page_items = filtered_items[offset:offset+limit]
    
    return jsonify({
        'listings': page_items,
        'total': len(filtered_items),
        'page': page,
        'limit': limit
    })

if __name__ == '__main__':
    # Initialize DB
    database.init_db()
    
    # Start Scheduler in Background
    t = threading.Thread(target=run_schedule)
    t.daemon = True
    t.start()
    
    # Start Web Server
    # Host 0.0.0.0 is important for Docker
    app.run(host='0.0.0.0', port=5000)

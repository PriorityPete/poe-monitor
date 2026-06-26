import os
import time
import requests
import json
import logging
from urllib.parse import quote
import database
import datetime

# Configuration
# Try to read from persistent file first, fall back to env var
def get_session_id():
    if os.path.exists('/data/session.txt'):
        with open('/data/session.txt', 'r') as f:
            return f.read().strip()
    return os.environ.get('POESESSID', '')

POESESSID = get_session_id()
NTFY_URL = os.environ.get('NTFY_URL', '')
TOPIC = os.environ.get('NTFY_TOPIC', '')

# Determine APP_URL for notification links
from urllib.parse import urlparse
APP_URL = os.environ.get('APP_URL') or os.environ.get('MONITOR_URL')
if not APP_URL:
    try:
        parsed = urlparse(NTFY_URL)
        if parsed.hostname:
            APP_URL = f"http://{parsed.hostname}:8060"
        else:
            APP_URL = "http://localhost:8060"
    except Exception:
        APP_URL = "http://localhost:8060"

logger = logging.getLogger(__name__)

def send_notification(item, price, sale_id=None):
    """Send ntfy notification for a sale."""
    title = f"Sale: {item}"
    message = f"Sold for: {price}"
    
    headers = {
        "Title": title.encode('utf-8'),
        "Priority": "default",
        "Tags": "moneybag,poe2"
    }
    
    if sale_id and APP_URL:
        headers["Click"] = f"{APP_URL}/#sale-{sale_id}"
    
    try:
        url = f"{NTFY_URL}/{TOPIC}"
        requests.post(url, data=message.encode('utf-8'), headers=headers)
        logger.info(f"Notification sent for: {item} (sale_id: {sale_id})")
    except Exception as e:
        logger.error(f"Failed to send notification: {e}")

def send_session_expired_notification():
    """Send high priority ntfy notification when session expired."""
    headers = {
        "Title": "⚠️ Session Expired".encode('utf-8'),
        "Priority": "high",
        "Tags": "warning,poe2"
    }
    message = "POESESSID is invalid or expired. Please update it."
    
    try:
        url = f"{NTFY_URL}/{TOPIC}"
        requests.post(url, data=message.encode('utf-8'), headers=headers)
        logger.warning("Session expiry notification sent.")
    except Exception as e:
        logger.error(f"Failed to send session expiry notification: {e}")

# Currency code → display name mapping for PoE2
CURRENCY_NAMES = {
    'divine':          'Divine Orb',
    'exalted':         'Exalted Orb',
    'chaos':           'Chaos Orb',
    'gold':            'Gold',
    'annul':           'Orb of Annulment',
    'regal':           'Regal Orb',
    'vaal':            'Vaal Orb',
    'blessed':         'Blessed Orb',
    'alch':            'Orb of Alchemy',
    'alt':             'Orb of Alteration',
    'aug':             'Orb of Augmentation',
    'chance':          'Orb of Chance',
    'chrom':           'Chromatic Orb',
    'fuse':            'Orb of Fusing',
    'gcp':             "Gemcutter's Prism",
    'jewellers':       "Jeweller's Orb",
    'regret':          'Orb of Regret',
    'scour':           'Orb of Scouring',
    'transmute':       'Orb of Transmutation',
    'artificersstone': "Artificer's Stone",
    'glassblower':     "Glassblower's Bauble",
    'breach':          'Breach Splinter',
    'mirror':          'Mirror of Kalandra',
}

def currency_text(code):
    """Convert a currency code to its display name."""
    return CURRENCY_NAMES.get(code, code)

def time_ago(iso_timestamp):
    """Convert an ISO 8601 timestamp to a human-readable relative string."""
    try:
        ts = datetime.datetime.fromisoformat(iso_timestamp.replace('Z', '+00:00'))
        now = datetime.datetime.now(datetime.timezone.utc)
        seconds = (now - ts).total_seconds()
        if seconds < 3600:
            m = max(1, int(seconds / 60))
            return f"{m} minute{'s' if m != 1 else ''} ago"
        elif seconds < 86400:
            h = int(seconds / 3600)
            return f"{h} hour{'s' if h != 1 else ''} ago"
        else:
            d = int(seconds / 86400)
            return f"{d} day{'s' if d != 1 else ''} ago"
    except Exception:
        return iso_timestamp

def get_sales_from_web():
    """Fetch sales history via the PoE Trade JSON API.

    The trade history page is a Vue.js SPA – sales are loaded dynamically
    after the user selects a league and clicks Refresh. The internal API
    endpoint used by the browser is:
        GET /api/trade2/history/{league_id}
    We call that directly instead of scraping HTML.
    """
    if not POESESSID:
        logger.error("POESESSID not set!")
        return []

    league = os.environ.get('POE_LEAGUE', 'Runes of Aldur')
    api_url = f'https://www.pathofexile.com/api/trade2/history/{quote(league)}'

    cookies = {'POESESSID': POESESSID}
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
        'Accept': 'application/json',
        'X-Requested-With': 'XMLHttpRequest',
        'Referer': 'https://www.pathofexile.com/trade2/history',
    }

    logger.info(f"Fetching history from API: {api_url}")

    try:
        response = requests.get(api_url, cookies=cookies, headers=headers, timeout=30)

        if response.status_code in (401, 403):
            logger.error(f"Authentication failed (HTTP {response.status_code}) – session expired?")
            send_session_expired_notification()
            return []

        if response.status_code == 429:
            logger.warning("Rate limited (HTTP 429). Will retry next cycle.")
            return []

        if response.status_code != 200:
            logger.error(f"API returned unexpected status: {response.status_code}")
            return []

        data = response.json()
        # API returns {"result": [...]} or a bare list
        entries = data.get('result', data) if isinstance(data, dict) else data
        logger.info(f"API returned {len(entries)} history entries for league '{league}'")

        sales = []
        for entry in entries:
            try:
                # Build item name – same logic as the Vue template:
                # name + typeLine when name exists, otherwise just typeLine
                item_data = entry.get('item') or {}
                name      = item_data.get('name', '').strip()
                type_line = item_data.get('typeLine', '').strip()
                item_name = f"{name} {type_line}".strip() if name else (type_line or 'Unknown')

                # Build price string
                price_data = entry.get('price', {})
                amount     = price_data.get('amount', 1)
                currency   = currency_text(price_data.get('currency', ''))
                price_str  = f"{amount}x {currency}"

                # Build relative time for the signature
                time_raw  = entry.get('time', '')
                time_text = time_ago(time_raw) if time_raw else ''

                sales.append({
                    'item':      item_name,
                    'price':     price_str,
                    'signature': f"{item_name}|{price_str}|{time_text}",
                    'item_json': json.dumps(item_data)
                })
            except Exception as e:
                logger.error(f"Error parsing entry: {e}")
                continue

        return sales

    except ValueError as e:
        logger.error(f"Failed to parse JSON response: {e}")
        return []
    except Exception as e:
        logger.error(f"Failed to fetch history: {e}")
        return []

def check_and_update(force=False):
    logger.info("Checking if trade history update is needed...")
    
    # Persistent Rate Limiting Check
    last_check_str = database.get_last_check()
    if last_check_str and not force:
        try:
            last_check = datetime.datetime.strptime(last_check_str, "%Y-%m-%d %H:%M")
            now = datetime.datetime.now()
            diff = (now - last_check).total_seconds() / 60
            
            check_interval = int(os.environ.get('CHECK_INTERVAL', 15))
            if diff < check_interval:
                logger.info(f"Skipping check. Last check was {diff:.1f} minutes ago (Interval: {check_interval}m)")
                return
        except Exception as e:
            logger.error(f"Error checking last check time: {e}")

    logger.info("Checking trade history...")
    database.set_last_check()  # Record when we checked
    current_sales = get_sales_from_web()
    
    if not current_sales:
        return

    # Get last signatures from DB to find overlap
    # We get last 20 to be safe
    old_signatures = database.get_last_n_signatures(20)
    
    new_sales_found = []
    
    if not old_signatures:
        # DB is empty. Import all, but only notify for newest 3 to avoid spam.
        logger.info("First run or empty DB. Importing all found sales.")
        batch_ts = datetime.datetime.now()
        sale_ids = {}
        for sale in reversed(current_sales):
            sid = database.add_sale(sale['item'], sale['price'], timestamp=batch_ts, item_json=sale.get('item_json'))
            sale_ids[sale['signature']] = sid
        
        # Record this as the latest batch
        database.set_last_sale_batch(batch_ts.strftime("%Y-%m-%d %H:%M:%S"))
        
        # Only alert for top 3 newest
        for sale in current_sales[:3]:
            sid = sale_ids.get(sale['signature'])
            send_notification(sale['item'], sale['price'], sale_id=sid)
        return

    # Normal update: Find where new list starts deviating from old list
    # The 'current_sales' list has Newest at index 0.
    # old_signatures is ordered by ID DESC (newest first)
    
    # IMPROVED LOGIC: Match a SEQUENCE of signatures, not just one.
    # This handles cases where multiple items have the same name+price.
    
    # Build web signatures list (full with time)
    web_signatures_full = [s['signature'] for s in current_sales]
    
    # Also build a version without time for matching legacy DB entries
    # Legacy format: "item|price", New format: "item|price|time"
    def strip_time(sig):
        parts = sig.rsplit('|', 1)
        if len(parts) == 2 and ('ago' in parts[1] or 'hour' in parts[1] or 'minute' in parts[1] or 'day' in parts[1]):
            return parts[0]
        return sig
    
    web_signatures_stripped = [strip_time(s) for s in web_signatures_full]
    
    # Check if DB signatures are legacy (no time) or new (with time)
    first_db_sig = old_signatures[0]
    db_is_legacy = not ('ago' in first_db_sig or 'hour' in first_db_sig or 'minute' in first_db_sig)
    
    # Use appropriate comparison list
    if db_is_legacy:
        web_sigs_for_match = web_signatures_stripped
        logger.info(f"[MATCH] DB uses LEGACY format (no time). Stripping time from web sigs for comparison.")
    else:
        web_sigs_for_match = web_signatures_full
        logger.info(f"[MATCH] DB uses NEW format (with time). Using full web sigs for comparison.")
    
    logger.info(f"[MATCH] First DB sig: '{first_db_sig}'")
    logger.info(f"[MATCH] First web sig (for match): '{web_sigs_for_match[0] if web_sigs_for_match else 'NONE'}'")
    
    cutoff_index = -1
    found_overlap = False
    
    # Find all positions where the first DB signature appears in web list
    candidate_positions = [i for i, sig in enumerate(web_sigs_for_match) if sig == first_db_sig]
    
    logger.info(f"[MATCH] Found {len(candidate_positions)} candidate position(s)")
    
    # For each candidate, check if the following entries also match
    for pos in candidate_positions:
        match_count = 0
        db_sigs_to_check = [strip_time(s) if db_is_legacy else s for s in old_signatures[:10]]
        
        for offset, db_sig in enumerate(db_sigs_to_check):
            web_idx = pos + offset
            if web_idx >= len(web_sigs_for_match):
                break
            if web_sigs_for_match[web_idx] == db_sig:
                match_count += 1
            else:
                break
        
        # If we matched at least 3 in sequence (or all available), we found our overlap point
        if match_count >= min(3, len(old_signatures)):
            cutoff_index = pos
            found_overlap = True
            logger.info(f"[MATCH] ✓ Overlap found at position {pos} with {match_count} matching signatures")
            break
    
    if found_overlap:
        new_sales_found = current_sales[:cutoff_index]
        if new_sales_found:
            logger.info(f"[RESULT] Found {len(new_sales_found)} NEW sale(s):")
            for s in new_sales_found[:5]:
                logger.info(f"  → {s['item'][:40]} for {s['price']}")
        else:
            logger.info(f"[RESULT] No new sales (overlap at position 0)")
    else:
        # No sequence match found - be conservative
        logger.warning("[RESULT] ✗ No sequence overlap found with DB history.")
        logger.warning("[RESULT] Being conservative - not importing anything to avoid duplicates.")
        logger.warning("[RESULT] If items are genuinely new, manual refresh or next check will catch them.")
        new_sales_found = []

    # Process new sales (Oldest to Newest)
    if new_sales_found:
        logger.info(f"Found {len(new_sales_found)} new sales.")
        batch_ts = datetime.datetime.now()
        for sale in reversed(new_sales_found):
            sale_id = database.add_sale(sale['item'], sale['price'], timestamp=batch_ts, item_json=sale.get('item_json'))
            send_notification(sale['item'], sale['price'], sale_id=sale_id)
        
        # Record this as the latest batch
        database.set_last_sale_batch(batch_ts.strftime("%Y-%m-%d %H:%M:%S"))
    else:
        logger.info("No new sales.")

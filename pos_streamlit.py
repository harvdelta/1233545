import os
import time
import hmac
import hashlib
import requests
import re
import pandas as pd
import streamlit as st
from streamlit_autorefresh import st_autorefresh
import gspread
import json
from datetime import datetime

# Auto-refresh every 15 seconds
st_autorefresh(interval=15000)
st.set_page_config(layout="wide")

# ---------- CONFIG ----------
API_KEY = st.secrets["DELTA_API_KEY"]
API_SECRET = st.secrets["DELTA_API_SECRET"]
BASE_URL = st.secrets.get("DELTA_BASE_URL", "https://api.india.delta.exchange")
TG_BOT_TOKEN = st.secrets.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT_ID = st.secrets.get("TELEGRAM_CHAT_ID", "")

# Google Sheets Config
GOOGLE_SHEET_ID = st.secrets["GOOGLE_SHEET_ID"]
GOOGLE_CREDENTIALS = json.loads(st.secrets["GOOGLE_SERVICE_ACCOUNT_JSON"])

# ---------- Google Sheets Functions ----------
@st.cache_resource
def get_google_client():
    """Initialize Google Sheets client with credentials"""
    try:
        gc = gspread.service_account_from_dict(GOOGLE_CREDENTIALS)
        return gc
    except Exception as e:
        st.error(f"Failed to initialize Google Sheets client: {e}")
        return None

def load_alerts_from_sheet():
    """Load alerts from Google Sheets into session state"""
    try:
        gc = get_google_client()
        if not gc:
            return False
            
        # Open the spreadsheet
        sheet = gc.open_by_key(GOOGLE_SHEET_ID)
        
        # Try to get the worksheet
        try:
            worksheet = sheet.worksheet("Delta Alerts")
        except gspread.WorksheetNotFound:
            # Sheet doesn't exist, no alerts to load
            return True
        
        # Get all values
        values = worksheet.get_all_values()
        
        # Skip if empty or only headers
        if len(values) <= 1:
            return True
            
        # Parse alerts (skip header row)
        loaded_alerts = []
        for row in values[1:]:  # Skip header
            if len(row) >= 6 and row[0]:  # Now expecting 6 columns with triggered_at
                try:
                    loaded_alerts.append({
                        "symbol": row[0],
                        "criteria": row[1],
                        "condition": row[2],
                        "threshold": float(row[3]),
                        "status": row[4] if len(row) > 4 else "Active",
                        "triggered_at": row[5] if len(row) > 5 else None
                    })
                except (ValueError, IndexError):
                    continue  # Skip invalid rows
        
        # Update session state with all alerts
        st.session_state.alerts = loaded_alerts
        return True
        
    except Exception as e:
        st.error(f"Error loading from Google Sheets: {e}")
        return False

def update_google_sheet():
    """Update Google Sheets with current alerts"""
    try:
        gc = get_google_client()
        if not gc:
            return False
            
        # Open the spreadsheet
        sheet = gc.open_by_key(GOOGLE_SHEET_ID)
        
        # Try to get the worksheet, create if it doesn't exist
        try:
            worksheet = sheet.worksheet("Delta Alerts")
        except gspread.WorksheetNotFound:
            worksheet = sheet.add_worksheet(title="Delta Alerts", rows="100", cols="10")
        
        # Prepare data with headers (now includes triggered_at column)
        headers = ["Symbol", "Criteria", "Condition", "Threshold", "Status", "Triggered At"]
        data = [headers]
        
        # Add current alerts
        for alert in st.session_state.alerts:
            row = [
                alert["symbol"],
                alert["criteria"], 
                alert["condition"],
                alert["threshold"],
                alert.get("status", "Active"),
                alert.get("triggered_at", "")
            ]
            data.append(row)
        
        # Clear existing content and write new data
        worksheet.clear()
        if data:
            worksheet.update(range_name="A1", values=data)
        
        return True
        
    except Exception as e:
        st.error(f"Error updating Google Sheets: {e}")
        return False

# ---------- helpers ----------
def sign_request(method: str, path: str, payload: str, timestamp: str) -> str:
    sig_data = method + timestamp + path + payload
    return hmac.new(API_SECRET.encode(), sig_data.encode(), hashlib.sha256).hexdigest()

def api_get(path: str, timeout=15):
    timestamp = str(int(time.time()))
    method = "GET"
    payload = ""
    signature = sign_request(method, path, payload, timestamp)
    headers = {
        "Accept": "application/json",
        "api-key": API_KEY,
        "signature": signature,
        "timestamp": timestamp,
    }
    url = BASE_URL.rstrip("/") + path
    resp = requests.get(url, headers=headers, timeout=timeout)
    resp.raise_for_status()
    return resp.json()

def to_float(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None

def detect_underlying(product: dict, fallback_symbol: str):
    if not isinstance(product, dict):
        product = {}
    for key in ("underlying_symbol", "underlying", "base_asset_symbol", "settlement_asset_symbol"):
        val = product.get(key)
        if isinstance(val, str):
            v = val.upper()
            if "BTC" in v:
                return "BTC"
            if "ETH" in v:
                return "ETH"
    spot = product.get("spot_index") or {}
    if isinstance(spot, dict):
        s = (spot.get("symbol") or "").upper()
        if "BTC" in s:
            return "BTC"
        if "ETH" in s:
            return "ETH"
    txt = (fallback_symbol or "").upper()
    m = re.search(r"\b(BTC|ETH)\b", txt)
    if m:
        return m.group(1)
    if "BTC" in txt:
        return "BTC"
    if "ETH" in txt:
        return "ETH"
    return None

def send_telegram_message(text):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TG_CHAT_ID, "text": text}
    try:
        requests.post(url, data=payload, timeout=5)
    except:
        pass

def badge_upnl(val):
    try:
        num = float(val)
    except:
        return val
    if num > 0:
        return f"<span style='padding:4px 8px;border-radius:6px;background:#4CAF50;color:white;font-weight:bold;'>{num:.2f}</span>"
    elif num < 0:
        return f"<span style='padding:4px 8px;border-radius:6px;background:#F44336;color:white;font-weight:bold;'>{num:.2f}</span>"
    else:
        return f"<span style='padding:4px 8px;border-radius:6px;background:#999;color:white;font-weight:bold;'>{num:.2f}</span>"

def create_alert_id(alert):
    """Create unique ID for alert tracking"""
    return f"{alert['symbol']}_{alert['criteria']}_{alert['condition']}_{alert['threshold']}"

def check_and_trigger_alerts(df, total_upnl):
    """Check alerts and trigger only once, then auto-deactivate"""
    alerts_triggered = 0
    
    for i, alert in enumerate(st.session_state.alerts):
        # Only check active alerts
        if alert.get("status", "Active") != "Active":
            continue
        
        current_value = None
        symbol = alert["symbol"]
        
        # Handle Total P&L alerts
        if symbol == "TOTAL_PNL":
            current_value = total_upnl
        else:
            # Handle individual position alerts
            row = df[df["Symbol"] == symbol]
            if row.empty:
                continue
                
            # Get current value
            val_str = row.iloc[0].get(alert["criteria"])
            try:
                current_value = float(val_str)
            except:
                continue
        
        if current_value is None:
            continue
        
        # Check if condition is met
        condition_met = False
        if alert["condition"] == ">=":
            condition_met = current_value >= alert["threshold"]
        else:  # "<="
            condition_met = current_value <= alert["threshold"]
        
        # If condition is met, trigger alert and deactivate
        if condition_met:
            # Create alert message
            if symbol == "TOTAL_PNL":
                alert_msg = (f"🚨 TOTAL P&L ALERT TRIGGERED!\n"
                            f"Condition: Total P&L {alert['condition']} {alert['threshold']}\n"
                            f"Current Total P&L: {current_value:.2f} USD\n"
                            f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            else:
                alert_msg = (f"🚨 POSITION ALERT TRIGGERED!\n"
                            f"Symbol: {symbol}\n"
                            f"Condition: {alert['criteria']} {alert['condition']} {alert['threshold']}\n"
                            f"Current Value: {current_value:.2f}\n"
                            f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            
            # Send telegram notification
            send_telegram_message(alert_msg)
            
            # Show alert in Streamlit
            if symbol == "TOTAL_PNL":
                st.error(f"🚨 TOTAL P&L ALERT: {current_value:.2f} USD ({alert['condition']} {alert['threshold']})")
            else:
                st.error(f"🚨 ALERT: {symbol} - {alert['criteria']} is {current_value:.2f} ({alert['condition']} {alert['threshold']})")
            
            # Auto-deactivate the alert
            st.session_state.alerts[i]["status"] = "Triggered"
            st.session_state.alerts[i]["triggered_at"] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            
            alerts_triggered += 1
    
    # Update Google Sheets if any alerts were triggered
    if alerts_triggered > 0:
        update_google_sheet()
        st.success(f"✅ {alerts_triggered} alert(s) triggered and auto-deactivated!")
    
    return alerts_triggered

# ---------- fetch data ----------
positions_j = api_get("/v2/positions/margined")
positions = positions_j.get("result", []) if isinstance(positions_j, dict) else []

tickers_j = api_get("/v2/tickers")
tickers = tickers_j.get("result", []) if isinstance(tickers_j, dict) else []

# ---------- BTC/ETH index map ----------
index_map = {}
for t in tickers:
    sym = (t.get("symbol") or "").upper()
    price = t.get("index_price") or t.get("spot_price") or t.get("last_traded_price") or t.get("mark_price")
    price = to_float(price)
    if not price:
        continue
    if "BTC" in sym and "USD" in sym and "BTC" not in index_map:
        index_map["BTC"] = price
    if "ETH" in sym and "USD" in sym and "ETH" not in index_map:
        index_map["ETH"] = price

# ---------- process positions ----------
rows = []
for p in positions:
    product = p.get("product") or {}
    contract_symbol = product.get("symbol") or p.get("symbol") or ""
    size_lots = to_float(p.get("size"))
    underlying = detect_underlying(product, contract_symbol)

    entry_price = to_float(p.get("entry_price"))
    mark_price = to_float(p.get("mark_price"))

    index_price = p.get("index_price") or product.get("index_price")
    if isinstance(index_price, dict):
        index_price = index_price.get("index_price") or index_price.get("price")
    if index_price is None and isinstance(product.get("spot_index"), dict):
        index_price = product["spot_index"].get("index_price") or product["spot_index"].get("spot_price")
    if index_price is None and underlying and underlying in index_map:
        index_price = index_map[underlying]
    index_price = to_float(index_price)

    upnl_val = None
    size_coins = None
    if size_lots is not None and underlying:
        lots_per_coin = {"BTC": 1000.0, "ETH": 100.0}.get(underlying, 1.0)
        size_coins = size_lots / lots_per_coin
    if entry_price is not None and mark_price is not None and size_coins is not None:
        if size_coins < 0:
            upnl_val = (entry_price - mark_price) * abs(size_coins)
        else:
            upnl_val = (mark_price - entry_price) * abs(size_coins)

    rows.append({
        "Symbol": contract_symbol,
        "Size (lots)": f"{size_lots:.0f}" if size_lots is not None else None,
        "Size (coins)": f"{size_coins:.2f}" if size_coins is not None else None,
        "Entry Price": f"{entry_price:.2f}" if entry_price is not None else None,
        "Index Price": f"{index_price:.2f}" if index_price is not None else None,
        "Mark Price": f"{mark_price:.2f}" if mark_price is not None else None,
        "UPNL (USD)": f"{upnl_val:.2f}" if upnl_val is not None else None
    })

df = pd.DataFrame(rows)

# Sort by absolute UPNL
df = df.sort_values(by="UPNL (USD)", key=lambda x: x.map(lambda v: abs(float(v)) if v else -999999), ascending=False).reset_index(drop=True)

# ---------- CALCULATE TOTAL P&L ----------
total_upnl = 0
valid_upnl_count = 0
for _, row in df.iterrows():
    upnl_str = row.get("UPNL (USD)")
    if upnl_str:
        try:
            upnl_val = float(upnl_str)
            total_upnl += upnl_val
            valid_upnl_count += 1
        except:
            continue

# ---------- STATE ----------
if "alerts" not in st.session_state:
    st.session_state.alerts = []
if "edit_symbol" not in st.session_state:
    st.session_state.edit_symbol = None
if "sheets_updated" not in st.session_state:
    st.session_state.sheets_updated = False

# Load alerts from Google Sheets on every refresh (auto-sync)
load_alerts_from_sheet()

# ---------- SMART ALERT CHECK (FIRE ONCE ONLY) ----------
# This replaces your old alert checking logic
alerts_triggered = check_and_trigger_alerts(df, total_upnl)

# ---------- CSS ----------
st.markdown("""
<style>
.full-width-table {width: 100%; border-collapse: collapse;}
.full-width-table th {text-align: center; font-weight: bold; color: #999; padding: 8px;}
.full-width-table td {text-align: center; font-family: monospace; padding: 8px; white-space: nowrap;}
.symbol-cell {text-align: left !important; font-weight: bold; font-family: monospace;}
.alert-btn {background-color: transparent; border: 1px solid #666; border-radius: 6px; padding: 0 8px; font-size: 18px; cursor: pointer; color: #aaa;}
.alert-btn:hover {background-color: #444;}
.sheets-status {padding: 8px; border-radius: 4px; margin: 8px 0; text-align: center; font-weight: bold;}
.sheets-success {background-color: #4CAF50; color: white;}
.sheets-error {background-color: #F44336; color: white;}
.status-badge {padding: 2px 8px; border-radius: 12px; font-size: 12px; font-weight: bold;}
.status-active {background-color: #4CAF50; color: white;}
.status-triggered {background-color: #FF9800; color: white;}
.status-inactive {background-color: #666; color: white;}
</style>
""", unsafe_allow_html=True)

# ---------- LAYOUT ----------
# Show Total P&L at the top
total_col1, total_col2, total_col3 = st.columns([2, 2, 1])

with total_col1:
    st.markdown("### 💰 Total Portfolio P&L")
    
with total_col2:
    # Display total P&L with color coding
    if total_upnl > 0:
        st.markdown(f"<div style='background:#4CAF50;padding:15px;border-radius:10px;text-align:center;'>"
                   f"<h2 style='color:white;margin:0;'>+${total_upnl:.2f}</h2>"
                   f"<p style='color:white;margin:0;opacity:0.9;'>Positions: {valid_upnl_count}</p>"
                   f"</div>", unsafe_allow_html=True)
    elif total_upnl < 0:
        st.markdown(f"<div style='background:#F44336;padding:15px;border-radius:10px;text-align:center;'>"
                   f"<h2 style='color:white;margin:0;'>${total_upnl:.2f}</h2>"
                   f"<p style='color:white;margin:0;opacity:0.9;'>Positions: {valid_upnl_count}</p>"
                   f"</div>", unsafe_allow_html=True)
    else:
        st.markdown(f"<div style='background:#666;padding:15px;border-radius:10px;text-align:center;'>"
                   f"<h2 style='color:white;margin:0;'>${total_upnl:.2f}</h2>"
                   f"<p style='color:white;margin:0;opacity:0.9;'>Positions: {valid_upnl_count}</p>"
                   f"</div>", unsafe_allow_html=True)

with total_col3:
    # Quick Total P&L alert button
    if st.button("🔔 Set Total P&L Alert", help="Set alert on total portfolio P&L"):
        st.session_state.show_total_pnl_form = True

# Show Total P&L Alert Form
if st.session_state.get("show_total_pnl_form", False):
    with st.expander("🎯 Total P&L Alert Setup", expanded=True):
        pnl_col1, pnl_col2, pnl_col3, pnl_col4 = st.columns([2, 2, 2, 2])
        
        with pnl_col1:
            pnl_condition = st.selectbox("Condition", [">=", "<="], key="pnl_condition")
        
        with pnl_col2:
            pnl_threshold = st.number_input("Threshold ($)", format="%.2f", value=0.0, key="pnl_threshold")
        
        with pnl_col3:
            if st.button("💾 Save Total P&L Alert"):
                if pnl_threshold != 0.0:
                    # Check for duplicate
                    duplicate = False
                    for existing_alert in st.session_state.alerts:
                        if (existing_alert["symbol"] == "TOTAL_PNL" and 
                            existing_alert["condition"] == pnl_condition and
                            existing_alert["threshold"] == pnl_threshold):
                            duplicate = True
                            break
                    
                    if not duplicate:
                        new_alert = {
                            "symbol": "TOTAL_PNL",
                            "criteria": "Total P&L",
                            "condition": pnl_condition,
                            "threshold": pnl_threshold,
                            "status": "Active",
                            "triggered_at": None
                        }
                        st.session_state.alerts.append(new_alert)
                        
                        if update_google_sheet():
                            st.success("✅ Total P&L alert saved!")
                        else:
                            st.error("❌ Failed to sync to sheets")
                        
                        st.session_state.show_total_pnl_form = False
                        st.experimental_rerun()
                    else:
                        st.warning("⚠️ This Total P&L alert already exists!")
                else:
                    st.warning("⚠️ Please enter a non-zero threshold!")
        
        with pnl_col4:
            if st.button("❌ Cancel"):
                st.session_state.show_total_pnl_form = False
                st.experimental_rerun()

st.markdown("---")

left_col, right_col = st.columns([4, 1])

# --- LEFT: TABLE ---
if not df.empty:
    table_html = "<table class='full-width-table'><thead><tr>"
    for col in df.columns:
        table_html += f"<th>{col.upper()}</th>"
    table_html += "<th>ALERT</th></tr></thead><tbody>"

    for idx, row in df.iterrows():
        table_html += "<tr>"
        for col in df.columns:
            if col == "Symbol":
                table_html += f"<td class='symbol-cell'>{row[col]}</td>"
            elif col == "UPNL (USD)":
                table_html += f"<td>{badge_upnl(row[col])}</td>"
            else:
                table_html += f"<td>{row[col]}</td>"
        table_html += f"<td><button class='alert-btn' onclick=\"alert('Click the + button in the right column for {row['Symbol']}')\">+</button></td>"
        table_html += "</tr>"

    table_html += "</tbody></table>"
    left_col.markdown(table_html, unsafe_allow_html=True)

# --- RIGHT: ALERT EDITOR ---
right_col.subheader("Create Alert")

# Symbol selector
symbol_options = ["Select a symbol..."] + df["Symbol"].tolist()
selected_symbol = right_col.selectbox("Choose Symbol", symbol_options, key="symbol_selector")

if selected_symbol != "Select a symbol...":
    # Get selected row data
    sel_row = df[df["Symbol"] == selected_symbol].iloc[0]
    upnl_val = float(sel_row["UPNL (USD)"]) if sel_row["UPNL (USD)"] else 0
    header_bg = "#4CAF50" if upnl_val > 0 else "#F44336" if upnl_val < 0 else "#999"
    
    right_col.markdown(f"<div style='background:{header_bg};padding:10px;border-radius:8px;margin:10px 0;'><b>{selected_symbol}</b></div>", unsafe_allow_html=True)
    right_col.markdown(f"**UPNL (USD):** {badge_upnl(sel_row['UPNL (USD)'])}", unsafe_allow_html=True)
    right_col.markdown(f"**Mark Price:** {sel_row['Mark Price']}")

    with right_col.form("alert_form", clear_on_submit=True):
        criteria_choice = st.selectbox("Criteria", ["UPNL (USD)", "Mark Price"])
        condition_choice = st.selectbox("Condition", [">=", "<="])
        threshold_value = st.number_input("Threshold", format="%.2f", value=0.0)
        
        submitted = st.form_submit_button("💾 Save Alert")
        
        if submitted and threshold_value != 0.0:
            # Check for duplicate alerts
            duplicate = False
            for existing_alert in st.session_state.alerts:
                if (existing_alert["symbol"] == selected_symbol and 
                    existing_alert["criteria"] == criteria_choice and
                    existing_alert["condition"] == condition_choice and
                    existing_alert["threshold"] == threshold_value):
                    duplicate = True
                    break
            
            if not duplicate:
                # Add alert to session state with Active status
                new_alert = {
                    "symbol": selected_symbol,
                    "criteria": criteria_choice,
                    "condition": condition_choice,
                    "threshold": threshold_value,
                    "status": "Active",
                    "triggered_at": None
                }
                st.session_state.alerts.append(new_alert)
                
                # Update Google Sheets
                if update_google_sheet():
                    st.success("✅ Alert saved and synced to Google Sheets!")
                else:
                    st.error("❌ Alert saved locally, but failed to sync to Google Sheets")
                
                # Reset form
                time.sleep(1)  # Brief delay before rerun
                st.experimental_rerun()
            else:
                st.warning("⚠️ This exact alert already exists!")
        elif submitted and threshold_value == 0.0:
            st.warning("⚠️ Please enter a non-zero threshold value!")
else:
    right_col.info("👆 Select a symbol above to create an alert")

# --- ACTIVE ALERTS ---
st.subheader("Alert Management")

# Separate active and inactive alerts
active_alerts = [alert for alert in st.session_state.alerts if alert.get("status", "Active") == "Active"]
inactive_alerts = [alert for alert in st.session_state.alerts if alert.get("status", "Active") in ["Inactive", "Triggered"]]

# Show alerts in two columns (keeping original layout)
alert_col1, alert_col2 = st.columns(2)

# Active Alerts Column
with alert_col1:
    st.markdown("### 🟢 Active Alerts")
    if active_alerts:
        for i, alert in enumerate(st.session_state.alerts):
            if alert.get("status", "Active") == "Active":
                cols = st.columns([4, 1, 1])
                alert_text = f"{alert['symbol']} | {alert['criteria']} {alert['condition']} {alert['threshold']}"
                cols[0].write(alert_text)
                
                # Deactivate button
                if cols[1].button("⏸️", key=f"deactivate_{alert['symbol']}_{i}", help="Deactivate"):
                    st.session_state.alerts[i]["status"] = "Inactive"
                    if update_google_sheet():
                        st.success("Alert deactivated!")
                    st.experimental_rerun()
                
                # Delete button
                if cols[2].button("❌", key=f"delete_{alert['symbol']}_{i}", help="Delete"):
                    st.session_state.alerts.pop(i)
                    if update_google_sheet():
                        st.success("Alert deleted!")
                    st.experimental_rerun()
    else:
        st.write("No active alerts.")

# Inactive/Triggered Alerts Column  
with alert_col2:
    st.markdown("### ⏸️ Triggered/Inactive Alerts")
    if inactive_alerts:
        for i, alert in enumerate(st.session_state.alerts):
            if alert.get("status", "Active") in ["Inactive", "Triggered"]:
                cols = st.columns([4, 1, 1])
                alert_text = f"{alert['symbol']} | {alert['criteria']} {alert['condition']} {alert['threshold']}"
                
                # Show status badge and triggered time if available
                status = alert.get("status", "Inactive")
                triggered_time = alert.get("triggered_at", "")
                if status == "Triggered" and triggered_time:
                    cols[0].write(f"🔥 {alert_text}")
                    cols[0].caption(f"Triggered: {triggered_time}")
                else:
                    cols[0].write(alert_text)
                
                # Reactivate button
                if cols[1].button("▶️", key=f"reactivate_{alert['symbol']}_{i}", help="Reactivate"):
                    st.session_state.alerts[i]["status"] = "Active"
                    st.session_state.alerts[i]["triggered_at"] = None
                    if update_google_sheet():
                        st.success("Alert reactivated!")
                    st.experimental_rerun()
                
                # Delete button
                if cols[2].button("❌", key=f"delete_inactive_{alert['symbol']}_{i}", help="Delete"):
                    st.session_state.alerts.pop(i)
                    if update_google_sheet():
                        st.success("Alert deleted!")
                    st.experimental_rerun()
    else:
        st.write("No triggered/inactive alerts.")

# Manual sync buttons (for backup/manual control)
sync_col1, sync_col2 = st.columns([1, 1])
with sync_col1:
    if st.button("🔄 Force Sync from Sheets"):
        if load_alerts_from_sheet():
            st.success("✅ Force loaded from Sheets!")
        else:
            st.error("❌ Load failed")

with sync_col2:
    if st.button("📤 Force Sync to Sheets"):
        if update_google_sheet():
            st.success("✅ Force synced to Sheets!")
        else:
            st.error("❌ Sync failed")

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

# Auto-refresh every 3 seconds
st_autorefresh(interval=3000)
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
        
        # Prepare data with headers
        headers = ["Symbol", "Criteria", "Condition", "Threshold"]
        data = [headers]
        
        # Add current alerts
        for alert in st.session_state.alerts:
            row = [
                alert["symbol"],
                alert["criteria"], 
                alert["condition"],
                alert["threshold"]
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

# ---------- STATE ----------
if "alerts" not in st.session_state:
    st.session_state.alerts = []
if "triggered" not in st.session_state:
    st.session_state.triggered = set()
if "edit_symbol" not in st.session_state:
    st.session_state.edit_symbol = None
if "sheets_updated" not in st.session_state:
    st.session_state.sheets_updated = False

# ---------- ALERT CHECK ----------
for alert in st.session_state.alerts:
    row = df[df["Symbol"] == alert["symbol"]]
    if row.empty:
        continue
    val_str = row.iloc[0].get(alert["criteria"])
    try:
        val = float(val_str)
    except:
        continue
    cond = (val >= alert["threshold"]) if alert["condition"] == ">=" else (val <= alert["threshold"])
    if cond:
        send_telegram_message(f"ALERT: {alert['symbol']} {alert['criteria']} {alert['condition']} {alert['threshold']}")

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
</style>
""", unsafe_allow_html=True)

# ---------- LAYOUT ----------
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

    with right_col.form("alert_form"):
        criteria_choice = st.selectbox("Criteria", ["UPNL (USD)", "Mark Price"])
        condition_choice = st.selectbox("Condition", [">=", "<="])
        threshold_value = st.number_input("Threshold", format="%.2f")
        
        if st.form_submit_button("💾 Save Alert"):
            # Add alert to session state
            st.session_state.alerts.append({
                "symbol": selected_symbol,
                "criteria": criteria_choice,
                "condition": condition_choice,
                "threshold": threshold_value
            })
            
            # Update Google Sheets
            if update_google_sheet():
                st.success("Alert saved and uploaded to Google Sheets!")
            else:
                st.error("Alert saved locally, but failed to upload to Google Sheets")
            
            # Reset the selector
            st.session_state.symbol_selector = "Select a symbol..."
            st.experimental_rerun()
else:
    right_col.info("👆 Select a symbol above to create an alert")

# --- ACTIVE ALERTS ---
st.subheader("Active Alerts")

# Show Google Sheets status
col1, col2 = st.columns([3, 1])
with col1:
    if st.session_state.alerts:
        for i, alert in enumerate(st.session_state.alerts):
            cols = st.columns([5, 1])
            alert_text = f"{alert['symbol']} | {alert['criteria']} {alert['condition']} {alert['threshold']}"
            cols[0].write(alert_text)
            if cols[1].button("❌", key=f"remove_alert_{i}"):
                st.session_state.alerts.pop(i)
                # Update Google Sheets after deletion
                if update_google_sheet():
                    st.success("Alert deleted and Google Sheets updated!")
                else:
                    st.error("Alert deleted locally, but failed to update Google Sheets")
                st.experimental_rerun()
    else:
        st.write("No active alerts.")

with col2:
    if st.button("🔄 Sync to Sheets"):
        if update_google_sheet():
            st.success("✅ Synced!")
        else:
            st.error("❌ Sync failed")

# --- GOOGLE SHEETS CONNECTION STATUS ---
st.sidebar.subheader("Google Sheets Status")
try:
    gc = get_google_client()
    if gc:
        sheet = gc.open_by_key(GOOGLE_SHEET_ID)
        st.sidebar.success(f"✅ Connected to: {sheet.title}")
        st.sidebar.info(f"📊 Total alerts: {len(st.session_state.alerts)}")
    else:
        st.sidebar.error("❌ Connection failed")
except Exception as e:
    st.sidebar.error(f"❌ Error: {str(e)[:50]}...")

# Show sheet link
st.sidebar.markdown(f"[📋 Open Google Sheet](https://docs.google.com/spreadsheets/d/{GOOGLE_SHEET_ID}/edit)")

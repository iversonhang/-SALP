import streamlit as st
import yfinance as yf
import pandas as pd
import plotly.express as px
import requests
from bs4 import BeautifulSoup
import xml.etree.ElementTree as ET
from datetime import datetime
import json
import os

# Configure advanced page environment
st.set_page_config(page_title="Situational Awareness Intelligence Terminal", layout="wide")

st.title("🦅 Situational Awareness LP - Advanced Terminal")
st.caption(f"Portal Sync Engine: Active | Live Execution: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} HKT")
st.markdown("---")

FUND_CIK = "0002045724"
DB_FILE = "situational_history.json"

# --- DATABASE MANAGEMENT SUBSYSTEM ---
def load_historical_snapshot():
    """Loads previous cycle data to track trade deltas (buys/sells)."""
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, 'r') as f:
                return json.load(f)
        except:
            return {}
    return {}

def save_current_snapshot(df, filing_date):
    """Commits current positions to database for next cycle comparison."""
    if df.empty:
        return
    snapshot = {
        'filing_date': filing_date,
        'positions': df.set_index('Company')['Disclosed_Shares'].to_dict()
    }
    with open(DB_FILE, 'w') as f:
        json.dump(snapshot, f, indent=4)

# --- SEC MULTI-FORM SCRAPER ENGINE ---
@st.cache_data(ttl=43200) # Cache structural scrape for 12 hours
def scrape_sec_alpha_stream(cik):
    """Fetches the most recent filing timeline, alerting on 13F, 13D, and 13G documents."""
    headers = {'User-Agent': 'Research Portal admin@researchportal.com'}
    sec_url = f"https://data.sec.gov/submissions/CIK{cik.zfill(10)}.json"
    
    response = requests.get(sec_url, headers=headers)
    if response.status_code != 200:
        st.error(f"SEC Portal Connection Interrupted (Status: {response.status_code}).")
        return pd.DataFrame(), "Unknown"
        
    data = response.json()
    recent_filings = data['filings']['recent']
    
    # Audit recent timeline for urgent structural actions (13D / 13G Accumulation)
    st.sidebar.subheader("Regulatory Activity Timeline")
    urgent_alerts = 0
    for i in range(min(15, len(recent_filings['form']))):
        f_type = recent_filings['form'][i]
        f_date = recent_filings['filingDate'][i]
        if f_type in ['13D', '13G']:
            st.sidebar.warning(f"🚨 Major Stake Event: Form {f_type} registered on {f_date}")
            urgent_alerts += 1
    if urgent_alerts == 0:
        st.sidebar.info("No active 13D/13G ownership spike forms detected this period.")

    # Isolate current holdings base via 13F-HR
    idx = -1
    for i, form_type in enumerate(recent_filings['form']):
        if form_type == '13F-HR':
            idx = i
            break
            
    if idx == -1:
        st.error("No valid 13F holdings statement discovered.")
        return pd.DataFrame(), "Unknown"
        
    acc_num = recent_filings['accessionNumber'][idx].replace('-', '')
    filing_date = recent_filings['filingDate'][idx]
    
    # Process filing data sheet
    dir_url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc_num}/"
    dir_response = requests.get(dir_url, headers=headers)
    soup = BeautifulSoup(dir_response.text, 'html.parser')
    
    xml_path = None
    for link in soup.find_all('a'):
        href = link.get('href', '')
        if href.endswith('.xml') and 'primary_doc' not in href.lower():
            xml_path = href
            break
            
    if not xml_path:
        for link in soup.find_all('a'):
            href = link.get('href', '')
            if href.endswith('.xml'):
                xml_path = href
                
    if not xml_path:
        st.error("Filing structure missing data blocks.")
        return pd.DataFrame(), filing_date
        
    xml_url = f"https://www.sec.gov{xml_path}"
    xml_response = requests.get(xml_url, headers=headers)
    
    root = ET.fromstring(xml_response.content)
    for el in root.iter():
        if '}' in el.tag:
            el.tag = el.tag.split('}', 1)[1]
            
    records = []
    for info in root.findall('infoTable'):
        name = info.find('nameOfIssuer').text if info.find('nameOfIssuer') is not None else "Unknown"
        shares = int(info.find('shrsOrPrntAmt/sshPrnamt').text) if info.find('shrsOrPrntAmt/sshPrnamt') is not None else 0
        value_thousands = int(info.find('value').text) if info.find('value') is not None else 0
        
        put_call = info.find('putCall')
        option_type = f" ({put_call.text.upper()})" if put_call is not None and put_call.text else ""
        
        records.append({
            'Company': f"{name.title()}{option_type}",
            'Disclosed_Shares': shares,
            'SEC_Value_Thousands': value_thousands
        })
        
    return pd.DataFrame(records), filing_date

# --- MARKET DATA & DELTA ENGINE ---
@st.cache_data(ttl=3600)
def process_market_and_deltas(raw_df, historical_data):
    """Processes pricing data and calculates historical buys and sells."""
    if raw_df.empty:
        return pd.DataFrame()
        
    ticker_routing = {
        'Bloom Energy Corp': 'BE', 'Lumentum Hldgs Inc': 'LITE', 
        'Core Scientific Inc': 'CORZ', 'Iren Ltd': 'IREN', 
        'Applied Digital Corp': 'APLD', 'Sandisk Corp': 'SNDK', 
        'Eqt Corp': 'EQT', 'Tower Semiconductor Ltd': 'TSEM', 
        'Cipher Mining Inc': 'CIFR', 'Intel Corp': 'INTC'
    }
    
    prev_positions = historical_data.get('positions', {})
    
    tickers, prices, changes, deltas, actions = [], [], [], [], []
    
    for _, row in raw_df.iterrows():
        name = row['Company']
        clean_name = name.split('(')[0].strip()
        ticker = ticker_routing.get(clean_name, None)
        
        # Calculate Share Volume Changes (Buys/Sells)
        current_shares = row['Disclosed_Shares']
        past_shares = prev_positions.get(name, None)
        
        if past_shares is None:
            delta = current_shares
            action = "🟢 New Position / Buy"
        elif current_shares > past_shares:
            delta = current_shares - past_shares
            action = "🟢 Accumulating / Buy"
        elif current_shares < past_shares:
            delta = current_shares - past_shares
            action = "🔴 Reducing / Sell"
        else:
            delta = 0
            action = "⚪ Holding Position"
            
        close_price, pct_change = 0.0, 0.0
        if ticker and "CALL" not in name and "PUT" not in name:
            try:
                stock = yf.Ticker(ticker)
                todays_data = stock.history(period='1d')
                if not todays_data.empty:
                    close_price = todays_data['Close'].iloc[0]
                    open_price = todays_data['Open'].iloc[0]
                    pct_change = ((close_price - open_price) / open_price) * 100
            except:
                pass
        else:
            ticker = "DERIVATIVE/OPTION"
            
        tickers.append(ticker)
        prices.append(close_price)
        changes.append(pct_change)
        deltas.append(delta)
        actions.append(action)
        
    # Process potential liquidations (positions that vanished from the new filing)
    processed_df = raw_df.copy()
    processed_df['Ticker'] = tickers
    processed_df['Live Price ($)'] = prices
    processed_df['Daily Change (%)'] = changes
    processed_df['Position Delta (Shares)'] = deltas
    processed_df['Trade Action'] = actions
    
    processed_df['Est. Value ($M)'] = processed_df.apply(
        lambda r: round((r['Disclosed_Shares'] * r['Live Price ($)']) / 1_000_000, 2) if r['Live Price ($)'] > 0 
        else round((r['SEC_Value_Thousands'] * 1000) / 1_000_000, 2), axis=1
    )
    
    return processed_df

# --- EXECUTE APPS CORE PIPELINE ---
historical_snapshot = load_historical_snapshot()

with st.spinner("Connecting to SEC EDGAR Framework..."):
    raw_holdings, current_filing_date = scrape_sec_alpha_stream(FUND_CIK)

if not raw_holdings.empty:
    with st.spinner("Aligning Live Market Feeds..."):
        df = process_market_and_deltas(raw_holdings, historical_snapshot)
    
    # If the user is parsing a newer SEC filing than the database has, log it for next check
    if current_filing_date != historical_snapshot.get('filing_date', ''):
        save_current_snapshot(df, current_filing_date)
        
    # --- UI LAYOUT GENERATION ---
    total_val = df['Est. Value ($M)'].sum()
    active_equities = df[df['Live Price ($)'] > 0]
    
    col1, col2, col3 = st.columns(3)
    with col1:
        st.metric(label="Calculated Long Asset Exposure", value=f"${total_val:,.2f}M")
    with col2:
        if not active_equities.empty:
            top_mover = active_equities.loc[active_equities['Daily Change (%)'].idxmax()]
            st.metric(label="Top Public Momentum Driver", value=top_mover['Ticker'], delta=f"{top_mover['Daily Change (%)']:.2f}%")
        else:
            st.metric(label="Top Public Momentum Driver", value="N/A")
    with col3:
        buys_count = len(df[df['Trade Action'].str.contains('Buy')])
        st.metric(label="Filing Trade Adjustments Detected", value=f"{buys_count} Positions")
            
    st.markdown("---")
    
    left_chart_col, right_chart_col = st.columns(2)
    with left_chart_col:
        st.subheader("Asset Allocation Profile")
        fig_pie = px.pie(df, values='Est. Value ($M)', names='Company', hole=0.4, color_discrete_sequence=px.colors.sequential.Viridis)
        st.plotly_chart(fig_pie, use_container_width=True)
        
    with right_chart_col:
        st.subheader("Public Equity Volatility Matrix")
        if not active_equities.empty:
            fig_bar = px.bar(active_equities.sort_values(by='Daily Change (%)'), x='Ticker', y='Daily Change (%)',
                             color='Daily Change (%)', text_auto='.2f', color_continuous_scale=px.colors.diverging.RdYlGn, color_continuous_midpoint=0)
            st.plotly_chart(fig_bar, use_container_width=True)
        else:
            st.info("No active open public market equity tickers mapped for tracking charts.")
            
    # --- ACTIVE INSIGHT MODULES ---
    st.subheader("🔄 Strategic Trade Delta Log (Buys & Sells Tracking)")
    st.dataframe(
        df[['Ticker', 'Company', 'Position Delta (Shares)', 'Trade Action', 'Disclosed_Shares', 'Est. Value ($M)']].sort_values(by='Position Delta (Shares)', key=abs, ascending=False),
        hide_index=True,
        use_container_width=True
    )
    
    st.subheader("📋 Comprehensive Live Assets Matrix")
    st.dataframe(
        df[['Ticker', 'Company', 'Disclosed_Shares', 'Live Price ($)', 'Daily Change (%)', 'Est. Value ($M)']].sort_values(by='Est. Value ($M)', ascending=False),
        hide_index=True,
        use_container_width=True
    )
else:
    st.error("System failed to download data tables. Verify engine configurations.")

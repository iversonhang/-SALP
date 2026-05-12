import streamlit as st
import yfinance as yf
import pandas as pd
import plotly.express as px
import requests
from bs4 import BeautifulSoup
import xml.etree.ElementTree as ET
from datetime import datetime

# Configure page environment
st.set_page_config(page_title="Situational Awareness Live Portal", layout="wide")

st.title("🦅 Situational Awareness LP - Live SEC Tracking Portal")
st.caption(f"Portal Sync Engine: Active | Live Execution: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} HKT")
st.markdown("---")

# Fund ID assigned by the Securities and Exchange Commission (SEC)
FUND_CIK = "0002045724"

@st.cache_data(ttl=86400) # Cache SEC structural scrape for 24 hours to stay efficient
def scrape_latest_13f_holdings(cik):
    """Fetches and decodes the most recent Form 13F filing directly from SEC EDGAR."""
    headers = {
        'User-Agent': 'Research Portal admin@researchportal.com'
    }
    
    # Step A: Request the global submissions profile for the CIK
    sec_url = f"https://data.sec.gov/submissions/CIK{cik.zfill(10)}.json"
    response = requests.get(sec_url, headers=headers)
    
    if response.status_code != 200:
        st.error(f"Failed to connect to SEC database (Status: {response.status_code}).")
        return pd.DataFrame()
        
    data = response.json()
    recent_filings = data['filings']['recent']
    
    # Step B: Find the index of the absolute newest 13F-HR (Holdings Report) 
    idx = -1
    for i, form_type in enumerate(recent_filings['form']):
        if form_type == '13F-HR':
            idx = i
            break
            
    if idx == -1:
        st.error("No valid 13F Holdings Report discovered for this entity identifier.")
        return pd.DataFrame()
        
    acc_num = recent_filings['accessionNumber'][idx].replace('-', '')
    filing_date = recent_filings['filingDate'][idx]
    st.info(f"📁 Target Found: Parsing SEC Filing Date {filing_date} (Accession No: {acc_num})")
    
    # Step C: Dynamic Directory Mapping
    # Instead of filtering purely by string matching, we read the index page table cells
    doc_index_url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc_num}/{recent_filings['accessionNumber'][idx]}.txt"
    
    # Fallback to scanning the main directory page securely
    dir_url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc_num}/"
    dir_response = requests.get(dir_url, headers=headers)
    soup = BeautifulSoup(dir_response.text, 'html.parser')
    
    xml_path = None
    # Look for files ending in .xml that are NOT the primary cover page document
    for link in soup.find_all('a'):
        href = link.get('href', '')
        if href.endswith('.xml') and 'primary_doc' not in href.lower():
            xml_path = href
            break
            
    # Emergency fallback: if they named it uniquely without distinct directory anchors
    if not xml_path:
        for link in soup.find_all('a'):
            href = link.get('href', '')
            if href.endswith('.xml'):
                xml_path = href  # Grab available XML sheet
    
    if not xml_path:
        st.error("Target XML information sheet data block layout missing from index directory.")
        return pd.DataFrame()
        
    xml_url = f"https://www.sec.gov{xml_path}"
    xml_response = requests.get(xml_url, headers=headers)
    
    # Step D: Robust XML Tree Navigation
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
        
    return pd.DataFrame(records)

@st.cache_data(ttl=3600) # Cache market pricing changes for 1 hour
def process_market_telemetry(raw_df):
    """Cross-references asset records against real-time equity market charts."""
    if raw_df.empty:
        return pd.DataFrame()
        
    # Standard translation mapping for core assets identified via regulatory reports
    ticker_routing = {
        'Bloom Energy Corp': 'BE',
        'Lumentum Hldgs Inc': 'LITE',
        'Core Scientific Inc': 'CORZ',
        'Iren Ltd': 'IREN',
        'Applied Digital Corp': 'APLD',
        'Sandisk Corp': 'SNDK',
        'Eqt Corp': 'EQT',
        'Tower Semiconductor Ltd': 'TSEM',
        'Cipher Mining Inc': 'CIFR',
        'Intel Corp': 'INTC'
    }
    
    tickers = []
    prices = []
    changes = []
    
    for _, row in raw_df.iterrows():
        clean_name = row['Company'].split('(')[0].strip()
        ticker = ticker_routing.get(clean_name, None)
        
        close_price, pct_change = 0.0, 0.0
        
        # If the asset has a known ticker and is not an exotic option proxy, fetch data
        if ticker and "CALL" not in row['Company'] and "PUT" not in row['Company']:
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
            ticker = "DERIVATIVE/PRIVATE"
            
        tickers.append(ticker)
        prices.append(close_price)
        changes.append(pct_change)
        
    processed_df = raw_df.copy()
    processed_df['Ticker'] = tickers
    processed_df['Live Price ($)'] = prices
    processed_df['Daily Change (%)'] = changes
    
    # Use real price logic where available; fall back to filing disclosures for non-standard assets
    processed_df['Est. Value ($M)'] = processed_df.apply(
        lambda r: round((r['Disclosed_Shares'] * r['Live Price ($)']) / 1_000_000, 2) if r['Live Price ($)'] > 0 
        else round(r['SEC_Value_Thousands'] / 1000, 2), axis=1
    )
    
    return processed_df

# --- RUN DATA PIPELINE ---
with st.spinner("Connecting to SEC EDGAR Servers..."):
    raw_holdings = scrape_latest_13f_holdings(FUND_CIK)

if not raw_holdings.empty:
    with st.spinner("Connecting to Live Market Feeds..."):
        df = process_market_telemetry(raw_holdings)
        
    # --- UI RENDER SYSTEM ---
    total_val = df['Est. Value ($M)'].sum()
    active_equities = df[df['Live Price ($)'] > 0]
    
    col1, col2, col3 = st.columns(3)
    with col1:
        st.metric(label="Calculated Portfolio AUM Exposure", value=f"${total_val:,.2f}M")
    with col2:
        if not active_equities.empty:
            top_mover = active_equities.loc[active_equities['Daily Change (%)'].idxmax()]
            st.metric(label="Top Public Gainer Today", value=top_mover['Ticker'], delta=f"{top_mover['Daily Change (%)']:.2f}%")
        else:
            st.metric(label="Top Public Gainer Today", value="N/A")
    with col3:
        if not active_equities.empty:
            worst_mover = active_equities.loc[active_equities['Daily Change (%)'].idxmin()]
            st.metric(label="Max Public Drag Today", value=worst_mover['Ticker'], delta=f"{worst_mover['Daily Change (%)']:.2f}%", delta_color="inverse")
        else:
            st.metric(label="Max Public Drag Today", value="N/A")
            
    st.markdown("---")
    
    left_chart_col, right_chart_col = st.columns(2)
    with left_chart_col:
        st.subheader("Asset Allocation Mapping")
        fig_pie = px.pie(df, values='Est. Value ($M)', names='Company', hole=0.4, color_discrete_sequence=px.colors.sequential.Plotlysh)
        st.plotly_chart(fig_pie, use_container_width=True)
        
    with right_chart_col:
        st.subheader("Public Equity Volatility Matrix")
        if not active_equities.empty:
            fig_bar = px.bar(active_equities.sort_values(by='Daily Change (%)'), x='Ticker', y='Daily Change (%)',
                             color='Daily Change (%)', text_auto='.2f', color_continuous_scale=px.colors.diverging.RdYlGn, color_continuous_midpoint=0)
            st.plotly_chart(fig_bar, use_container_width=True)
        else:
            st.info("No active open public market equity tickers mapped for historical performance tracking charts.")
            
    st.subheader("Live Portfolio Position Tapes (SEC Dynamic Data Mapping)")
    st.dataframe(
        df[['Ticker', 'Company', 'Disclosed_Shares', 'Live Price ($)', 'Daily Change (%)', 'Est. Value ($M)']].sort_values(by='Est. Value ($M)', ascending=False),
        hide_index=True,
        use_container_width=True
    )
else:
    st.error("System failed to build database tables. Verify SEC connection metrics and try again.")

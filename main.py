print("Starting Grok Sauce Streaming Machine...")

import requests
import websocket as websocket
import json
import threading
import time
import certifi
import os
import yfinance as yf
import pandas_datareader.data as web
import pandas as pd
from flask import Flask, render_template, request, redirect, url_for
from flask_socketio import SocketIO, emit
from collections import defaultdict
from dotenv import load_dotenv
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
from datetime import datetime, timezone, timedelta

load_dotenv()

app = Flask(__name__)
socketio = SocketIO(app)

market_data = defaultdict(lambda: {"price": 0, "volume": 0, "open": 0, "last_update": 0, "avg_volume": 0})
news_feed = []
forex_events = []
upcoming_events = []
master_sentiment = 0
pain_points = {}

streamer_thread = None
streamer_symbols = []
session_token = None

SETTINGS_FILE = "settings.json"
DAILY_FILE = "daily.json"
DEFAULT_INDICATORS = {
    "SPY": {"sentiment": "positive"},
    "XLF": {"sentiment": "positive"},
    "XLU": {"sentiment": "neutral"},
    "XLP": {"sentiment": "positive"},
    "IYR": {"sentiment": "neutral"},
    "TIP": {"sentiment": "neutral"},
    "DXY": {"sentiment": "negative"},
    "VIX": {"sentiment": "negative"},
    "UUP": {"sentiment": "negative"},
    "QQQ": {"sentiment": "positive"},
    "BITO": {"sentiment": "positive"},
    "GLD": {"sentiment": "neutral"},
    "TLT": {"sentiment": "neutral"}
}
DEFAULT_SETTINGS = {
    "indicators": DEFAULT_INDICATORS,
    "enable_forex_factory": False
}

if os.path.exists(SETTINGS_FILE):
    try:
        with open(SETTINGS_FILE, 'r') as f:
            settings = json.load(f)
        INDICATORS = settings.get("indicators", DEFAULT_INDICATORS)
        ENABLE_FOREX_FACTORY = settings.get("enable_forex_factory", False)
        print(f"Loaded settings from {SETTINGS_FILE}: {settings}")
    except (json.JSONDecodeError, IOError) as e:
        print(f"Failed to load {SETTINGS_FILE}: {e}, using defaults")
        INDICATORS = DEFAULT_INDICATORS
        ENABLE_FOREX_FACTORY = False
else:
    INDICATORS = DEFAULT_INDICATORS
    ENABLE_FOREX_FACTORY = False
    with open(SETTINGS_FILE, 'w') as f:
        json.dump(DEFAULT_SETTINGS, f)
    print(f"Initialized settings to default in {SETTINGS_FILE}")

CHAOS_EVENTS = {
    "LBUCONF": ("Consumer Confidence", "high"),
    "FEDFUNDS": ("FOMC Rate Decision", "high"),
    "PAYEMS": ("Non-Farm Payrolls", "high"),
    "CPIAUCSL": ("CPI", "high"),
    "UNRATE": ("Unemployment Rate", "medium"),
    "RSXFS": ("Retail Sales", "medium"),
    "HOUST": ("Housing Starts", "low"),
    "INDPRO": ("Industrial Production", "low")
}

def login_and_get_quote_token(username, password, input_session_token=None, remember_token=None):
    global session_token
    login_url = "https://api.tastytrade.com/sessions"
    payload = {"login": username, "password": password}
    headers = {"Content-Type": "application/json", "User-Agent": "grok-client/1.0"}
    
    try:
        response = requests.post(login_url, json=payload, headers=headers, verify=certifi.where())
        response.raise_for_status()
        session_data = response.json()["data"]
        session_token = session_data["session-token"]
        print("Logged in successfully. Session token acquired.")
        
        quote_token_url = "https://api.tastytrade.com/api-quote-tokens"
        headers["Authorization"] = session_token
        quote_response = requests.get(quote_token_url, headers=headers, verify=certifi.where())
        quote_response.raise_for_status()
        api_quote_token = quote_response.json()["data"]["token"]
        print("API quote token acquired.")
        return api_quote_token
    except requests.exceptions.RequestException as e:
        print(f"Failed to log in or get quote token: {e}")
        return None

def fetch_historical_volume(symbol, session_token, days=5):
    end_date = datetime.now(timezone.utc).date()
    start_date = end_date - timedelta(days=days)
    url = f"https://api.tastytrade.com/market-metrics/historic?symbols={symbol}&start-date={start_date.strftime('%Y-%m-%d')}&end-date={end_date.strftime('%Y-%m-%d')}"
    headers = {"Authorization": session_token, "Content-Type": "application/json"}
    try:
        response = requests.get(url, headers=headers, verify=certifi.where())
        response.raise_for_status()
        data = response.json()
        print(f"Raw response for {symbol} (tastytrade): {json.dumps(data)}")
        if "data" in data and "items" in data["data"] and data["data"]["items"]:
            total_volume = sum(float(day["volume"]) for day in data["data"]["items"])
            avg_volume = total_volume / len(data["data"]["items"])
            print(f"Fetched avg volume for {symbol} (tastytrade): {avg_volume}")
            return avg_volume
        print(f"No historical data for {symbol} from tastytrade")
    except (requests.exceptions.RequestException, KeyError, ValueError) as e:
        print(f"Failed to fetch historical volume for {symbol} (tastytrade): {e}")

    try:
        ticker = yf.Ticker(symbol)
        hist = ticker.history(start=start_date.strftime('%Y-%m-%d'), end=end_date.strftime('%Y-%m-%d'))
        if not hist.empty and "Volume" in hist:
            avg_volume = hist["Volume"].mean()
            print(f"Fetched avg volume for {symbol} (yfinance): {avg_volume}")
            return avg_volume if avg_volume > 0 else 1000000
        print(f"No historical data for {symbol} from yfinance")
    except Exception as e:
        print(f"Failed to fetch historical volume for {symbol} (yfinance): {e}")
    
    if os.path.exists(DAILY_FILE):
        with open(DAILY_FILE, 'r') as f:
            daily_data = json.load(f)
        if symbol in daily_data and daily_data[symbol]["volume"] > 0:
            avg_volume = daily_data[symbol]["volume"] / 5
            print(f"Using daily.json avg volume for {symbol}: {avg_volume}")
            return avg_volume
    return 1000000

def fetch_volume_profile(symbol, days=7):
    end_date = datetime.now(timezone.utc).date()
    start_date = end_date - timedelta(days=days)
    try:
        ticker = yf.Ticker(symbol)
        hist = ticker.history(start=start_date.strftime('%Y-%m-%d'), end=end_date.strftime('%Y-%m-%d'), interval="1h")
        if not hist.empty:
            hist["PriceBin"] = hist["Close"].round(0)
            profile = hist.groupby("PriceBin")["Volume"].sum().to_dict()
            print(f"Volume Profile for {symbol}: {profile}")
            return profile
        print(f"No volume profile data for {symbol} from yfinance")
        return {}
    except Exception as e:
        print(f"Failed to fetch volume profile for {symbol}: {e}")
        return {}

def fetch_pain_point(symbol, session_token):
    expiration = (datetime.now(timezone.utc) + timedelta(days=3)).strftime('%Y-%m-%d')
    url = f"https://api.tastytrade.com/instruments/equity-options?symbol={symbol}&expiration-date={expiration}"
    headers = {"Authorization": session_token, "Content-Type": "application/json"}
    try:
        response = requests.get(url, headers=headers, verify=certifi.where())
        response.raise_for_status()
        options = response.json()["data"]["items"]
        strike_oi = defaultdict(float)
        for option in options:
            strike = float(option["strike-price"])
            oi = float(option.get("open-interest", 0))
            strike_oi[strike] += oi
        
        today = datetime.now(timezone.utc)
        dte = (datetime.strptime(expiration, '%Y-%m-%d') - today).days
        witching = today.day > 14 and today.weekday() == 4 and today.month in [3, 6, 9, 12]
        
        if strike_oi:
            max_pain = max(strike_oi.items(), key=lambda x: x[1])[0]
            pain_points[symbol] = {"max_pain": max_pain, "dte": dte, "witching": witching}
            print(f"Pain Point for {symbol}: Max Pain={max_pain}, DTE={dte}, Witching={witching}")
            return max_pain
        return None
    except Exception as e:
        print(f"Failed to fetch pain point for {symbol}: {e}")
        return None

def on_message(ws, message):
    data = json.loads(message)
    if data["type"] == "FEED_DATA":
        event_type, event_data = data["data"]
        if isinstance(event_data, list) and len(event_data) > 1:
            symbol = event_data[1]
            channel_id = ws.channel_id
            if event_type == "Trade" and len(event_data) >= 4:
                market_data[symbol]["price"] = float(event_data[2])
                market_data[symbol]["volume"] += float(event_data[3])
                print(f"Stream {channel_id} - Trade for {symbol}: Price={event_data[2]}, Size={event_data[3]}")
            elif event_type == "Quote" and len(event_data) >= 4:
                bid, ask = float(event_data[2]), float(event_data[3])
                market_data[symbol]["price"] = (bid + ask) / 2
                if market_data[symbol]["open"] == 0:
                    market_data[symbol]["open"] = market_data[symbol]["price"]
                print(f"Stream {channel_id} - Quote for {symbol}: Bid={bid}, Ask={ask}, Open={market_data[symbol]['open']}")
            elif event_type == "Summary" and len(event_data) >= 3:
                open_price = event_data[2]
                if open_price != "NaN" and open_price:
                    market_data[symbol]["open"] = float(open_price)
                print(f"Stream {channel_id} - Summary for {symbol}: Open={open_price}")
            market_data[symbol]["last_update"] = time.time()
            
            price = market_data[symbol]["price"]
            open_price = market_data[symbol]["open"]
            volume = market_data[symbol]["volume"]
            avg_volume = market_data[symbol]["avg_volume"] or 1
            rvol = volume / avg_volume if avg_volume > 0 else 0
            change_price = price - open_price if open_price > 0 else 0
            change_percent = (change_price / open_price * 100) if open_price > 0 else 0
            color = "darkgreen" if change_percent > 0 else "darkred" if change_percent < 0 else "gray"
            text_color = "orange" if INDICATORS[symbol]["sentiment"] == "positive" else "lightblue" if INDICATORS[symbol]["sentiment"] == "neutral" else "pink"
            
            pain = pain_points.get(symbol, {}).get("max_pain", "N/A")
            dte = pain_points.get(symbol, {}).get("dte", "N/A")
            witching = pain_points.get(symbol, {}).get("witching", False)
            
            socketio.emit('update_indicator', {
                'symbol': symbol,
                'price': round(price, 2),
                'rvol': round(rvol, 2) if not pd.isna(rvol) else 0,
                'change_price': round(change_price, 2),
                'change_percent': round(change_percent, 2),
                'color': color,
                'text_color': text_color,
                'max_pain': pain,
                'dte': dte,
                'witching': witching
            })

def on_error(ws, error):
    print(f"Stream {ws.channel_id} - Error: {error}")

def on_close(ws, close_status_code, close_msg):
    print(f"Stream {ws.channel_id} - Connection closed.")

def create_stream(api_quote_token, symbols, channel_id):
    global streamer_thread
    dxlink_url = "wss://tasty-openapi-ws.dxfeed.com/realtime"

    def on_open(ws):
        print(f"Stream {channel_id} connected for {symbols}")
        ws.send(json.dumps({"type": "SETUP", "channel": 0, "version": "0.1-DXF-JS/0.3.0", "keepaliveTimeout": 60, "acceptKeepaliveTimeout": 60}))
        ws.send(json.dumps({"type": "AUTH", "channel": 0, "token": api_quote_token}))
        ws.send(json.dumps({"type": "CHANNEL_REQUEST", "channel": channel_id, "service": "FEED", "parameters": {"contract": "AUTO"}}))
        ws.send(json.dumps({
            "type": "FEED_SETUP",
            "channel": channel_id,
            "acceptAggregationPeriod": 0.1,
            "acceptDataFormat": "COMPACT",
            "acceptEventFields": {
                "Trade": ["eventType", "eventSymbol", "price", "size"],
                "Quote": ["eventType", "eventSymbol", "bidPrice", "askPrice"],
                "Summary": ["eventType", "eventSymbol", "dayOpenPrice"]
            }
        }))
        subscription = [{"type": "Trade", "symbol": s} for s in symbols] + \
                      [{"type": "Quote", "symbol": s} for s in symbols] + \
                      [{"type": "Summary", "symbol": s} for s in symbols]
        ws.send(json.dumps({"type": "FEED_SUBSCRIPTION", "channel": channel_id, "reset": True, "add": subscription}))
        
        def keepalive():
            while ws.keep_running:
                ws.send(json.dumps({"type": "KEEPALIVE", "channel": 0}))
                time.sleep(30)
        threading.Thread(target=keepalive, daemon=True).start()

    ws = websocket.WebSocketApp(
        dxlink_url,
        on_open=on_open,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close
    )
    ws.channel_id = channel_id
    
    ws_thread = threading.Thread(target=ws.run_forever, kwargs={'sslopt': {"ca_certs": certifi.where()}}, daemon=True)
    ws_thread.start()
    return ws, ws_thread

def stop_stream(streamer):
    if streamer:
        streamer[0].close()
        streamer[1].join(timeout=2)

def fetch_news(api_key, query="market OR fed OR inflation OR gdp OR economic"):
    global master_sentiment
    url = "https://newsapi.org/v2/everything"
    params = {
        "q": query,
        "apiKey": api_key,
        "language": "en",
        "sortBy": "publishedAt",
        "pageSize": 50
    }
    try:
        response = requests.get(url, params=params)
        response.raise_for_status()
        articles = response.json()["articles"]
        analyzer = SentimentIntensityAnalyzer()
        sentiments = []
        news_feed.clear()
        for article in articles:
            title = article["title"]
            sentiment = analyzer.polarity_scores(title)["compound"]
            sentiments.append(sentiment)
            news_feed.append({
                "title": title,
                "sentiment": sentiment,
                "time": article["publishedAt"],
                "url": article["url"]
            })
        master_sentiment = sum(sentiments) / len(sentiments) if sentiments else 0
        print(f"Master Sentiment Index: {master_sentiment}")
    except requests.exceptions.RequestException as e:
        print(f"Failed to fetch news: {e}")
    socketio.emit('update_sentiment', {'master_sentiment': round(master_sentiment, 2)})

def fetch_forex_factory():
    global upcoming_events
    if not ENABLE_FOREX_FACTORY:
        print("Forex Factory disabled in settings")
        return
    url = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
    try:
        response = requests.get(url)
        response.raise_for_status()
        events = response.json()
        upcoming_events.clear()
        for event in events:
            event_time = datetime.strptime(event["date"], "%Y-%m-%dT%H:%M:%S%z")
            if event.get("currency") == "USD":
                impact = event["impact"].lower()
                upcoming_events.append({
                    "title": event["title"],
                    "time": event["date"],
                    "impact": impact,
                    "forecast": event.get("forecast", "N/A"),
                    "previous": event.get("previous", "N/A")
                })
                print(f"Upcoming USD Event: {event['title']} | Time: {event['date']} | Impact: {impact}")
        print(f"Loaded {len(upcoming_events)} upcoming USD events from Forex Factory")
    except requests.exceptions.RequestException as e:
        print(f"Failed to fetch Forex Factory JSON: {e}")

def fetch_fred_events():
    global forex_events
    forex_events.clear()
    start = datetime.now(timezone.utc) - timedelta(days=1)
    end = datetime.now(timezone.utc)
    today = datetime.now(timezone.utc).date()
    try:
        for series, (title, impact) in CHAOS_EVENTS.items():
            data = web.DataReader(series, "fred", start, end)
            print(f"FRED Raw Data for {title}: {data.tail(2)}")
            if not data.empty:
                latest = data.iloc[-1]
                event_time = latest.name.replace(tzinfo=timezone.utc)
                if event_time.date() == today:
                    actual = float(latest[series]) if pd.notna(latest[series]) else "N/A"
                    previous = float(data.iloc[-2][series]) if len(data) > 1 else "N/A"
                    color = "lightred" if impact == "high" else "lightorange" if impact == "medium" else "paleyellow"
                    event = {
                        "title": title,
                        "time": event_time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                        "impact": impact,
                        "actual": actual,
                        "forecast": "N/A",
                        "previous": previous,
                        "color": color
                    }
                    for upcoming in upcoming_events:
                        if upcoming["title"] == title and abs((datetime.strptime(upcoming["time"], "%Y-%m-%dT%H:%M:%S%z") - event_time).total_seconds()) < 3600:
                            event["forecast"] = upcoming.get("forecast", "N/A")
                            print(f"Cross-Correlated: {title} | FRED Actual: {actual} | FF Forecast: {event['forecast']}")
                    forex_events.append(event)
                    socketio.emit('update_events', event)
                    print(f"FRED Event: {title} | Time: {event_time} | Actual: {actual} | Impact: {impact}")
        forex_events.sort(key=lambda x: x["time"])
        latest_event = next(
            (e for e in reversed(forex_events) if datetime.strptime(e["time"], "%Y-%m-%dT%H:%M:%S%z") <= datetime.now(timezone.utc)),
            forex_events[-1] if forex_events else None
        )
        if latest_event:
            print(f"Latest FRED Event: {latest_event['title']} | Actual: {latest_event['actual']}")
        return latest_event
    except Exception as e:
        print(f"FRED fetch failed: {e}")
        return None

@app.route('/')
def index():
    return redirect(url_for('indicator_page'))

@app.route('/indicator', methods=['GET'])
def indicator_page():
    indicators = []
    for symbol, config in INDICATORS.items():
        data = market_data[symbol]
        price = data["price"]
        open_price = data["open"]
        volume = data["volume"]
        avg_volume = data["avg_volume"]
        
        rvol = volume / avg_volume if avg_volume > 0 else 0
        change_price = price - open_price if open_price > 0 else 0
        change_percent = (change_price / open_price * 100) if open_price > 0 else 0
        color = "darkgreen" if change_percent > 0 else "darkred" if change_percent < 0 else "gray"
        text_color = "orange" if config["sentiment"] == "positive" else "lightblue" if config["sentiment"] == "neutral" else "pink"
        
        pain = pain_points.get(symbol, {}).get("max_pain", "N/A")
        dte = pain_points.get(symbol, {}).get("dte", "N/A")
        witching = pain_points.get(symbol, {}).get("witching", False)
        
        indicators.append({
            "symbol": symbol,
            "text_color": text_color,
            "color": color,
            "price": round(price, 2),
            "rvol": round(rvol, 2) if not pd.isna(rvol) else 0,
            "change_price": round(change_price, 2),
            "change_percent": round(change_percent, 2),
            "max_pain": pain,
            "dte": dte,
            "witching": witching
        })
    latest_event = fetch_fred_events()
    headers = ["Symbol", "Price", "RVOL", "Change ($)", "Change (%)", "Max Pain", "DTE", "Witching"]
    return render_template("indicator.html", indicators=indicators, forex_events=forex_events[:5], forex_event=latest_event, master_sentiment=round(master_sentiment, 2), headers=headers)

@app.route('/news', methods=['GET', 'POST'])
def news_page():
    if request.method == 'POST':
        company = request.form.get('company', '')
        fetch_news(os.getenv("NEWSAPI_KEY"), query=company)
        return render_template("news.html", news=news_feed)
    return render_template("news.html", news=news_feed)

@app.route('/setup', methods=['GET', 'POST'])
def setup_page():
    global streamer_thread, streamer_symbols, session_token, ENABLE_FOREX_FACTORY, INDICATORS
    if request.method == 'POST':
        print("Setup POST received")
        new_indicators = {}
        for sector, color in [("positive", "orange"), ("neutral", "lightblue"), ("negative", "pink")]:
            symbols = request.form.get(f"{sector}_symbols", "").split(",")
            for symbol in [s.strip() for s in symbols if s.strip()]:
                new_indicators[symbol] = {"sentiment": sector}
                print(f"Added {symbol} to {sector}")
        
        ENABLE_FOREX_FACTORY = bool(request.form.get("enable_forex_factory", False))
        INDICATORS = new_indicators if new_indicators else INDICATORS
        
        new_symbols = list(INDICATORS.keys())
        if new_symbols != streamer_symbols:
            print(f"Restarting stream with {new_symbols}")
            stop_stream(streamer_thread)
            streamer_symbols = new_symbols
            quote_token = login_and_get_quote_token(
                os.getenv("TASTY_USERNAME"),
                os.getenv("TASTY_PASSWORD"),
                session_token,
                os.getenv("TASTY_REMEMBER_TOKEN")
            )
            if quote_token:
                for symbol in new_symbols:
                    market_data[symbol]["avg_volume"] = fetch_historical_volume(symbol, session_token)
                streamer_thread = create_stream(quote_token, streamer_symbols, 1)
        
        settings = {"indicators": INDICATORS, "enable_forex_factory": ENABLE_FOREX_FACTORY}
        with open(SETTINGS_FILE, 'w') as f:
            json.dump(settings, f)
        print(f"Saved settings to {SETTINGS_FILE}: {settings}")
        
        return redirect(url_for('indicator_page'))
    
    print("Rendering setup page")
    positive_symbols = ','.join([s for s, c in INDICATORS.items() if c['sentiment'] == 'positive'])
    neutral_symbols = ','.join([s for s, c in INDICATORS.items() if c['sentiment'] == 'neutral'])
    negative_symbols = ','.join([s for s, c in INDICATORS.items() if c['sentiment'] == 'negative'])
    return render_template("setup.html", positive_symbols=positive_symbols, neutral_symbols=neutral_symbols, negative_symbols=negative_symbols, enable_forex_factory=ENABLE_FOREX_FACTORY)

def news_thread():
    api_key = os.getenv("NEWSAPI_KEY")
    while True:
        fetch_news(api_key)
        time.sleep(300)

def fred_thread():
    while True:
        fetch_fred_events()
        time.sleep(600)

def forex_daily_thread():
    while True:
        now = datetime.now(timezone.utc)
        if ENABLE_FOREX_FACTORY and now.hour == 11 and now.minute == 0:
            fetch_forex_factory()
        time.sleep(60)

def volume_profile_thread():
    while True:
        now = datetime.now(timezone.utc)
        if now.weekday() == 0 and now.hour == 0 and now.minute == 0:
            for symbol in INDICATORS.keys():
                fetch_volume_profile(symbol)
        time.sleep(60)

def pain_point_thread():
    while True:
        now = datetime.now(timezone.utc)
        for symbol in INDICATORS.keys():
            fetch_pain_point(symbol, session_token)
        time.sleep(3600)

def save_daily_data():
    daily_data = {symbol: {"price": data["price"], "volume": data["volume"]} for symbol, data in market_data.items()}
    with open(DAILY_FILE, 'w') as f:
        json.dump(daily_data, f)
    print(f"Saved daily data to {DAILY_FILE}")

if __name__ == "__main__":
    tasty_username = os.getenv("TASTY_USERNAME")
    tasty_password = os.getenv("TASTY_PASSWORD")
    tasty_session_token = os.getenv("TASTY_SESSION_TOKEN")
    tasty_remember_token = os.getenv("TASTY_REMEMBER_TOKEN")
    
    if not tasty_username or not tasty_password:
        print("Error: TASTY_USERNAME or TASTY_PASSWORD not found in .env file.")
    else:
        quote_token = login_and_get_quote_token(
            tasty_username,
            tasty_password,
            tasty_session_token,
            tasty_remember_token
        )
        
        if quote_token:
            for symbol in INDICATORS.keys():
                market_data[symbol]["avg_volume"] = fetch_historical_volume(symbol, session_token)
            streamer_symbols = list(INDICATORS.keys())
            streamer_thread = create_stream(quote_token, streamer_symbols, 1)
            
            flask_thread = threading.Thread(target=socketio.run, args=(app,), kwargs={"host": "0.0.0.0", "port": 5010}, daemon=True)
            flask_thread.start()
            news_thread_instance = threading.Thread(target=news_thread, daemon=True)
            news_thread_instance.start()
            fred_thread_instance = threading.Thread(target=fred_thread, daemon=True)
            fred_thread_instance.start()
            forex_thread_instance = threading.Thread(target=forex_daily_thread, daemon=True)
            forex_thread_instance.start()
            volume_thread_instance = threading.Thread(target=volume_profile_thread, daemon=True)
            volume_thread_instance.start()
            pain_thread_instance = threading.Thread(target=pain_point_thread, daemon=True)
            pain_thread_instance.start()
            
            try:
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                print("Shutting down all streams and server...")
                save_daily_data()
        else:
            print("Couldn’t start streaming without a quote token.")
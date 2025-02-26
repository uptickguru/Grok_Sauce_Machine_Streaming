from flask import Flask, render_template, jsonify, request
import sys
import os
import smtplib
from email.mime.text import MIMEText
import threading
import time
from datetime import datetime, timezone, timedelta
sys.path.append(os.path.abspath("../code"))
from trading_logic import authenticate, get_market_data, fetch_history_once, check_news, build_butterfly, evaluate_trade, strategy_settings
from logging_config import setup_logging

app = Flask(__name__)
logger = setup_logging()

active_positions = []
breakout_status = {"/MES": "Pending", "/MNQ": "Pending", "^GSPC": "Pending", "^NDX": "Pending", "/CL": "Pending", "/GC": "Pending"}
forex_strategy_settings = {
    "FastEMA_Period": 20, "SlowEMA_Period": 50, "RSI_Period": 14, "LotSize": 0.01,
    "StopLossPips": 20, "TakeProfitPips": 40, "MaxDailyLoss": 3.0, "StartHourCST": 2, "EndHourCST": 9
}
forex_trades = []

def send_notification(subject, body):
    sender = os.getenv("EMAIL_SENDER")
    password = os.getenv("EMAIL_PASSWORD")
    receiver = os.getenv("EMAIL_RECEIVER")
    
    if all([sender, password, receiver]):
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = sender
        msg["To"] = receiver
        logger.info(f"Attempting email notification to {receiver}: {subject}")
        try:
            with smtplib.SMTP("smtp.gmail.com", 587) as server:
                server.starttls()
                server.login(sender, password)
                server.send_message(msg)
            logger.info(f"Email notification sent to {receiver}")
            print(f"Email notification sent to {receiver}")
        except Exception as e:
            logger.error(f"Failed to send email: {str(e)}")
            print(f"Failed to send email: {e}")
    else:
        logger.warning("Email config missing in .env—skipping notification")
        print("Email config missing in .env—skipping notification")

def monitor_breakouts():
    global breakout_status
    token = authenticate()
    last_price = {"/MES": None, "/MNQ": None, "^GSPC": None, "^NDX": None, "/CL": None, "/GC": None}
    price_history = {"/MES": [], "/MNQ": [], "^GSPC": [], "^NDX": [], "/CL": [], "/GC": []}
    last_alert_sent = None
    
    while True:
        market_data, _ = get_market_data(token, symbols=["TLT", "GLD", "SPY", "^VIX", "/MES", "/MNQ", "^GSPC", "^NDX", "/CL", "/GC"])
        logger.info(f"Market data fetched: {market_data.keys()}")
        _, breakouts = evaluate_trade(False)
        logger.info(f"Breakouts computed: {breakouts.keys()}")
        
        if not market_data:
            logger.warning("No market data available—skipping iteration")
            time.sleep(30)
            continue
        
        now = datetime.now(timezone.utc) - timedelta(hours=6)  # CST
        cst_hour, cst_min = now.hour, now.minute
        
        if cst_hour == 9 and cst_min >= 55 and cst_min <= 56 and last_alert_sent != now.date():
            highest_prob = None
            highest_prob_symbol = None
            for symbol, breakout in breakouts.items():
                if breakout is None:  # Skip None values
                    continue
                if highest_prob is None or breakout["probability"] > highest_prob:
                    highest_prob = breakout["probability"]
                    highest_prob_symbol = symbol
            
            if highest_prob_symbol:
                breakout = breakouts[highest_prob_symbol]
                price = market_data[highest_prob_symbol]["price"]
                price_history[highest_prob_symbol].append(price)
                if len(price_history[highest_prob_symbol]) > 20:
                    price_history[highest_prob_symbol].pop(0)
                roc = (price - price_history[highest_prob_symbol][0]) / 10 if len(price_history[highest_prob_symbol]) >= 20 else 0
                
                trade_type = "Short" if price < breakout["breakout_down"] else "Long"
                trade = breakout["short"] if trade_type == "Short" else breakout["long"]
                body = (f"10am CST Trade Alert:\n"
                        f"Anticipated Trade: {highest_prob_symbol} {trade_type}\n"
                        f"Price Level: {price:.2f}\n"
                        f"Entry: {trade['entry']:.2f}, Target: {trade['target']:.2f}, Stop: {trade['stop']:.2f}\n"
                        f"10-min Rate of Change: {roc:.2f} points/min\n"
                        f"Probability: {highest_prob:.2f}")
                send_notification(f"{highest_prob_symbol} 10am CST Alert", body)
                last_alert_sent = now.date()
        
        for symbol in ["/MES", "/MNQ", "^GSPC", "^NDX", "/CL", "/GC"]:
            if symbol in breakouts and breakouts[symbol] and symbol in market_data:
                breakout = breakouts[symbol]
                price = market_data[symbol]["price"]
                last_price[symbol] = price
                
                if price > breakout["breakout_up"] and breakout_status[symbol] == "Pending":
                    breakout_status[symbol] = "Long Triggered"
                    trade = breakout["long"]
                    body = f"{symbol} Breakout Long:\nEntry: {trade['entry']}\nTarget: {trade['target']}\nStop: {trade['stop']}\nBest Hour: {breakout['best_hour_cst']} CST\nProbability: {breakout['probability']:.2f}"
                    send_notification(f"{symbol} Breakout Alert", body)
                elif price < breakout["breakout_down"] and breakout_status[symbol] == "Pending":
                    breakout_status[symbol] = "Short Triggered"
                    trade = breakout["short"]
                    body = f"{symbol} Breakout Short:\nEntry: {trade['entry']}\nTarget: {trade['target']}\nStop: {trade['stop']}\nBest Hour: {breakout['best_hour_cst']} CST\nProbability: {breakout['probability']:.2f}"
                    send_notification(f"{symbol} Breakout Alert", body)
            else:
                logger.warning(f"No breakout or price data for {symbol}")
        
        time.sleep(30)

def fetch_dashboard_data():
    token = authenticate()
    market_data, vix = get_market_data(token, symbols=["TLT", "GLD", "SPY", "^VIX", "/MES", "/MNQ", "^GSPC", "^NDX", "/CL", "/GC"])
    outcomes, breakouts = evaluate_trade(False)
    logger.info(f"Dashboard breakouts: {breakouts.keys()}")
    
    symbols = ["TLT", "GLD", "SPY"]
    futures = ["/MES", "/MNQ", "^GSPC", "^NDX", "/CL", "/GC"]
    
    now_utc = datetime.now(timezone.utc)
    est_time = now_utc - timedelta(hours=5)
    cst_time = now_utc - timedelta(hours=6)
    est_time_str = est_time.strftime('%I:%M:%S %p').lstrip('0')
    cst_time_str = cst_time.strftime('%I:%M:%S %p').lstrip('0')
    
    assets = {}
    print(f"Fetching assets for {symbols}")
    for symbol in symbols:
        if symbol not in market_data:
            logger.warning(f"No market data for {symbol}—skipping asset")
            assets[symbol] = {"price": 0, "gap": 0, "criteria_met": False, "butterfly": None}
            continue
        price = market_data[symbol]["price"]
        gap_percent = fetch_history_once(token, symbol)
        gap_percent_rounded = round(gap_percent, 2)
        has_news = check_news()
        butterfly = build_butterfly(token, symbol, price, market_data[symbol]["iv"], vix) if not has_news else None
        if butterfly and outcomes[symbol] == "Simulated win":
            active_positions.append(butterfly)
            print(f"Added butterfly for {symbol}: {butterfly}")
        assets[symbol] = {
            "price": price,
            "gap": gap_percent_rounded,
            "criteria_met": 0.05 <= abs(gap_percent_rounded) <= 1 and not has_news,
            "butterfly": butterfly
        }
    print(f"Assets populated: {assets}")
    print(f"Active positions: {active_positions}")
    
    highest_prob = None
    highest_prob_symbol = None
    for symbol, breakout in breakouts.items():
        if breakout is None:  # Skip None values
            logger.debug(f"Skipping None breakout for {symbol}")
            continue
        if highest_prob is None or breakout["probability"] > highest_prob:
            highest_prob = breakout["probability"]
            highest_prob_symbol = symbol
    
    recommendation = (f"Watch {highest_prob_symbol} (Prob: {highest_prob:.2f}) at 10am CST—check price action near 9:55 CST."
                     if highest_prob_symbol else "No breakouts with data available yet—awaiting futures/indices.")
    
    return {
        "assets": assets,
        "vix": vix,
        "gap_criteria": "0.05% - 1%",
        "active_positions": active_positions,
        "breakouts": breakouts,
        "breakout_status": breakout_status,
        "est_time": est_time_str,
        "cst_time": cst_time_str,
        "highest_prob_symbol": highest_prob_symbol,
        "recommendation": recommendation
    }

@app.route('/')
def dashboard():
    data = fetch_dashboard_data()
    logger.info(f"Data sent to template: {data['breakouts'].keys()}")
    return render_template('index.html', data=data)

@app.route('/monitor')
def monitor():
    return jsonify(breakout_status)

@app.route('/settings', methods=['GET', 'POST'])
def settings():
    if request.method == 'POST':
        strategy_settings["prob_scaler"] = float(request.form.get("prob_scaler", 0.2))
        strategy_settings["rr_ratio"] = float(request.form.get("rr_ratio", 2.0))
        return jsonify({"message": "Futures settings updated", "settings": strategy_settings})
    return render_template('settings.html', settings=strategy_settings)

@app.route('/forex_settings', methods=['GET', 'POST'])
def forex_settings():
    if request.method == 'POST':
        forex_strategy_settings["FastEMA_Period"] = int(request.form.get("FastEMA_Period", 20))
        forex_strategy_settings["SlowEMA_Period"] = int(request.form.get("SlowEMA_Period", 50))
        forex_strategy_settings["RSI_Period"] = int(request.form.get("RSI_Period", 14))
        forex_strategy_settings["LotSize"] = float(request.form.get("LotSize", 0.01))
        forex_strategy_settings["StopLossPips"] = int(request.form.get("StopLossPips", 20))
        forex_strategy_settings["TakeProfitPips"] = int(request.form.get("TakeProfitPips", 40))
        forex_strategy_settings["MaxDailyLoss"] = float(request.form.get("MaxDailyLoss", 3.0))
        forex_strategy_settings["StartHourCST"] = int(request.form.get("StartHourCST", 2))
        forex_strategy_settings["EndHourCST"] = int(request.form.get("EndHourCST", 9))
        return jsonify({"message": "Forex settings updated", "settings": forex_strategy_settings})
    return render_template('forex_settings.html', settings=forex_strategy_settings)

@app.route('/forex_settings_json')
def forex_settings_json():
    return jsonify(forex_strategy_settings)

@app.route('/forex_trades', methods=['POST'])
def forex_trades():
    trade_data = request.get_json()
    forex_trades.append(trade_data)
    logger.info(f"Received forex trade: {trade_data}")
    print(f"Received forex trade: {trade_data}")
    return jsonify({"message": "Trade logged", "trade": trade_data})

@app.route('/close/<symbol>')
def close_trade(symbol):
    global active_positions
    active_positions = [pos for pos in active_positions if pos["symbol"] != symbol]
    return {"message": f"Closed {symbol} at market (simulated)"}

@app.route('/close_all')
def close_all_trades():
    global active_positions
    closed = len(active_positions)
    active_positions = []
    return {"message": f"Closed all {closed} trades at market (simulated)"}

if __name__ == '__main__':
    threading.Thread(target=monitor_breakouts, daemon=True).start()
    app.run(debug=True, host='0.0.0.0', port=5010)  # Note: You said 5000, but code uses 5010—stick with 5010?
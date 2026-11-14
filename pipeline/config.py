"""Portfolio, demo research set and default watch rules.

Everything the investor can change lives here (or in the app's Alerts screen),
so the pipeline itself never hard-codes a company.
"""

# Mock portfolio: 18 NSE names across sectors and sizes. Weight = % of portfolio.
PORTFOLIO = [
    {"symbol": "RELIANCE",   "name": "Reliance Industries",   "sector": "Energy & Telecom",  "weight": 9.0},
    {"symbol": "HDFCBANK",   "name": "HDFC Bank",             "sector": "Banks",             "weight": 9.0},
    {"symbol": "TCS",        "name": "TCS",                   "sector": "IT Services",       "weight": 7.0},
    {"symbol": "INFY",       "name": "Infosys",               "sector": "IT Services",       "weight": 6.0},
    {"symbol": "ITC",        "name": "ITC",                   "sector": "FMCG",              "weight": 6.0},
    {"symbol": "LT",         "name": "Larsen & Toubro",       "sector": "Capital Goods",     "weight": 7.0},
    {"symbol": "BHARTIARTL", "name": "Bharti Airtel",         "sector": "Telecom",           "weight": 6.0},
    {"symbol": "SUNPHARMA",  "name": "Sun Pharma",            "sector": "Pharma",            "weight": 5.0},
    {"symbol": "MARUTI",     "name": "Maruti Suzuki",         "sector": "Auto",              "weight": 5.0},
    {"symbol": "TITAN",      "name": "Titan",                 "sector": "Consumer",          "weight": 5.0},
    {"symbol": "BAJFINANCE", "name": "Bajaj Finance",         "sector": "NBFC",              "weight": 6.0},
    {"symbol": "DMART",      "name": "Avenue Supermarts",     "sector": "Retail",            "weight": 4.0},
    {"symbol": "POLYCAB",    "name": "Polycab",               "sector": "Cables",            "weight": 4.0},
    {"symbol": "DIXON",      "name": "Dixon Technologies",    "sector": "Electronics",       "weight": 3.0},
    {"symbol": "HAL",        "name": "Hindustan Aeronautics", "sector": "Defence",           "weight": 4.0},
    {"symbol": "ETERNAL",    "name": "Eternal (Zomato)",      "sector": "Internet",          "weight": 3.0},
    {"symbol": "CDSL",       "name": "CDSL",                  "sector": "Capital Markets",   "weight": 2.5},
    {"symbol": "KPITTECH",   "name": "KPIT Technologies",     "sector": "Auto Software",     "weight": 2.5},
]

# Ad-hoc research demo set (outside the portfolio). The engine works for any NSE ticker.
RESEARCH_EXTRA = ["TRENT", "ASTRAL", "PIDILITIND", "CAMS"]

# Default watch rules. The app lets the investor change these.
DEFAULT_RULES = {
    "price_move_pct": 5.0,        # alert if |1-day move| beyond this
    "volume_multiple": 2.0,       # alert if volume > N x 20-day average
    "alert_score": 70,            # score at or above this is "Act now"
    "digest_score": 30,           # below this is collapsed as routine
    "muted_categories": ["investor_meetings", "agm_egm", "unclassified"],
}

INDEX_SYMBOL = "^NSEI"  # Nifty 50, used for move-relative-to-market

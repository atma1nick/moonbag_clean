import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN   = os.getenv("BOT_TOKEN", "")
DATABASE_URL= os.getenv("DATABASE_URL", "sqlite+aiosqlite:///moonbag.db")
HELIUS_KEY  = os.getenv("HELIUS_KEY", "")
ADMIN_ID    = int(os.getenv("ADMIN_ID", "0"))
TMA_URL     = os.getenv("TMA_URL", "")

# Feature flags
PRICE_CHECK_EVERY   = int(os.getenv("PRICE_CHECK_EVERY",   "60"))   # секунд
WALLET_CHECK_EVERY  = int(os.getenv("WALLET_CHECK_EVERY",  "90"))
TWITTER_CHECK_EVERY = int(os.getenv("TWITTER_CHECK_EVERY", "300"))

from datetime import datetime
from sqlalchemy import Column, Integer, BigInteger, String, Float, Boolean, DateTime, Text
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    user_id   = Column(BigInteger, primary_key=True)
    username  = Column(String(64), nullable=True)
    lang      = Column(String(4), default="en")
    wallet    = Column(String(64), nullable=True)   # их Solana кошелёк
    mode      = Column(String(16), default="manual") # manual | wallet
    currency  = Column(String(4),  default="SOL")   # SOL | USD
    whale_min = Column(Float,      default=5.0)      # SOL порог кита
    is_pro    = Column(Boolean,    default=False)
    pro_until = Column(DateTime,   nullable=True)
    created_at= Column(DateTime,   default=datetime.utcnow)


class Position(Base):
    __tablename__ = "positions"
    id          = Column(Integer,     primary_key=True, autoincrement=True)
    user_id     = Column(BigInteger,  nullable=False, index=True)
    contract    = Column(String(64),  nullable=False)
    symbol      = Column(String(32),  default="???")
    name        = Column(String(128), default="")
    entry_price = Column(Float,       default=0.0)
    entry_mcap  = Column(Float,       default=0.0)
    sol_in      = Column(Float,       default=0.0)
    exit_plan   = Column(Text,        nullable=True)   # JSON [{"x":4,"pct":50},...]
    stop_loss   = Column(Float,       default=0.0)     # mcap уровень, 0=выкл
    source      = Column(String(16),  default="manual")# manual|wallet|kol
    status      = Column(String(16),  default="active")# active|closed
    note        = Column(Text,        nullable=True)
    closed_at   = Column(DateTime,    nullable=True)
    exit_price  = Column(Float,       nullable=True)
    exit_mcap   = Column(Float,       nullable=True)
    sol_out     = Column(Float,       default=0.0)
    created_at  = Column(DateTime,    default=datetime.utcnow)


class JournalEntry(Base):
    __tablename__ = "journal"
    id          = Column(Integer,   primary_key=True, autoincrement=True)
    user_id     = Column(BigInteger,nullable=False, index=True)
    position_id = Column(Integer,   nullable=True)
    contract    = Column(String(64),nullable=False)
    symbol      = Column(String(32),default="???")
    sol_in      = Column(Float,     default=0.0)
    sol_out     = Column(Float,     default=0.0)
    pnl_sol     = Column(Float,     default=0.0)
    pnl_pct     = Column(Float,     default=0.0)
    exit_x      = Column(Float,     default=0.0)
    note        = Column(Text,      nullable=True)
    error_tag   = Column(String(32),nullable=True)  # sold_early|held_too_long|etc
    created_at  = Column(DateTime,  default=datetime.utcnow)


class SmartWallet(Base):
    __tablename__ = "smart_wallets"
    id       = Column(Integer,    primary_key=True, autoincrement=True)
    user_id  = Column(BigInteger, nullable=False, index=True)
    address  = Column(String(64), nullable=False)
    label    = Column(String(64), nullable=True)
    winrate  = Column(Float,      default=0.0)
    added_at = Column(DateTime,   default=datetime.utcnow)


class SmartWalletTx(Base):
    __tablename__ = "smart_wallet_txs"
    id         = Column(Integer,    primary_key=True, autoincrement=True)
    user_id    = Column(BigInteger, nullable=False, index=True)
    address    = Column(String(64), nullable=False)
    contract   = Column(String(64), nullable=False)
    label      = Column(String(64), nullable=True)
    action     = Column(String(8),  default="buy")   # buy|sell
    sol_amount = Column(Float,      default=0.0)
    tx_sig     = Column(String(128),nullable=True, unique=True)
    seen_at    = Column(DateTime,   default=datetime.utcnow)


class KOL(Base):
    __tablename__ = "kols"
    id       = Column(Integer,    primary_key=True, autoincrement=True)
    user_id  = Column(BigInteger, nullable=False, index=True)
    handle   = Column(String(64), nullable=False)
    added_at = Column(DateTime,   default=datetime.utcnow)


class SeenTx(Base):
    __tablename__ = "seen_txs"
    sig = Column(String(128), primary_key=True)


class FiredAlert(Base):
    """Дедупликация алертов — чтобы один и тот же не слать дважды."""
    __tablename__ = "fired_alerts"
    id         = Column(Integer,    primary_key=True, autoincrement=True)
    user_id    = Column(BigInteger, nullable=False)
    alert_key  = Column(String(256),nullable=False, unique=True)
    fired_at   = Column(DateTime,   default=datetime.utcnow)

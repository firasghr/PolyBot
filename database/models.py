import time
from sqlalchemy import Column, String, Float, Integer, Boolean, JSON
from sqlalchemy.orm import declarative_base

Base = declarative_base()

class Trader(Base):
    __tablename__ = "traders"

    wallet = Column(String(42), primary_key=True, index=True)
    name = Column(String(100), default="")
    pseudonym = Column(String(100), default="")
    profile_image = Column(String(255), default="")
    bio = Column(String(), default="")

    # Performance Stats
    trade_count = Column(Integer, default=0)
    decided_trades = Column(Integer, default=0)
    win_rate = Column(Float, default=0.0)
    avg_position_size_usdc = Column(Float, default=0.0)
    avg_win_usdc = Column(Float, default=0.0)
    avg_loss_usdc = Column(Float, default=0.0)
    market_focus = Column(String(50), default="other")
    market_distribution = Column(JSON, default=dict) # JSON mapping counts
    sharpe_ratio = Column(Float, default=0.0)
    total_pnl_usdc = Column(Float, default=0.0)
    
    # PolyBot v3 Scoring
    composite_score = Column(Float, default=0.0)
    last_updated = Column(Float, default=lambda: time.time())


class Trade(Base):
    """A historically executed trade, either opened (and maybe closed) by the bot."""
    __tablename__ = "trades"

    id = Column(String(64), primary_key=True, index=True)
    wallet = Column(String(42), index=True)
    market_id = Column(String(100), index=True)  # condition_id or slug
    market_title = Column(String(255))
    outcome = Column(String(50))   # "Yes", "No", etc.
    side = Column(String(10))      # "BUY" or "SELL"
    category = Column(String(50), default="other")
    
    entry_price = Column(Float, default=0.0)
    exit_price = Column(Float, default=0.0)
    size_usdc = Column(Float, default=0.0)
    shares = Column(Float, default=0.0)
    
    entry_timestamp = Column(Float, default=lambda: time.time())
    exit_timestamp = Column(Float, default=0.0)
    
    realised_pnl = Column(Float, default=0.0)
    status = Column(String(20), default="open")  # "open", "closed", "failed"
    closed_outcome = Column(String(20), default="open") # "win", "loss", "open"
    evm_tx_hash = Column(String(100), default="")

class Position(Base):
    """An active holding that needs to be tracked for exit signals."""
    __tablename__ = "positions"

    id = Column(String(64), primary_key=True) # Usually matches the open trade id
    wallet = Column(String(42), index=True)
    market_id = Column(String(100), index=True)  
    market_title = Column(String(255))
    outcome = Column(String(50))
    category = Column(String(50), default="other")
    
    entry_price = Column(Float)
    size_usdc = Column(Float)
    shares = Column(Float)
    timestamp = Column(Float, default=lambda: time.time())

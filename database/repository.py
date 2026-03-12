from typing import Sequence, Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update, delete
from sqlalchemy.dialects.sqlite import insert as sqlite_upsert

from .models import Trade, Position, Trader

class DBRepository:
    """Encapsulates all SQLite database operations for PolyBot v3."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # --------------------------------------------------------------------------
    # Trades
    # --------------------------------------------------------------------------
    async def get_trade(self, trade_id: str) -> Optional[Trade]:
        stmt = select(Trade).where(Trade.id == trade_id)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()
    
    async def get_all_trades(self, limit: int = 100) -> Sequence[Trade]:
        stmt = select(Trade).order_by(Trade.entry_timestamp.desc()).limit(limit)
        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def add_trade(self, trade: Trade) -> None:
        self.session.add(trade)
        await self.session.commit()
    
    async def update_trade(self, trade: Trade) -> None:
        await self.session.merge(trade)
        await self.session.commit()

    # --------------------------------------------------------------------------
    # Positions
    # --------------------------------------------------------------------------
    async def get_open_positions(self) -> Sequence[Position]:
        stmt = select(Position).order_by(Position.timestamp.desc())
        result = await self.session.execute(stmt)
        return result.scalars().all()
    
    async def get_position_by_id(self, pos_id: str) -> Optional[Position]:
        stmt = select(Position).where(Position.id == pos_id)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def add_position(self, position: Position) -> None:
        self.session.add(position)
        await self.session.commit()

    async def remove_position(self, pos_id: str) -> None:
        stmt = delete(Position).where(Position.id == pos_id)
        await self.session.execute(stmt)
        await self.session.commit()

    # --------------------------------------------------------------------------
    # Traders (Watched Wallets)
    # --------------------------------------------------------------------------
    async def upsert_trader(self, trader_data: dict) -> None:
        """Insert or update a trader record."""
        stmt = sqlite_upsert(Trader).values(**trader_data)
        
        # When conflict on primary key (wallet), update the stats
        update_dict = {
            c.name: c
            for c in stmt.excluded
            if c.name != "wallet"
        }
        
        stmt = stmt.on_conflict_do_update(
            index_elements=["wallet"],
            set_=update_dict
        )
        await self.session.execute(stmt)
        await self.session.commit()
        
    async def get_top_traders(self, limit: int = 20) -> Sequence[Trader]:
        stmt = select(Trader).order_by(Trader.composite_score.desc()).limit(limit)
        result = await self.session.execute(stmt)
        return result.scalars().all()

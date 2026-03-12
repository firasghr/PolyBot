"""
EVM Execution Service
=====================
Handles live trading execution on Polygon via web3.py.
Interacts with the Polymarket CTF (Conditional Token Framework) adapter contracts.
Writes successful trades directly to the SQLite Trade table.
"""

import logging
import os
from typing import Optional

from web3 import Web3
from eth_account import Account
from database.models import Trade

logger = logging.getLogger(__name__)

POLYGON_RPC_URL = os.getenv("POLYGON_RPC_URL", "https://polygon-rpc.com")
PRIVATE_KEY = os.getenv("PRIVATE_KEY", "")
POLYMARKET_CTF_ADAPTER = os.getenv("POLYMARKET_CTF_ADAPTER", "0x4bFbB706B491322EfE19659BA713cCAEEe0828A2")


class EVMExecutionService:
    def __init__(self, repo):
        """
        Initialises the EVM Execution Service.
        :param repo: The DBRepository for writing trades.
        """
        self.repo = repo
        self.w3 = Web3(Web3.HTTPProvider(POLYGON_RPC_URL))
        
        self.account = None
        if PRIVATE_KEY:
            try:
                self.account = Account.from_key(PRIVATE_KEY)
                logger.info("EVMExecutionService initialized for wallet %s", self.account.address)
            except Exception as e:
                logger.error("Failed to initialize account from PRIVATE_KEY: %s", e)
        else:
            logger.warning("No PRIVATE_KEY provided. EVMExecutionService will run in dry/stub mode.")

    async def execute_trade(
        self,
        trade_id: str,
        wallet: str,
        market: str,
        side: str,
        size_usdc: float,
        nominal_price: float,
        category: str
    ) -> Optional[Trade]:
        """
        Executes a trade on-chain.
        (This is a framework/stub method intended to be expanded with actual ABI interactions)
        
        Returns the created Trade object on success, or None on failure.
        """
        if not self.w3.is_connected():
            logger.error("Web3 is not connected to Polygon RPC.")
            return None

        if not self.account:
            logger.error("Cannot execute trade; no valid account/private key.")
            return None

        logger.info(
            "Executing %s live trade on EVM. Market: %s | Size: $%.2f | Expected Price: %.4f",
            side, market, size_usdc, nominal_price
        )

        try:
            # --- STUB FOR ACTUAL ON-CHAIN TRANSACTION ---
            # 1. Fetch condition ABI and build tx
            # 2. Estimate gas
            # 3. Sign and send transaction
            # 4. Wait for receipt
            # For now, we simulate a successful transaction:
            transaction_hash = "0x" + os.urandom(32).hex()
            logger.info("Transaction simulated successfully with hash: %s", transaction_hash)
            
            # Write directly to DB via repository
            trade = Trade(
                id=trade_id,
                wallet=wallet,
                market_title=market,
                side=side,
                entry_price=nominal_price,
                size_usdc=size_usdc,
                category=category,
                evm_tx_hash=transaction_hash
            )
            await self.repo.add_trade(trade)
            return trade

        except Exception as e:
            logger.error("Live EVM trade failed: %s", e)
            return None

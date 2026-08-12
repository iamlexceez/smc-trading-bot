import logging
import asyncio
from typing import List, Dict, Any, Optional
from executors.mt5 import MT5Executor
from executors.base import BaseExecutor, ExecutionResult
from config import BrokerConfig, TradeSettings

logger = logging.getLogger(__name__)

class MultiBrokerManager(BaseExecutor):
    def __init__(self, settings: TradeSettings):
        super().__init__("MultiBrokerManager")
        self.settings = settings
        self.executors: Dict[str, MT5Executor] = {}
        self._initialize_executors()

    def _initialize_executors(self):
        """Create an executor for each active broker."""
        for broker in self.settings.brokers:
            if broker.is_active:
                # Create a specialized MT5Executor for this broker
                executor = MT5Executor()
                # Override credentials for this specific instance
                executor.login = broker.login
                executor.password = broker.password
                executor.server = broker.server
                executor.terminal_path = broker.terminal_path
                self.executors[broker.name] = executor
                logger.info(f"Initialized executor for broker: {broker.name}")

    async def get_account_info(self) -> Dict[str, Any]:
        """Get aggregated account info or primary account info."""
        if not self.executors:
            return {"balance": 0, "equity": 0, "error": "No active brokers"}
        
        # For now, return the primary (first) broker's info
        primary = list(self.executors.values())[0]
        return await primary.get_account_info()

    async def execute_trade(self, **kwargs) -> ExecutionResult:
        """Execute trade across all active brokers based on sync_mode."""
        if not self.executors:
            return ExecutionResult(success=False, message="No active brokers configured")

        results = []
        
        # Mirror mode: Execute the exact same trade on all brokers
        if self.settings.sync_mode == "mirror":
            for name, executor in self.executors.items():
                try:
                    res = await executor.execute_trade(**kwargs)
                    results.append(res)
                    logger.info(f"Trade executed on {name}: {res.message}")
                except Exception as e:
                    logger.error(f"Failed to execute trade on {name}: {e}")
            
            # Return success if at least one succeeded
            success = any(r.success for r in results)
            msg = "; ".join([f"{n}: {r.message}" for n, r in zip(self.executors.keys(), results)])
            return ExecutionResult(success=success, message=msg)

        # Split mode: Divide the lot size across brokers
        elif self.settings.sync_mode == "split":
            total_lot = kwargs.get("lot_size", 0.01)
            share = total_lot / len(self.executors)
            
            for name, executor in self.executors.items():
                split_kwargs = kwargs.copy()
                split_kwargs["lot_size"] = share
                res = await executor.execute_trade(**split_kwargs)
                results.append(res)
            
            success = any(r.success for r in results)
            return ExecutionResult(success=success, message="Split execution completed")

        return ExecutionResult(success=False, message="Invalid sync mode")

    async def close_all_positions(self, symbol: str = None) -> bool:
        """Close positions across all brokers."""
        success = True
        for executor in self.executors.values():
            if not await executor.close_all_positions(symbol):
                success = False
        return success

    async def get_open_positions(self, symbol: str = None) -> List[Dict[str, Any]]:
        """Get open positions from all brokers."""
        all_positions = []
        for name, executor in self.executors.items():
            positions = await executor.get_open_positions(symbol)
            for p in positions:
                p["broker"] = name
            all_positions.extend(positions)
        return all_positions

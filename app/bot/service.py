"""交易服务生命周期管理。"""

import logging
import time
from threading import Event, Lock, Thread
from typing import Dict, Optional

from app.bot.engine import TradingEngine


logger = logging.getLogger(__name__)


class TradingService:
    """在单一后台线程中调度交易周期。"""

    # 连续失败后退避等待的最大倍数，避免故障期间高频重试打爆接口与预算
    MAX_BACKOFF_MULTIPLIER = 4
    MAX_BACKOFF_SECONDS = 3600

    def __init__(self, engine: TradingEngine, app):
        self.engine = engine
        self.app = app
        self._state_lock = Lock()
        self._stop_event = Event()
        self._thread: Optional[Thread] = None
        self._last_error: Optional[str] = None
        self._halt_reason: Optional[str] = None

    @property
    def is_running(self) -> bool:
        with self._state_lock:
            return bool(self._thread and self._thread.is_alive())

    @property
    def live_trading(self) -> bool:
        return self.engine.live_trading

    @property
    def halt_reason(self) -> Optional[str]:
        return self._halt_reason

    def start(self):
        with self._state_lock:
            if self._thread and self._thread.is_alive():
                raise RuntimeError("机器人已在运行")
        # 启动前验证交易所可达并校准时钟；该调用涉及网络 I/O，
        # 放在状态锁外，避免阻塞并发的状态查询。
        self.engine.data_engine.binance.synchronize_time()
        with self._state_lock:
            if self._thread and self._thread.is_alive():
                raise RuntimeError("机器人已在运行")
            self._stop_event.clear()
            self._last_error = None
            self._halt_reason = None
            self._thread = Thread(
                target=self._trading_loop,
                name="trading-loop",
                daemon=True,
            )
            self._thread.start()
        logger.info("交易服务已启动")

    def stop(self, wait_seconds: float = 5) -> bool:
        """请求停止交易循环，返回线程是否已在等待时间内结束。

        单个交易周期可能持续数分钟，因此超时返回 False 属正常情况，
        调用方应据此告知用户循环仍在收尾。
        """
        with self._state_lock:
            thread = self._thread
            if not thread or not thread.is_alive():
                raise RuntimeError("机器人未运行")
            self._stop_event.set()
        thread.join(timeout=wait_seconds)
        stopped = not thread.is_alive()
        logger.info("交易服务停止信号已处理，线程已结束: %s", stopped)
        return stopped

    def run_once(self) -> dict:
        if self.is_running:
            raise RuntimeError("后台交易循环运行中，不能执行单次周期")
        with self.app.app_context():
            return self.engine.run_cycle()

    def enable_live_trading(self, enable: bool):
        if self.is_running:
            raise RuntimeError("切换交易模式前必须停止后台循环")
        self.engine.enable_live_trading(enable)

    def set_custom_instructions(self, instructions: str):
        self.engine.set_custom_instructions(instructions)

    def get_status(self) -> Dict:
        status = {
            "running": self.is_running,
            "live_trading": self.engine.live_trading,
            "timestamp": time.time(),
            "last_error": self._last_error,
            "halt_reason": self._halt_reason,
        }
        status.update(self.engine.get_status())
        return status

    def _wait_seconds(self, interval: int, elapsed: float, failures: int) -> float:
        """计算下一个周期前的等待时间，失败时指数退避。"""
        if failures <= 0:
            return max(0, interval - elapsed)
        multiplier = 2 ** min(failures, self.MAX_BACKOFF_MULTIPLIER)
        return min(interval * multiplier, self.MAX_BACKOFF_SECONDS)

    def _trading_loop(self):
        interval = self.engine.config.TRADING_INTERVAL_MINUTES * 60
        max_failures = self.engine.config.MAX_CONSECUTIVE_CYCLE_FAILURES
        logger.info("交易循环已激活，间隔 %d 秒", interval)
        failures = 0
        try:
            while not self._stop_event.is_set():
                started = time.monotonic()
                try:
                    with self.app.app_context():
                        result = self.engine.run_cycle()
                except Exception as exc:
                    # 单个周期的异常不得终止循环，否则机器人会静默停摆。
                    failures += 1
                    self._last_error = str(exc)
                    logger.exception("交易周期异常: %s", exc)
                else:
                    if result.get("halt_required"):
                        self._halt_reason = result.get("error") or "需要人工核对"
                        self._last_error = self._halt_reason
                        logger.critical("交易循环已暂停: %s", self._halt_reason)
                        break
                    if result.get("success"):
                        failures = 0
                        self._last_error = None
                    else:
                        failures += 1
                        self._last_error = result.get("error") or "交易周期未成功"
                        logger.error("交易周期失败: %s", self._last_error)
                if failures >= max_failures:
                    self._halt_reason = (
                        f"连续 {failures} 个周期未成功，已暂停交易循环：{self._last_error}"
                    )
                    logger.critical("交易循环已暂停: %s", self._halt_reason)
                    break
                elapsed = time.monotonic() - started
                self._stop_event.wait(self._wait_seconds(interval, elapsed, failures))
        finally:
            self._stop_event.set()
            logger.info("交易循环已结束")

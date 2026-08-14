"""
宏观指标表述模块。

把已采集的宏观指标（市场宽度等）转写为提示词可读的摘要。
数据采集由行情客户端负责，本模块不发起网络请求。
"""

import logging
import math

logger = logging.getLogger(__name__)


class MacroDataClient:
    """
    宏观市场指标的表述器。
    
    将市场宽度等指标转为自然语言判断，供模型解读。
    """
    
    def format_macro_summary(self, advance_decline_ratio: float) -> str:
        """
        将宏观数据格式化为人类可读的 AI 上下文摘要。
        
        Args:
            advance_decline_ratio: 市场宽度指标
            
        Returns:
            格式化的摘要字符串
        """
        if advance_decline_ratio is None or not math.isfinite(advance_decline_ratio):
            return "全球市场上下文:\n- 市场宽度 (A/D 比率): 不可用 - 禁止据此判断方向"

        # 格式化 A/D 比率
        if advance_decline_ratio > 1.5:
            ad_assessment = "强劲 (广泛反弹)"
        elif advance_decline_ratio > 1.0:
            ad_assessment = "健康"
        elif advance_decline_ratio > 0.5:
            ad_assessment = "疲软 (BTC 主导)"
        else:
            ad_assessment = "非常疲软 (市场低迷)"
        
        # 极端值统一收敛显示，避免刷屏
        if advance_decline_ratio >= 9999:
            ad_display = "9999+"
        else:
            ad_display = f"{advance_decline_ratio:.2f}"
        
        return f"""全球市场上下文:
- 市场宽度 (A/D 比率): {ad_display} - {ad_assessment}"""

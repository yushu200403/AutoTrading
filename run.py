"""
OpenNOF1 - 启动脚本

启动带有已初始化交易服务的 Flask 应用程序。
"""

import logging
import os

from app.runtime import create_runtime_app
from config import get_config

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

def main():
    """主入口点。"""
    config = get_config()
    
    app = create_runtime_app()
    engine = app.extensions["trading_service"].engine
    
    # 从环境变量获取端口或使用默认值
    port = int(os.environ.get('PORT', 5000))
    host = os.environ.get('HOST_BIND_ADDRESS', '127.0.0.1')
    
    mode_label = "实盘交易（真实订单）" if engine.live_trading else "模拟交易（真实行情）"
    print(f"""
╔════════════════════════════════════════╗
║                     OPENNOF1                           ║
║                  AI 自动交易工作流                      ║
╠════════════════════════════════════════╣
║  仪表板:   http://localhost:{port}                    ║
║  设置:    http://localhost:{port}/settings            ║
╠════════════════════════════════════════╣
║  模式:    {mode_label:<43}║
║  提示:    实盘模式需要环境变量显式确认                 ║
╚════════════════════════════════════════╝
    """)
    
    # 运行 Flask 应用
    # 关闭自动重载，防止调试模式下服务被初始化两次
    app.run(
        host=host,
        port=port,
        debug=config.DEBUG,
        threaded=True,
        use_reloader=False  # 禁用自动重载，避免交易服务重复初始化
    )


if __name__ == '__main__':
    main()

"""
OpenNOF1 Web 界面的 Flask 路由。

提供仪表板、API 端点。设置功能已整合到仪表板页面。
"""

import hmac
import json
import logging
import secrets
import time
from functools import wraps
from datetime import datetime, timezone, timedelta
from typing import Optional
from flask import (
    Blueprint,
    current_app,
    jsonify,
    redirect,
    render_template,
    request,
    session,
)

from app.models import EquitySnapshot, MemoryBoard, TradeDecision
from app.bot.data_engine import DataEngine
from app.bot.service import TradingService

logger = logging.getLogger(__name__)

# 主路由蓝图
main_bp = Blueprint('main', __name__)

# 服务实例 (由 run.py 设置)
_service: Optional[TradingService] = None


def _constant_time_equal(left: str, right: str) -> bool:
    """对任意 Unicode 文本执行时序安全比较。"""
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def _format_timestamp(dt: datetime) -> Optional[str]:
    """将 datetime 序列化为带时区的 ISO 字符串。
    
    假设数据库存储的 naive datetime 为 UTC 时间，
    转换到配置的时区后输出，确保浏览器能正确解析。
    """
    if dt is None:
        return None
    tz = timezone(timedelta(hours=current_app.config['TIMEZONE_OFFSET']))
    
    # 如果是不带时区的时间，按 UTC 解释后转换到配置时区
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    
    # 转换到配置的时区
    local_dt = dt.astimezone(tz)
    return local_dt.isoformat()


def init_service(service: TradingService):
    """初始化交易服务引用。"""
    global _service
    _service = service


def _session_authenticated() -> bool:
    """判断当前会话是否处于有效的认证窗口内。"""
    authenticated_at = session.get('console_authenticated_at')
    if not authenticated_at:
        return False
    ttl_seconds = current_app.config['CONSOLE_SESSION_TTL_MINUTES'] * 60
    return time.time() - authenticated_at <= ttl_seconds


def _control_auth_required(view):
    """为控制接口提供可配置的会话认证与 CSRF 校验。"""
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_app.config['CONSOLE_AUTH_ENABLED']:
            return view(*args, **kwargs)
        if not _session_authenticated():
            session.clear()
            return jsonify({'error': '控制台会话未认证或已过期'}), 401
        expected = session.get('csrf_token', '')
        provided = request.headers.get('X-CSRF-Token', '')
        if not expected or not _constant_time_equal(provided, expected):
            return jsonify({'error': 'CSRF 校验失败'}), 403
        return view(*args, **kwargs)
    return wrapped


def _readonly_auth_required(view):
    """在配置禁止访客读取时要求有效的控制台会话。"""
    @wraps(view)
    def wrapped(*args, **kwargs):
        config = current_app.config
        if not (
            config['CONSOLE_AUTH_ENABLED']
            and config['CONSOLE_READONLY_AUTH_ENABLED']
        ):
            return view(*args, **kwargs)
        if not _session_authenticated():
            session.clear()
            return jsonify({'error': '登录后才能查看账户与策略数据'}), 401
        return view(*args, **kwargs)
    return wrapped


# =============================================================================
# 页面路由
# =============================================================================

@main_bp.route('/')
def dashboard():
    """仪表板页面。"""
    return render_template('dashboard.html')


@main_bp.route('/settings')
def settings():
    """设置入口重定向到仪表板中的设置标签。"""
    return redirect('/#settings')


# =============================================================================
# 只读接口
# =============================================================================

@main_bp.route('/healthz')
def healthz():
    """容器存活探针；不暴露账户、持仓或策略信息。"""
    return jsonify({'status': 'ok'})


@main_bp.route('/api/auth-status')
def api_auth_status():
    """返回当前浏览器是否具备读取与控制权限。"""
    authentication_required = current_app.config['CONSOLE_AUTH_ENABLED']
    readonly_authentication_required = bool(
        authentication_required
        and current_app.config['CONSOLE_READONLY_AUTH_ENABLED']
    )
    if not authentication_required:
        return jsonify({
            'authentication_required': False,
            'control_access': True,
            'readonly_authentication_required': False,
            'read_access': True,
        })

    session_authenticated = _session_authenticated()
    if not session_authenticated:
        session.clear()
        control_access = False
    else:
        expected = session.get('csrf_token', '')
        provided = request.headers.get('X-CSRF-Token', '')
        control_access = bool(
            expected and _constant_time_equal(provided, expected)
        )

    return jsonify({
        'authentication_required': True,
        'control_access': control_access,
        'readonly_authentication_required': readonly_authentication_required,
        'read_access': (
            session_authenticated or not readonly_authentication_required
        ),
    })


@main_bp.route('/api/status')
@_readonly_auth_required
def api_status():
    """获取机器人状态。"""
    if not _service:
        return jsonify({'error': '交易服务未初始化'}), 503
    
    status = _service.get_status()
    status['timezone_offset'] = current_app.config['TIMEZONE_OFFSET']
    return jsonify(status)


@main_bp.route('/api/tickers')
@_readonly_auth_required
def api_tickers():
    """获取当前行情数据（含 24h 迷你走势）。"""
    if not _service:
        return jsonify({'error': '交易服务未初始化'}), 503
    
    try:
        tickers = []
        for symbol in _service.engine.data_engine.symbols:
            ticker = _service.engine.data_engine.binance.fetch_ticker(symbol)
            
            # 获取 24 小时 K 线数据作为迷你走势（1 小时间隔，共 24 根）
            try:
                ohlcv = _service.engine.data_engine.binance.fetch_ohlcv(symbol, '1h', 24)
                sparkline = [candle[4] for candle in ohlcv]  # 收盘价
            except Exception as e:
                logger.debug("获取 %s sparkline 失败: %s", symbol, e)
                sparkline = []
            
            tickers.append({
                'symbol': symbol,
                'price': ticker.last_price,
                'change_24h': ticker.change_24h_percent,
                'high': ticker.high_24h,
                'low': ticker.low_24h,
                'volume': ticker.volume_24h,
                'sparkline': sparkline
            })
        return jsonify(tickers)
    except Exception as e:
        logger.error("获取行情失败: %s", e)
        return jsonify({'error': str(e)}), 500


@main_bp.route('/api/alpha')
@_readonly_auth_required
def api_alpha():
    """获取 Alpha 指标。"""
    if not _service:
        return jsonify({'error': '交易服务未初始化'}), 503
    
    try:
        binance = _service.engine.data_engine.binance
        
        breadth = binance.fetch_top_gainers_losers(50)
        
        return jsonify({
            'advance_decline_ratio': breadth['advance_decline_ratio'],
            'top_gainers': breadth['gainers'][:3],
            'top_losers': breadth['losers'][:3]
        })
    except Exception as e:
        logger.error("获取 Alpha 指标失败: %s", e)
        return jsonify({'error': str(e)}), 500


@main_bp.route('/api/decisions')
@_readonly_auth_required
def api_decisions():
    """获取近期交易决策（含工具调用详情）。"""
    try:
        decisions = TradeDecision.query.order_by(
            TradeDecision.timestamp.desc()
        ).limit(20).all()
        
        result = []
        for d in decisions:
            # 解析 tool_args
            try:
                args = json.loads(d.tool_args) if d.tool_args else {}
            except Exception:
                args = {}
            
            result.append({
                'id': d.id,
                'timestamp': _format_timestamp(d.timestamp),
                'symbol': d.symbol,
                'action': d.action,
                'info': d.display_info,
                'tool_name': d.tool_name,
                'args': args,
                'status': d.execution_status,
                'price': d.executed_price,
                'reasoning': d.ai_reasoning  # AI 分析文本
            })
        
        return jsonify(result)
    except Exception as e:
        logger.error("获取近期决策失败: %s", e)
        return jsonify({'error': str(e)}), 500


@main_bp.route('/api/records')
@_readonly_auth_required
def api_records():
    """获取历史交易记录。"""
    try:
        limit = request.args.get('limit', default=200, type=int)
        limit = max(1, min(limit, 1000))
        query = TradeDecision.query.order_by(TradeDecision.timestamp.desc())
        query = query.limit(limit)
        decisions = query.all()
        
        result = []
        for d in decisions:
            try:
                args = json.loads(d.tool_args) if d.tool_args else {}
            except Exception:
                args = {}
            
            result.append({
                'id': d.id,
                'timestamp': _format_timestamp(d.timestamp),
                'symbol': d.symbol,
                'action': d.action,
                'info': d.display_info,
                'tool_name': d.tool_name,
                'args': args,
                'status': d.execution_status,
                'price': d.executed_price,
                'reasoning': d.ai_reasoning
            })
        
        return jsonify(result)
    except Exception as e:
        logger.error("获取交易记录失败: %s", e)
        return jsonify({'error': str(e)}), 500


@main_bp.route('/api/positions')
@_readonly_auth_required
def api_positions():
    """获取当前持仓。"""
    if not _service:
        return jsonify({'error': '交易服务未初始化'}), 503
    
    try:
        positions = _service.engine.broker.fetch_positions()
        return jsonify(positions)
    except Exception as e:
        logger.error("获取持仓失败: %s", e)
        return jsonify({'error': str(e)}), 500


@main_bp.route('/api/memory')
@_readonly_auth_required
def api_memory():
    """获取当前记忆白板内容。"""
    try:
        board = MemoryBoard.get_or_create()
        return jsonify({
            'content': board.content,
            'last_updated': _format_timestamp(board.last_updated)
        })
    except Exception as e:
        logger.error("获取记忆白板失败: %s", e)
        return jsonify({'error': str(e)}), 500


# =============================================================================
# 控制接口
# =============================================================================

@main_bp.route('/api/start', methods=['POST'])
@_control_auth_required
def api_start():
    """启动交易循环。"""
    if not _service:
        return jsonify({'error': '交易服务未初始化'}), 503
    
    try:
        _service.start()
        return jsonify({'success': True, 'message': '交易循环已启动'})
    except RuntimeError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        logger.exception("启动交易循环失败: %s", e)
        return jsonify({'error': '启动交易循环失败'}), 500


@main_bp.route('/api/stop', methods=['POST'])
@_control_auth_required
def api_stop():
    """停止交易循环。"""
    if not _service:
        return jsonify({'error': '交易服务未初始化'}), 503
    
    try:
        stopped = _service.stop()
        return jsonify({
            'success': True,
            'stopped': stopped,
            'message': (
                '交易循环已停止' if stopped
                else '停止信号已发送，当前周期正在收尾'
            ),
        })
    except RuntimeError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        logger.exception("停止交易循环失败: %s", e)
        return jsonify({'error': '停止交易循环失败'}), 500


@main_bp.route('/api/verify-password', methods=['POST'])
def api_verify_password():
    """验证控制台密码。"""
    data = request.get_json(silent=True) or {}
    password = data.get('password', '')
    if not isinstance(password, str):
        return jsonify({'success': False, 'error': '密码格式无效'}), 400
    
    # 使用时序安全的密码比较防止时序攻击
    if _constant_time_equal(password, current_app.config['CONSOLE_PASSWORD']):
        csrf_token = secrets.token_urlsafe(32)
        session.clear()
        session['console_authenticated_at'] = time.time()
        session['csrf_token'] = csrf_token
        return jsonify({'success': True, 'csrf_token': csrf_token})
    else:
        return jsonify({'success': False, 'error': '密码错误'}), 401


@main_bp.route('/api/logout', methods=['POST'])
@_control_auth_required
def api_logout():
    """退出控制台会话。"""
    session.clear()
    return jsonify({'success': True})


@main_bp.route('/api/live', methods=['POST'])
@_control_auth_required
def api_toggle_live():
    """切换实盘交易模式。"""
    if not _service:
        return jsonify({'error': '交易服务未初始化'}), 503
    
    data = request.get_json(silent=True) or {}
    enable = data.get('enable', False)
    if not isinstance(enable, bool):
        return jsonify({'error': '启用参数必须是布尔值'}), 400
    
    try:
        _service.enable_live_trading(enable)
    except RuntimeError as exc:
        return jsonify({'error': str(exc)}), 400
    except Exception as exc:
        logger.exception("切换交易模式失败: %s", exc)
        return jsonify({'error': '切换交易模式失败'}), 500
    
    return jsonify({
        'success': True, 
        'live_trading': _service.live_trading
    })


@main_bp.route('/api/instructions', methods=['GET'])
@_readonly_auth_required
def api_get_instructions():
    """获取当前自定义交易指令。"""
    try:
        from app.models import SystemSettings
        settings = SystemSettings.get_or_create()
        return jsonify({
            'instructions': settings.custom_instructions or '',
            'last_updated': _format_timestamp(settings.last_updated)
        })
    except Exception as e:
        logger.error("获取自定义指令失败: %s", e)
        return jsonify({'error': str(e)}), 500


@main_bp.route('/api/instructions', methods=['POST'])
@_control_auth_required
def api_instructions():
    """更新自定义交易指令。"""
    if not _service:
        return jsonify({'error': '交易服务未初始化'}), 503
    
    data = request.get_json(silent=True) or {}
    instructions = data.get('instructions', '')
    if not isinstance(instructions, str):
        return jsonify({'error': '自定义指令必须是字符串'}), 400
    try:
        _service.set_custom_instructions(instructions)
        return jsonify({'success': True, 'message': '自定义指令已更新'})
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400
    except Exception as exc:
        logger.exception("保存自定义指令失败: %s", exc)
        return jsonify({'error': '保存自定义指令失败'}), 500



@main_bp.route('/api/run-once', methods=['POST'])
@_control_auth_required
def api_run_once():
    """运行单个交易循环。"""
    if not _service:
        return jsonify({'error': '交易服务未初始化'}), 503
    
    try:
        result = _service.run_once()
        return jsonify(result)
    except RuntimeError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        logger.exception("单次交易周期失败: %s", e)
        return jsonify({'error': '单次交易周期失败'}), 500


@main_bp.route('/api/close-all', methods=['POST'])
@_control_auth_required
def api_close_all_positions():
    """一键全平：平掉所有持仓并取消所有挂单。"""
    if not _service:
        return jsonify({'error': '交易服务未初始化'}), 503
    
    try:
        results = _service.engine.close_all_positions()
        
        success = len(results['errors']) == 0
        return jsonify({
            'success': success,
            'trading_mode': _service.engine.trading_mode,
            'message': f"已平仓 {len(results['closed'])} 个持仓",
            'results': results
        })
    except Exception as e:
        logger.error("一键全平失败: %s", e)
        return jsonify({'error': str(e)}), 500


# =============================================================================
# 账户与净值接口
# =============================================================================

@main_bp.route('/api/account-summary')
@_readonly_auth_required
def api_account_summary():
    """获取账户总览数据。"""
    if not _service:
        return jsonify({'error': '交易服务未初始化'}), 503
    
    try:
        # 获取当前账户数据
        balance = _service.engine.broker.fetch_balance()
        positions = _service.engine.broker.fetch_positions()
        
        total_equity, free_balance, unrealized_pnl = DataEngine.resolve_account_equity(
            balance, positions
        )
        
        # 获取基准净值（第一个快照）
        mode = _service.engine.trading_mode
        first_snapshot = EquitySnapshot.get_first(mode)
        base_equity = float(first_snapshot.total_equity) if first_snapshot else total_equity
        
        # 计算总收益
        total_profit = total_equity - base_equity
        total_profit_pct = (total_profit / base_equity * 100) if base_equity > 0 else 0
        
        # 获取24小时前的净值
        snapshot_24h = EquitySnapshot.get_24h_ago(mode)
        if snapshot_24h:
            equity_24h = float(snapshot_24h.total_equity)
            profit_24h = total_equity - equity_24h
            profit_24h_pct = (profit_24h / equity_24h * 100) if equity_24h > 0 else 0
        else:
            profit_24h = 0
            profit_24h_pct = 0
        
        return jsonify({
            'total_equity': total_equity,
            'free_balance': free_balance,
            'unrealized_pnl': unrealized_pnl,
            'position_count': len(positions) if positions else 0,
            'base_equity': base_equity,
            'total_profit': total_profit,
            'total_profit_pct': total_profit_pct,
            'profit_24h': profit_24h,
            'profit_24h_pct': profit_24h_pct,
            'trading_mode': mode
        })
    except Exception as e:
        logger.error("获取账户总览失败: %s", e)
        return jsonify({'error': str(e)}), 500


@main_bp.route('/api/equity-history')
@_readonly_auth_required
def api_equity_history():
    """获取收益历史数据（用于曲线图）。"""
    try:
        limit = request.args.get('limit', 1000, type=int)
        limit = max(10, min(limit, 5000))
        mode = _service.engine.trading_mode if _service else 'paper'
        snapshots = EquitySnapshot.get_history(limit, mode)
        
        # 获取基准净值
        first_snapshot = EquitySnapshot.get_first(mode)
        base_equity = float(first_snapshot.total_equity) if first_snapshot else 0
        
        data = []
        for s in snapshots:
            equity = float(s.total_equity)
            profit_pct = ((equity - base_equity) / base_equity * 100) if base_equity > 0 else 0
            data.append({
                'timestamp': _format_timestamp(s.timestamp),
                'equity': equity,
                'profit_pct': profit_pct,
                'unrealized_pnl': float(s.unrealized_pnl or 0)
            })
        
        return jsonify({
            'base_equity': base_equity,
            'trading_mode': mode,
            'data': data
        })
    except Exception as e:
        logger.error("获取净值历史失败: %s", e)
        return jsonify({'error': str(e)}), 500


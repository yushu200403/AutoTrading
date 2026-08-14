"""
OpenNOF1 的 Flask 应用程序工厂。

创建并配置包含数据库集成的 Flask 应用。
"""

from flask import Flask, request
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate

# 数据库实例 - 跨模块共享
db = SQLAlchemy()
migrate = Migrate(compare_type=True)


def create_app(config_object=None):
    """
    应用程序工厂。
    
    Args:
        config_object: 要使用的配置类。如果为 None，则自动检测。
        
    Returns:
        配置好的 Flask 应用程序。
    """
    app = Flask(__name__)
    
    # 加载配置
    if config_object is None:
        from config import get_config
        config_object = get_config()

    validate = getattr(config_object, 'validate', None)
    if validate:
        validate()
    
    app.config.from_object(config_object)
    # 控制接口只接收小体量 JSON，限制请求体避免解析前就被撑爆内存。
    # Flask 默认已存在该键且值为 None，因此不能用 setdefault。
    if app.config.get('MAX_CONTENT_LENGTH') is None:
        app.config['MAX_CONTENT_LENGTH'] = 1024 * 1024
    
    # 初始化数据库
    db.init_app(app)
    migrate.init_app(app, db)
    
    from app import models  # noqa: F401
    if app.config.get('AUTO_CREATE_SCHEMA'):
        with app.app_context():
            db.create_all()
    
    # 注册蓝图
    from app.routes import main_bp
    app.register_blueprint(main_bp)

    @app.after_request
    def add_security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault(
            "Permissions-Policy", "camera=(), microphone=(), geolocation=()"
        )
        # 脚本全部本地分发，字体来自 Google Fonts；
        # 模板含少量内联样式，但不放开内联脚本
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; "
            "script-src 'self'; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com; "
            "img-src 'self' data:; "
            "connect-src 'self'; "
            "frame-ancestors 'none'; "
            "base-uri 'none'; "
            "object-src 'none'",
        )
        if request.is_secure:
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        return response
    
    return app

"""生产 WSGI 入口。"""

from app.runtime import create_runtime_app


app = create_runtime_app()

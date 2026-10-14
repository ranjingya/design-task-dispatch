"""Flask Webhook 服务入口。"""

import atexit
import hmac
import logging
import re

from flask import Flask, jsonify, request

from app.config import Settings, load_fields
from app.dispatch import Dispatcher
from app.task_logging import configure_logging

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None, dispatcher=None) -> Flask:
    """功能说明：创建应用。参数 settings：可选运行配置；dispatcher：可选后台处理器。返回值：Flask 应用。"""
    settings = settings or Settings.from_env()
    configure_logging()
    logging.getLogger("httpx").setLevel(logging.WARNING)
    load_fields(settings.fields_path)
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 16 * 1024
    owned = dispatcher is None
    dispatcher = dispatcher or Dispatcher(settings)
    app.extensions["dispatcher"] = dispatcher
    if owned:
        atexit.register(dispatcher.close)

    @app.get("/health")
    def health():
        """返回进程存活状态，不代表飞书或模型已连接成功。"""
        return jsonify(status="ok")

    @app.post("/webhook/task")
    def webhook():
        """功能说明：鉴权并接收任务。参数：无，读取当前 HTTP 请求。返回值：JSON 回执及状态码。"""
        provided = request.headers.get("X-Webhook-Token", "")
        if not hmac.compare_digest(provided.encode(), settings.webhook_token.encode()):
            logger.warning("Webhook 鉴权失败")
            return jsonify(error="unauthorized"), 401
        payload = request.get_json(silent=True)
        record_id = payload.get("record_id") if isinstance(payload, dict) else None
        if not isinstance(record_id, str) or not re.fullmatch(r"rec[A-Za-z0-9]{1,125}", record_id):
            return jsonify(error="record_id 必须是有效的飞书记录 ID"), 400
        try:
            accepted = dispatcher.submit(record_id)
        except (ValueError, RuntimeError):
            logger.exception("任务接收失败：record_id=%s", record_id)
            return jsonify(error="服务配置异常或繁忙，请查看日志后重试"), 503
        return jsonify(status="accepted" if accepted else "duplicate", record_id=record_id), (202 if accepted else 200)

    logger.info("Flask 服务初始化完成")
    return app

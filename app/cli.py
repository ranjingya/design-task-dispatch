"""通过运行中服务的队列手动重试任务。"""

import argparse
import logging
import re

import httpx

from app.config import Settings

logger = logging.getLogger(__name__)


def main(argv=None) -> int:
    """功能说明：提交手动重试并报告接收结果。参数 argv：可选命令行参数列表。返回值：退出码，0表示已接收或重复，1表示请求失败。"""
    parser = argparse.ArgumentParser(description="需求任务维护命令")
    commands = parser.add_subparsers(dest="command", required=True)
    retry = commands.add_parser("retry", help="按记录 ID 提交重试，后台处理结果请查看日志")
    retry.add_argument("record_id", help="飞书需求记录 ID")
    retry.add_argument("--url", default="http://127.0.0.1:8000", help="运行中服务地址，默认容器内本机地址")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"rec[A-Za-z0-9]{1,125}", args.record_id):
        parser.error("record_id 必须是有效的飞书记录 ID")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        settings = Settings.from_env()
        response = httpx.post(args.url.rstrip("/") + "/webhook/task",
                              headers={"X-Webhook-Token": settings.webhook_token},
                              json={"record_id": args.record_id}, timeout=30)
        if response.status_code not in {200, 202}:
            logger.error("重试提交失败：HTTP=%d", response.status_code)
            return 1
        status = response.json().get("status")
        if status == "accepted":
            logger.info("重试已入队，尚未完成；请查看服务日志：record_id=%s", args.record_id)
        elif status == "duplicate":
            logger.info("任务正在处理或已在五分钟内成功处理，未重复入队：record_id=%s", args.record_id)
        else:
            logger.error("服务返回了未知的接收状态")
            return 1
        return 0
    except (httpx.RequestError, ValueError) as exc:
        logger.error("重试提交失败：error_type=%s", type(exc).__name__)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

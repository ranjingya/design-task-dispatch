"""整理需求附件并构造模型图片输入。"""

import base64
import logging

from app.feishu import FeishuClient

logger = logging.getLogger(__name__)


def image_mime(data: bytes) -> str:
    """按文件内容识别支持的图片类型，避免信任附件扩展名。"""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    raise ValueError("仅支持 PNG、JPEG、WebP 图片内容")


def collect_images(attachments: list, record_id: str, field_id: str, feishu: FeishuClient) -> tuple[list, list]:
    """功能说明：下载并整理图片。参数 attachments：附件列表；record_id：需求记录；field_id：附件字段；feishu：飞书客户端。返回值：模型图片片段和待确认事项。"""
    settings = feishu.settings
    if not isinstance(attachments, list):
        raise ValueError("附件字段格式错误")
    images, issues, total = [], [], 0
    for index, attachment in enumerate(attachments, 1):
        try:
            if not isinstance(attachment, dict) or not attachment.get("file_token"):
                raise ValueError("附件信息不完整")
            mime = attachment.get("type", "")
            if mime not in ("image/png", "image/jpeg", "image/webp"):
                raise ValueError("附件格式暂不支持，请提供 PNG、JPEG 或 WebP 图片")
            if len(images) >= settings.max_images:
                raise ValueError("图片数量超过配置上限")
            limit = min(settings.max_image_bytes, settings.max_total_image_bytes - total)
            if limit <= 0 or attachment.get("size", 0) > limit:
                raise ValueError("图片大小超过配置上限")
            data = feishu.download_image(attachment["file_token"], record_id, field_id, limit)
            mime = image_mime(data)
            total += len(data)
            images.append({"type": "image_url", "image_url": {
                "url": f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}",
            }})
            logger.info("附件处理成功：record_id=%s index=%d bytes=%d", record_id, index, len(data))
        except (ValueError, RuntimeError) as exc:
            issues.append(f"附件 {index}：{exc}")
            logger.warning("附件处理需确认：record_id=%s index=%d reason=%s", record_id, index, exc)
    return images, issues

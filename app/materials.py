"""整理需求附件与在线表格，控制材料读取限额。"""

import base64
import logging
import time

from app.feishu import FeishuClient

logger = logging.getLogger(__name__)



class MaterialBundle:
    """一条需求的文本、图片、来源及共享读取限额。"""

    def __init__(self, settings, config):
        self.settings, self.config = settings, config
        self.texts, self.images, self.issues, self.image_sources = [], [], [], []
        self.image_parts = []
        self.text_size = self.image_size = self.file_size = 0

    def issue(self, source, reason):
        value = f"{source}：{reason}"
        if value not in self.issues:
            self.issues.append(value)
            logger.warning("需求材料读取不完整：%s", value)

    def add_text(self, source, value):
        value = str(value).strip()
        if not value:
            return True
        left = self.config["max_text_chars"] - self.text_size - len(source)
        if len(value) > max(left, 0):
            self.issue(source, "文本超过本次读取上限")
        if left <= 0:
            return False
        self.texts.append({"source": source, "text": value[:left]})
        self.text_size += len(source) + len(value[:left])
        return len(value) <= left

    def image_available(self, source):
        if len(self.images) >= self.settings.max_images or self.image_size >= self.settings.max_total_image_bytes:
            self.issue(source, "图片数量或总大小超过上限")
            return False
        return True

    def add_image(self, data, source, *, group=None, context="", requires_detail=False):
        """功能说明：校验并压缩图片后加入模型输入。参数 data：图片字节；source：文件、工作表或页码来源；group：所属工作表或文件；context：附近文字；requires_detail：是否必须查看原图。返回值：无。"""
        import io
        from app.visual_review import detail_images, visual_config
        from PIL import Image, ImageOps

        if not self.image_available(source):
            return
        if len(data) > self.config["max_file_bytes"]:
            self.issue(source, "图片文件过大")
            return
        with Image.open(io.BytesIO(data)) as picture:
            # 巨幅图在解码前拒绝，防止压缩图片占满服务内存。
            if picture.width * picture.height > 40_000_000:
                self.issue(source, "图片像素超过上限")
                return
            picture = ImageOps.exif_transpose(picture).convert("RGBA")
            background = Image.new("RGBA", picture.size, "white")
            picture = Image.alpha_composite(background, picture).convert("RGB")
            parts = detail_images(picture, visual_config(self.config))
            picture.thumbnail((self.config["image_long_edge"],) * 2)
            output = io.BytesIO()
            picture.save(output, format="JPEG", quality=85)
        encoded = output.getvalue()
        part_sizes = [len(base64.b64decode(part["image_url"]["url"].split(",", 1)[1])) for part in parts]
        total_size = len(encoded) + sum(part_sizes)
        if max([len(encoded), *part_sizes]) > self.settings.max_image_bytes or total_size > self.settings.max_total_image_bytes - self.image_size:
            self.issue(source, "图片大小超过上限")
            return
        self.image_size += total_size
        self.images.append({"type": "image_url", "image_url": {
            "url": "data:image/jpeg;base64," + base64.b64encode(encoded).decode("ascii"),
        }})
        self.image_parts.append(parts)
        self.image_sources.append({"image_index": len(self.images), "source": source,
                                   "group": group or source, "context": context[:2000], "requires_detail": requires_detail})


def sheet_token(value, config):
    """校验飞书电子表格链接，只提取令牌，不访问提交的任意网址。"""
    import re
    from urllib.parse import urlsplit

    if isinstance(value, dict):
        value = value.get("link") or value.get("url")
    if not isinstance(value, str):
        raise ValueError("需求文档链接格式无效")
    url = urlsplit(value.strip())
    match = re.fullmatch(r"/sheets/([A-Za-z0-9]+)(?:/)?", url.path)
    if (url.scheme != "https" or url.hostname not in config["allowed_sheet_hosts"] or
            url.username or url.password or url.port not in (None, 443) or not match):
        raise ValueError("请填写允许域名下的飞书电子表格 /sheets/ 链接")
    return match[1]


def collect_materials(attachments, document_link, record_id, field_id, feishu):
    """功能说明：读取附件与在线电子表格供评级。参数 attachments：附件列表；document_link：需求文档链接；record_id：记录 ID；field_id：附件字段 ID；feishu：飞书客户端。返回值：含文本、图片、来源及缺口的材料集合。"""
    import json
    from pathlib import Path
    from app.documents import read_document

    started = time.monotonic()
    path = feishu.settings.materials_path
    logger.info("加载材料配置：%s", path)
    config = json.loads(path.read_text(encoding="utf-8"))
    for key in ("max_files", "max_file_bytes", "max_total_file_bytes", "max_unpacked_bytes", "max_cells", "max_sheets",
                "max_pdf_pages", "max_text_chars", "image_long_edge", "export_attempts", "export_interval_seconds"):
        if type(config.get(key)) is not int or config[key] <= 0:
            raise ValueError(f"材料配置 {key} 必须为正整数")
    if not isinstance(config.get("allowed_sheet_hosts"), list) or type(config.get("skip_hidden_sheets")) is not bool:
        raise ValueError("材料域名或隐藏工作表配置无效")
    from app.visual_review import visual_config
    visual_config(config)
    bundle = MaterialBundle(feishu.settings, config)
    if not isinstance(attachments, list):
        raise ValueError("附件字段格式错误")
    mime_suffix = {"application/pdf": ".pdf", "application/vnd.ms-excel": ".xls",
                   "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx"}
    processed = 0
    for index, item in enumerate(attachments, 1):
        source = f"附件 {index}"
        try:
            if not isinstance(item, dict) or not item.get("file_token"):
                raise ValueError("附件信息不完整")
            name = str(item.get("name", ""))[:200]
            source += f"（{name}）" if name else ""
            mime = item.get("type", "")
            suffix = Path(name).suffix.lower() or mime_suffix.get(mime, "")
            if suffix in (".zip", ".rar", ".7z") or mime in ("application/zip", "application/x-zip-compressed"):
                bundle.add_text(source, "素材压缩包不参与识别，未读取内容")
                logger.info("跳过素材压缩包：record_id=%s index=%d", record_id, index)
                continue
            processed += 1
            if processed > config["max_files"]:
                raise ValueError("附件数量超过上限")
            image_file = mime in ("image/png", "image/jpeg", "image/webp") or suffix in (".png", ".jpg", ".jpeg", ".webp")
            if not image_file and suffix not in (".xlsx", ".xls", ".pdf"):
                raise ValueError("附件格式暂不支持")
            limit = min(config["max_file_bytes"], config["max_total_file_bytes"] - bundle.file_size)
            if limit <= 0 or item.get("size", 0) > limit:
                raise ValueError("附件大小超过上限")
            data = feishu.download_attachment(item["file_token"], record_id, field_id, limit)
            if not isinstance(data, bytes) or len(data) > limit:
                raise ValueError("附件内容无效或超过上限")
            bundle.file_size += len(data)
            if image_file:
                keywords = config["visual_review"]["instruction_keywords"] + config["visual_review"]["image_reference_keywords"]
                bundle.add_image(data, source, group="图片附件", context=name,
                                 requires_detail=any(word in name for word in keywords))
            else:
                read_document(data, suffix, source, bundle)
            logger.info("附件读取完成：record_id=%s index=%d", record_id, index)
        except (ValueError, RuntimeError) as exc:
            bundle.issue(source, str(exc))
        except Exception as exc:
            logger.warning("附件解析异常：record_id=%s index=%d error_type=%s", record_id, index, type(exc).__name__)
            bundle.issue(source, "文件损坏、加密或包含无法解析的内容")
    if document_link:
        source = "需求文档链接"
        try:
            token = sheet_token(document_link, config)
            limit = min(config["max_file_bytes"], config["max_total_file_bytes"] - bundle.file_size)
            if limit <= 0 or processed >= config["max_files"]:
                raise ValueError("本次材料数量或大小已达上限")
            data = feishu.export_sheet(token, limit, config["export_attempts"], config["export_interval_seconds"])
            bundle.file_size += len(data)
            read_document(data, ".xlsx", source, bundle)
        except (ValueError, RuntimeError) as exc:
            bundle.issue(source, str(exc))
        except Exception as exc:
            logger.warning("在线电子表格解析异常：error_type=%s", type(exc).__name__)
            bundle.issue(source, "在线表格无法完整解析")
    logger.info("材料整理完成：record_id=%s text_chars=%d images=%d issues=%d elapsed=%.2fs", record_id, bundle.text_size, len(bundle.images), len(bundle.issues), time.monotonic() - started)
    return bundle

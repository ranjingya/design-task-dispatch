"""整理需求附件与飞书在线资料，控制材料读取限额。"""

import base64
from contextlib import contextmanager
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
        self.skipped_attachments = []
        self._attachment_issues = None
        self.text_size = self.image_size = self.file_size = self.file_count = 0

    def file_limit(self):
        """返回当前文件允许读取的字节数，所有来源共用总限额。"""
        limit = min(self.config['max_file_bytes'], self.config['max_total_file_bytes'] - self.file_size)
        if limit <= 0:
            raise ValueError('本次材料总大小超过上限')
        return limit

    def claim_file(self):
        """登记一个参与解析的文件，超出限额时终止读取。"""
        self.file_count += 1
        if self.file_count > self.config['max_files']:
            raise ValueError('本次材料文件数量超过上限')

    def issue(self, source, reason):
        value = f"{source}：{reason}"
        if self._attachment_issues is not None:
            if value not in self._attachment_issues:
                self._attachment_issues.append(value)
            return
        if value not in self.issues:
            self.issues.append(value)
            logger.warning("需求材料读取不完整：%s", value)

    @contextmanager
    def read_attachment(self, source):
        """功能说明：隔离单个附件读取，失败时排除其内容并继续处理。参数 source：附件来源名称。返回值：附件读取上下文，失败原因写入跳过列表和日志。"""
        collections = (self.texts, self.images, self.image_parts, self.image_sources)
        lengths = [len(values) for values in collections]
        text_size, image_size = self.text_size, self.image_size
        previous_issues = self._attachment_issues
        issues = self._attachment_issues = []
        try:
            yield
            if issues:
                raise ValueError("；".join(issues))
        except Exception as exc:
            # 排除失败附件的部分解析结果；已发生的下载仍占用文件读取限额。
            for values, length in zip(collections, lengths):
                del values[length:]
            self.text_size, self.image_size = text_size, image_size
            reason = str(exc) if isinstance(exc, (ValueError, RuntimeError)) else f"文件无法解析（{type(exc).__name__}）"
            self.skipped_attachments.append({"source": source, "reason": reason})
            logger.warning("跳过附件 来源=%s 原因=%s", source, reason)
        finally:
            self._attachment_issues = previous_issues

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


def collect_materials(attachment_fields, document_link, record_id, feishu):
    """功能说明：读取多列附件与飞书在线资料供评级，共用材料限额。参数 attachment_fields：附件字段 ID 到该列附件列表的映射；document_link：需求文档链接；record_id：记录 ID；feishu：飞书客户端。返回值：含文本、图片、来源及缺口的材料集合。"""
    import json
    from pathlib import Path
    from app.documents import read_document

    started = time.monotonic()
    path = feishu.settings.materials_path
    logger.debug("加载材料配置：%s", path)
    config = json.loads(path.read_text(encoding="utf-8"))
    for key in ("max_files", "max_file_bytes", "max_total_file_bytes", "max_unpacked_bytes", "max_cells", "max_sheets",
                "max_pdf_pages", "max_text_chars", "image_long_edge", "export_attempts", "export_interval_seconds",
                "max_online_resources", "max_embed_depth", "max_doc_blocks", "max_base_fields", "max_base_records", "word_conversion_timeout"):
        if type(config.get(key)) is not int or config[key] <= 0:
            raise ValueError(f"材料配置 {key} 必须为正整数")
    if not isinstance(config.get("word_converter"), str) or not config["word_converter"].strip():
        raise ValueError("Word 转换器配置无效")
    if not isinstance(config.get("allowed_sheet_hosts"), list) or type(config.get("skip_hidden_sheets")) is not bool:
        raise ValueError("材料域名或隐藏工作表配置无效")
    from app.visual_review import visual_config
    visual_config(config)
    bundle = MaterialBundle(feishu.settings, config)
    if not isinstance(attachment_fields, dict) or any(not isinstance(items, list) for items in attachment_fields.values()):
        raise ValueError("附件字段格式错误")
    attachments = [(field_id, item) for field_id, items in attachment_fields.items() for item in items]
    mime_suffix = {"application/pdf": ".pdf", "application/vnd.ms-excel": ".xls",
                   "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
                   "application/msword": ".doc",
                   "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx"}
    for index, (field_id, item) in enumerate(attachments, 1):
        source = f"附件 {index}"
        name = str(item.get("name", ""))[:200] if isinstance(item, dict) else ""
        source += f"（{name}）" if name else ""
        with bundle.read_attachment(source):
            if not isinstance(item, dict) or not item.get("file_token"):
                raise ValueError("附件信息不完整")
            mime = item.get("type", "")
            suffix = Path(name).suffix.lower() or mime_suffix.get(mime, "")
            if suffix in (".zip", ".rar", ".7z") or mime in ("application/zip", "application/x-zip-compressed"):
                bundle.add_text(source, "素材压缩包不参与识别，未读取内容")
                logger.debug("跳过素材压缩包：record_id=%s index=%d", record_id, index)
                continue
            image_file = mime in ("image/png", "image/jpeg", "image/webp") or suffix in (".png", ".jpg", ".jpeg", ".webp")
            if not image_file and suffix not in (".xlsx", ".xls", ".pdf", ".docx", ".doc"):
                raise ValueError("附件格式暂不支持")
            bundle.claim_file()
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
            logger.debug("附件读取完成：record_id=%s index=%d", record_id, index)
    if document_link:
        from app.online_materials import OnlineReader, document_links
        reader = OnlineReader(feishu, bundle)
        try:
            links = document_links(document_link)
        except ValueError as exc:
            bundle.issue('需求文档链接', str(exc))
            links = []
        for index, link in enumerate(links, 1):
            source = f'需求文档链接 {index}'
            if index > config['max_online_resources']:
                bundle.issue(source, '在线资料数量超过上限')
                break
            try:
                reader.read_link(link, source)
            except (ValueError, RuntimeError) as exc:
                bundle.issue(source, str(exc))
            except Exception as exc:
                logger.warning('在线资料解析异常 error_type=%s', type(exc).__name__)
                bundle.issue(source, '在线资料无法完整解析')
    logger.debug("材料整理完成：record_id=%s text_chars=%d images=%d skipped=%d issues=%d elapsed=%.2fs", record_id, bundle.text_size, len(bundle.images), len(bundle.skipped_attachments), len(bundle.issues), time.monotonic() - started)
    return bundle

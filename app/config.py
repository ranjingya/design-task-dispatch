"""环境配置与容器外字段映射。"""

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

logger = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    """运行配置；凭证不参与对象文本展示。"""

    app_id: str
    app_secret: str = field(repr=False)
    base_token: str
    table_id: str
    webhook_token: str = field(repr=False)
    fields_path: Path
    rules_table_id: str = ""
    ai_base_url: str = "https://api.deepseek.com"
    ai_key: str = field(default="", repr=False)
    ai_model: str = ""
    prompt_path: Path = ROOT / "prompts/classify.md"
    rule_fields_path: Path = ROOT / "config/rule_fields.json"
    rules_cache_path: Path = ROOT / ".cache/rules.json"
    ai_timeout: float = 90
    ai_attempts: int = 3
    max_images: int = 8
    max_image_bytes: int = 10 * 1024 * 1024
    max_total_image_bytes: int = 20 * 1024 * 1024

    @classmethod
    def from_env(cls):
        """功能说明：加载并校验环境配置。参数 cls：配置类。返回值：配置实例。"""
        load_dotenv(ROOT / ".env", override=False)
        names = (
            "FEISHU_DATA_APP_ID", "FEISHU_DATA_APP_SECRET",
            "FEISHU_BASE_TOKEN", "FEISHU_TABLE_ID", "WEBHOOK_TOKEN",
        )
        values = {name: os.getenv(name, "").strip() for name in names}
        missing = [name for name, value in values.items() if not value]
        if missing:
            raise ValueError("缺少环境变量：" + ", ".join(missing))
        path = Path(os.getenv("FIELD_MAPPING_PATH", str(ROOT / "config/fields.json")))
        return cls(
            *(values[name] for name in names), fields_path=path,
            rules_table_id=os.getenv("FEISHU_RULES_TABLE_ID", "").strip(),
            ai_base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").strip().rstrip("/"),
            ai_key=os.getenv("DEEPSEEK_API_KEY", "").strip(),
            ai_model=os.getenv("DEEPSEEK_MODEL", "").strip(),
            prompt_path=Path(os.getenv("CLASSIFY_PROMPT_PATH", str(ROOT / "prompts/classify.md"))),
            rule_fields_path=Path(os.getenv("RULE_FIELD_MAPPING_PATH", str(ROOT / "config/rule_fields.json"))),
            rules_cache_path=Path(os.getenv("RULES_CACHE_PATH", str(ROOT / ".cache/rules.json"))),
            ai_timeout=float(os.getenv("AI_TIMEOUT_SECONDS", "90")),
            ai_attempts=int(os.getenv("AI_MAX_ATTEMPTS", "3")),
            max_images=int(os.getenv("MAX_IMAGES", "8")),
            max_image_bytes=int(os.getenv("MAX_IMAGE_BYTES", str(10 * 1024 * 1024))),
            max_total_image_bytes=int(os.getenv("MAX_TOTAL_IMAGE_BYTES", str(20 * 1024 * 1024))),
        )

    def validate_ai(self) -> None:
        """校验评级所需配置，错误仅包含配置项名称。"""
        from urllib.parse import urlsplit

        if not self.rules_table_id or not self.ai_key or not self.ai_model:
            raise ValueError("请配置 FEISHU_RULES_TABLE_ID、DEEPSEEK_API_KEY 和 DEEPSEEK_MODEL")
        url = urlsplit(self.ai_base_url)
        if url.scheme != "https" or not url.netloc or url.username or url.password or url.query or url.fragment:
            raise ValueError("DEEPSEEK_BASE_URL 必须是无凭证、查询参数的 HTTPS 接口地址")
        if not 0 < self.ai_timeout <= 300 or not 1 <= self.ai_attempts <= 3:
            raise ValueError("AI 超时需在 0 至 300 秒之间，总尝试次数需在 1 至 3 之间")
        if min(self.max_images, self.max_image_bytes, self.max_total_image_bytes) <= 0:
            raise ValueError("图片数量和大小限制必须为正数")


def load_fields(path: Path) -> dict[str, str]:
    """功能说明：读取最新字段映射。参数 path：JSON 文件路径。返回值：内部名称到飞书字段名的映射。"""
    logger.info("加载字段配置：%s", path)
    try:
        mapping = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("字段配置不可读取或 JSON 格式错误") from exc
    if not isinstance(mapping, dict) or not mapping:
        raise ValueError("字段配置必须为非空 JSON 对象")
    if any(not isinstance(v, str) or not v.strip() for v in mapping.values()):
        raise ValueError("每个字段映射值必须为非空字符串")
    if not {"title", "description"} <= mapping.keys():
        raise ValueError("字段配置必须包含 title 和 description")
    if len(set(mapping.values())) != len(mapping):
        raise ValueError("字段映射不能重复引用同一个飞书字段")
    return mapping

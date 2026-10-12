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
        return cls(*(values[name] for name in names), fields_path=path)


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

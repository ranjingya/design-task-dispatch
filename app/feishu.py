"""飞书应用身份认证及多维表记录读写。"""

import logging
import threading
import time
from urllib.parse import quote

import httpx

from app.config import Settings

logger = logging.getLogger(__name__)
API = "https://open.feishu.cn/open-apis"


class FeishuClient:
    def __init__(self, settings: Settings):
        """功能说明：初始化飞书网关。参数 settings：应用及目标表配置。返回值：无。"""
        self.settings = settings
        self._token = ""
        self._expires_at = 0.0
        self._lock = threading.Lock()

    @staticmethod
    def _request(method: str, path: str, **kwargs) -> dict:
        """发送请求，只记录状态码和错误码，避免输出凭证及业务正文。"""
        try:
            response = httpx.request(method, API + path, timeout=30, **kwargs)
        except httpx.RequestError:
            raise RuntimeError("飞书网络请求失败") from None
        if not response.is_success:
            raise RuntimeError(f"飞书 HTTP 请求失败：{response.status_code}")
        try:
            data = response.json()
        except ValueError:
            raise RuntimeError("飞书返回了非 JSON 响应") from None
        if not isinstance(data, dict) or data.get("code") != 0:
            code = data.get("code") if isinstance(data, dict) else "unknown"
            raise RuntimeError(f"飞书业务请求失败：code={code}")
        return data

    def _access_token(self) -> str:
        """缓存应用访问凭证，提前一分钟刷新。"""
        with self._lock:
            if time.monotonic() >= self._expires_at:
                logger.info("刷新飞书应用访问凭证")
                data = self._request("POST", "/auth/v3/tenant_access_token/internal", json={
                    "app_id": self.settings.app_id,
                    "app_secret": self.settings.app_secret,
                })
                self._token = data["tenant_access_token"]
                self._expires_at = time.monotonic() + max(0, data["expire"] - 60)
            return self._token

    def _record_path(self, record_id: str) -> str:
        parts = [quote(v, safe="") for v in (
            self.settings.base_token, self.settings.table_id, record_id,
        )]
        return f"/bitable/v1/apps/{parts[0]}/tables/{parts[1]}/records/{parts[2]}"

    def get_record(self, record_id: str) -> dict:
        """功能说明：获取需求记录。参数 record_id：飞书记录 ID。返回值：记录字段字典。"""
        logger.info("读取飞书记录：record_id=%s", record_id)
        data = self._request("GET", self._record_path(record_id),
                             headers={"Authorization": f"Bearer {self._access_token()}"})
        return data["data"]["record"]["fields"]

    def update_record(self, record_id: str, fields: dict) -> None:
        """功能说明：回写记录。参数 record_id：记录 ID；fields：飞书字段名与字段值。返回值：无。"""
        logger.info("回写飞书记录：record_id=%s field_count=%d", record_id, len(fields))
        self._request("PUT", self._record_path(record_id), json={"fields": fields},
                      headers={"Authorization": f"Bearer {self._access_token()}"})

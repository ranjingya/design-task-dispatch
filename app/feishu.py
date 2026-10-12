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

    def _list_all(self, table_id: str, resource: str) -> list[dict]:
        """功能说明：完整读取分页资源。参数 table_id：表 ID；resource：fields 或 records。返回值：全部项目列表。"""
        path = f"/bitable/v1/apps/{quote(self.settings.base_token, safe='')}/tables/{quote(table_id, safe='')}/{resource}"
        items, seen = [], set()
        params = {"page_size": 100}
        while True:
            data = self._request("GET", path, params=params,
                                 headers={"Authorization": f"Bearer {self._access_token()}"})["data"]
            items.extend(data.get("items", []))
            if not data.get("has_more"):
                return items
            token = data.get("page_token")
            if not token or token in seen:
                raise RuntimeError("飞书分页游标异常，无法确认资源完整")
            seen.add(token)
            params["page_token"] = token

    def get_fields(self) -> list[dict]:
        """读取需求表字段结构及单选选项。"""
        return self._list_all(self.settings.table_id, "fields")

    def get_rules(self) -> list[dict]:
        """读取规则表全部记录，保留真实规则 ID 和字段内容。"""
        rules = self._list_all(self.settings.rules_table_id, "records")
        rules = [{"record_id": item["record_id"], "fields": item["fields"]}
                 for item in rules if item.get("fields")]
        if not rules:
            raise ValueError("规则表为空，无法评级")
        logger.info("规则加载完成：count=%d", len(rules))
        return rules

    def download_image(self, file_token: str, record_id: str, field_id: str, limit: int) -> bytes:
        """功能说明：限量下载飞书素材。参数 file_token：素材令牌；record_id：所属记录；field_id：附件字段；limit：字节上限。返回值：文件字节。"""
        import json

        extra = {"bitablePerm": {"tableId": self.settings.table_id,
                                 "attachments": {field_id: {record_id: [file_token]}}}}
        url = API + f"/drive/v1/medias/{quote(file_token, safe='')}/download"
        try:
            with httpx.stream("GET", url, timeout=30, follow_redirects=True,
                              params={"extra": json.dumps(extra)},
                              headers={"Authorization": f"Bearer {self._access_token()}"}) as response:
                if not response.is_success:
                    raise RuntimeError(f"飞书附件下载失败：HTTP {response.status_code}")
                chunks, size = [], 0
                for chunk in response.iter_bytes(65536):
                    size += len(chunk)
                    if size > limit:
                        raise ValueError("图片超过大小限制")
                    chunks.append(chunk)
                if not size:
                    raise ValueError("图片为空")
                return b"".join(chunks)
        except httpx.RequestError:
            raise RuntimeError("飞书附件下载网络异常") from None

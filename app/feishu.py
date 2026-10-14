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
        self._contact_cache: dict[tuple[str, str], tuple[float, dict]] = {}

    @staticmethod
    def _request(method: str, path: str, **kwargs) -> dict:
        """功能说明：调用飞书接口并重试瞬时故障。参数 method：HTTP方法；path：接口路径；kwargs：请求参数。返回值：成功的JSON响应。"""
        # 查询、幂等字段回写和取凭证可重试；创建类请求不自动重放。
        attempts = 3 if method.upper() in {"GET", "PUT"} or path == "/auth/v3/tenant_access_token/internal" else 1
        for attempt in range(1, attempts + 1):
            retryable = False
            try:
                response = httpx.request(method, API + path, timeout=30, **kwargs)
            except httpx.RequestError as exc:
                error = f"飞书网络请求失败：{type(exc).__name__}"
                retryable = isinstance(exc, (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError))
            else:
                if response.is_success:
                    try:
                        data = response.json()
                    except ValueError:
                        raise RuntimeError("飞书返回了非 JSON 响应") from None
                    if not isinstance(data, dict) or data.get("code") != 0:
                        code = data.get("code") if isinstance(data, dict) else "unknown"
                        raise RuntimeError(f"飞书业务请求失败：code={code}")
                    if attempt > 1:
                        logger.info("飞书请求重试成功：method=%s attempt=%d", method, attempt)
                    return data
                error = f"飞书 HTTP 请求失败：{response.status_code}"
                retryable = response.status_code in {429, 500, 502, 503, 504}
            if not retryable or attempt == attempts:
                logger.error("飞书请求终止：method=%s attempt=%d error=%s", method, attempt, error)
                raise RuntimeError(error) from None
            delay = 2 ** (attempt - 1)
            logger.warning("飞书请求暂时失败，准备重试：method=%s attempt=%d/%d delay=%ds error=%s",
                           method, attempt, attempts, delay, error)
            time.sleep(delay)

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

    def _get_contact(self, resource: str, identity: str, key: str) -> dict:
        """读取通讯录资源，成功响应在进程内缓存一天，失败不缓存。"""
        now = time.monotonic()
        self._contact_cache = {k: v for k, v in self._contact_cache.items() if v[0] > now}
        cache_key = (resource, identity)
        if cache_key in self._contact_cache:
            logger.info("使用通讯录缓存：resource=%s", resource)
            return self._contact_cache[cache_key][1]
        logger.info("加载通讯录资源：resource=%s", resource)
        data = self._request("GET", f"/contact/v3/{resource}/{quote(identity, safe='')}",
                             params={"user_id_type": "open_id", "department_id_type": "open_department_id"},
                             headers={"Authorization": f"Bearer {self._access_token()}"})["data"][key]
        required = ("department_ids",) if resource == "users" else ("name", "parent_department_id")
        if all(field in data for field in required):
            self._contact_cache[cache_key] = (now + 86400, data)
        return data

    def get_contact_user(self, user_id: str) -> dict:
        """功能说明：查询人员所属部门。参数 user_id：当前应用下的人员 open_id。返回值：人员信息。"""
        return self._get_contact("users", user_id, "user")

    def get_contact_department(self, department_id: str) -> dict:
        """功能说明：查询部门及上级节点。参数 department_id：部门 open_department_id。返回值：部门信息。"""
        return self._get_contact("departments", department_id, "department")

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

    def _list_all(self, table_id: str, resource: str, base_token: str | None = None) -> list[dict]:
        """功能说明：完整读取分页资源。参数 table_id：表 ID；resource：fields 或 records；base_token：可选来源 Base。返回值：全部项目列表。"""
        path = f"/bitable/v1/apps/{quote(base_token or self.settings.base_token, safe='')}/tables/{quote(table_id, safe='')}/{resource}"
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

    def get_records(self, table_id: str, base_token: str | None = None) -> list[dict]:
        """功能说明：读取完整业务记录。参数 table_id：来源表；base_token：可选跨 Base 来源。返回值：含记录 ID 和字段的列表。"""
        records = self._list_all(table_id, "records", base_token)
        logger.info("业务记录加载完成：table_id=%s count=%d", table_id, len(records))
        return records

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

    def download_attachment(self, file_token: str, record_id: str, field_id: str, limit: int) -> bytes:
        """功能说明：限量下载飞书素材。参数 file_token：素材令牌；record_id：所属记录；field_id：附件字段；limit：字节上限。返回值：文件字节。"""
        import json

        extra = {"bitablePerm": {"tableId": self.settings.table_id,
                                 "attachments": {field_id: {record_id: [file_token]}}}}
        url = API + f"/drive/v1/medias/{quote(file_token, safe='')}/download"
        return self._download(url, limit, {"extra": json.dumps(extra)})

    def _download(self, url: str, limit: int, params: dict | None = None) -> bytes:
        """流式下载官方接口返回的文件，并限制实际接收字节。"""
        try:
            with httpx.stream("GET", url, timeout=30, follow_redirects=True,
                              params=params,
                              headers={"Authorization": f"Bearer {self._access_token()}"}) as response:
                if not response.is_success:
                    raise RuntimeError(f"飞书文件下载失败：HTTP {response.status_code}")
                chunks, size = [], 0
                for chunk in response.iter_bytes(65536):
                    size += len(chunk)
                    if size > limit:
                        raise ValueError("文件超过大小限制")
                    chunks.append(chunk)
                if not size:
                    raise ValueError("文件为空")
                return b"".join(chunks)
        except httpx.RequestError:
            raise RuntimeError("飞书文件下载网络异常") from None

    def export_sheet(self, token: str, limit: int, attempts: int, interval: int) -> bytes:
        """功能说明：导出在线电子表格为 Excel。参数 token：电子表格令牌；limit：文件字节上限；attempts：最多查询次数；interval：查询间隔秒数。返回值：Excel 文件字节。"""
        logger.info("开始导出飞书电子表格")
        headers = {"Authorization": f"Bearer {self._access_token()}"}
        ticket = self._request("POST", "/drive/v1/export_tasks", headers=headers,
                               json={"token": token, "type": "sheet", "file_extension": "xlsx"})["data"]["ticket"]
        for attempt in range(attempts):
            result = self._request("GET", f"/drive/v1/export_tasks/{quote(ticket, safe='')}",
                                   headers=headers, params={"token": token})["data"]["result"]
            status = result.get("job_status")
            if status == 0:
                if int(result.get("file_size", 0)) > limit:
                    raise ValueError("在线电子表格导出文件超过大小上限")
                path = "/drive/v1/export_tasks/file/" + quote(result["file_token"], safe="") + "/download"
                data = self._download(API + path, limit)
                logger.info("电子表格导出完成：bytes=%d", len(data))
                return data
            if status not in (1, 2):
                raise RuntimeError(f"电子表格导出失败：状态 {status}，请检查应用的文档访问与导出权限")
            if attempt + 1 < attempts:
                time.sleep(interval)
        raise RuntimeError("电子表格导出超时，请稍后重试")

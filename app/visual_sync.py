"""视觉共享群消息解析、数据包归属匹配与同步。"""

import argparse
import json
import logging
import multiprocessing
import re
import sqlite3
import threading
import time
import uuid
from collections import Counter
from dataclasses import replace
from datetime import datetime
import unicodedata
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from app.config import ROOT, Settings
from app.feishu import FeishuClient
from app.notifications import alert_open_id, escape_text, send_error
from app.recommendation import product_owner
from app.rules import plain

logger = logging.getLogger(__name__)
ZONE = ZoneInfo("Asia/Shanghai")
NOTIFY_MAX_ATTEMPTS = 3
NOTIFY_RETRY_SECONDS = 30


def listen_visual_events(settings, on_message, on_connected):
    """通过服务端 SDK 订阅群消息。参数 settings：飞书应用和目标群配置；on_message：接收目标群消息 ID 的回调；on_connected：首次连接或重连成功后的补读回调。返回值：无，持续监听。"""
    import lark_oapi as lark

    def received(data):
        """筛选目标群的人员消息，并通知读取线程。"""
        event = data.event
        if event.message.chat_id == settings.visual_sync_chat_id and event.sender.sender_type == "user":
            on_message(event.message.message_id)

    handler = lark.EventDispatcherHandler.builder("", "").register_p2_im_message_receive_v1(received).build()
    class VisualEventClient(lark.ws.Client):
        async def _connect(self):
            """握手成功后触发补读，覆盖 SDK 的首次连接与内部自动重连。"""
            await super()._connect()
            on_connected()

    client = VisualEventClient(settings.app_id, settings.app_secret, event_handler=handler,
                               log_level=lark.LogLevel.WARNING)
    client.start()


def _event_worker(settings, wake, stop):
    """在独立进程运行阻塞式 SDK。参数 settings：应用配置；wake：唤醒同步信号；stop：退出信号。返回值：无。"""
    logging.basicConfig(level=logging.INFO)

    def notify(message_id):
        """记录事件并唤醒处理线程。"""
        logger.info("收到视觉群消息事件 %s", message_id)
        wake.set()

    def connected():
        """连接就绪后唤醒读取线程，补回连接建立之前的消息。"""
        logger.info("视觉群长连接已就绪，准备补读")
        wake.set()

    incident = str(uuid.uuid4())
    while not stop.is_set():
        try:
            listen_visual_events(settings, notify, connected)
            incident = str(uuid.uuid4())
        except Exception as exc:
            send_error("", "群消息长连接", exc, FeishuClient(settings), "视觉共享群监听", visual=True, incident=incident)
            logger.warning("视觉群长连接中断（%s），30 秒后重连", type(exc).__name__)
        stop.wait(30)


def message_link(message):
    """根据飞书返回的链接或群聊、话题中的消息位置生成原消息链接。"""
    if message.get("message_app_link"):
        return message["message_app_link"]
    chat = message.get("chat_id")
    if not chat:
        return None
    thread = message.get("thread_id")
    position = message.get("thread_message_position")
    if thread and str(position).isdecimal():
        params = dict(openchatid=chat, openthreadid=thread, open_chat_id=chat,
                      open_thread_id=thread, thread_position=position)
        return "https://applink.feishu.cn/client/thread/open?" + urlencode(params)
    position = message.get("message_position")
    if str(position).isdecimal():
        return "https://applink.feishu.cn/client/chat/open?" + urlencode(dict(openChatId=chat, position=position))
    return None


def message_text(message, *, include_mentions=True):
    """功能说明：还原消息的可读正文。参数 message：群消息；include_mentions：是否还原人员提及。返回值：消息文字、链接和原有分段。"""
    body = json.loads(message["body"]["content"])
    mentions = {item["key"]: ("@" + item["name"] if include_mentions else "") for item in message.get("mentions", [])}
    if message["msg_type"] == "text":
        return re.sub(r"@_user_\d+", lambda m: mentions.get(m[0], m[0]), body["text"])
    lines = [body["title"]] if body.get("title") else []
    for paragraph in body.get("content", body.get("content_v2", [])):
        pieces = []
        for node in paragraph:
            if node["tag"] == "at":
                if include_mentions:
                    pieces.append(mentions.get(node.get("user_id"), "@" + node.get("user_name", "")))
            elif node["tag"] == "a" and node.get("href") != node.get("text"):
                pieces.append(f"{node.get('text', '')} {node.get('href', '')}")
            else:
                pieces.append(node.get("text", ""))
        lines.append("".join(pieces))
    return "\n".join(lines).strip("\n")


def message_image_keys(message):
    """按群消息原始顺序读取图片键，并合并同一张图片的重复引用。"""
    body = json.loads(message["body"]["content"])
    paragraphs = body.get("content", body.get("content_v2", [])) if message["msg_type"] == "post" else []
    return list(dict.fromkeys(node["image_key"] for paragraph in paragraphs for node in paragraph
                              if node.get("tag") == "img" and node.get("image_key")))


def demand_type(text, config):
    """根据本款发布说明判定上新或优化。"""
    if re.search(config["negative_pattern"], text):
        return ""
    if re.search(config["change_pattern"], text):
        return "优化"
    if re.search(config["new_pattern"], text):
        return "上新"
    return ""


def products(text, config):
    """功能说明：按商品段落提取独立款及组合。参数 text：消息正文；config：匹配规则。返回值：货号、品名及需求类型序列。"""
    sections = []
    header = []
    pattern = re.compile(config["code_pattern"])
    lines = []
    for line in text.splitlines():
        line = re.split(r"[A-Za-z]:[\\/]|\\\\|https?://|@", line, maxsplit=1)[0].strip()
        if not line:
            continue
        positions = list(pattern.finditer(line))
        start = 0
        for previous, following in zip(positions, positions[1:]):
            between = line[previous.end():following.start()].strip(" -、，,；;")
            if between and "+" not in between and "＋" not in between:
                lines.append(line[start:following.start()])
                start = following.start()
        lines.append(line[start:])
    for line in lines:
        codes = list(dict.fromkeys(x.upper() for x in pattern.findall(line)))
        if codes:
            title = pattern.sub("", line).strip(" -、+，:：")
            # 商品标题和操作说明常在同一行，以空白、业务动作或括注分隔。
            cut = re.search(r"\s{2,}|[（(](?:公司级别|检测报告未出)|数据包|(?:主图|详情页?|模特图|文案|充绒量|数据成分|检测报告)(?=.{0,12}(?:" + config["change_pattern"] + r"))|"
                            + config["new_pattern"] + "|" + config["change_pattern"], title)
            name = (title[:cut.start()] if cut else title).strip(" -、+，:：\\（(")
            sections.append((codes, name, [line]))
        elif sections:
            sections[-1][2].append(line)
        else:
            header.append(line)
    seen = set()
    for codes, name, lines in sections:
        key = (" ".join(codes), name)
        if key not in seen:
            seen.add(key)
            kind = demand_type("\n".join(lines), config) or demand_type("\n".join(header), config)
            yield *key, kind


class VisualSync:
    """共用正式同步和历史预览的消息处理器。"""

    def __init__(self, client):
        self.client = client
        self.bot = None
        self.chat_id = client.settings.visual_sync_chat_id
        if not self.chat_id.startswith("oc_"):
            raise ValueError("请在 .env 中设置 VISUAL_SYNC_CHAT_ID，填写 oc_ 开头的飞书群 ID")
        self.config = json.loads((ROOT / "config/visual_sync.json").read_text())
        recommendation = json.loads((ROOT / "config/recommendation.json").read_text())
        self.product = dict(recommendation["product"], code_pattern=self.config["code_pattern"])
        self.members = recommendation["members"]
        self.owner_routes = self.config.get("owner_routes", {})
        for owner, pool in self.owner_routes.items():
            if (not isinstance(pool, list) or not pool or any(n not in self.members for n in pool)
                    or len(pool) != len(set(pool)) or set(pool).intersection(recommendation["excluded_owners"])):
                raise ValueError(f"视觉群承接人员配置无效：{owner}")
        self.catalog = None

    def resolve(self, codes, name):
        """功能说明：按完整货号组合和品名版本查认领人。参数 codes：货号串；name：通知品名。返回值：品名与原认领人姓名，未匹配返回空。"""
        pattern = re.compile(self.config["code_pattern"], re.I)
        target = set(codes.split())
        if self.catalog is None:
            self.catalog = []
            try:
                for source in self.product["sources"]:
                    table = getattr(self.client.settings, source["table_setting"])
                    fields = [self.product["title_field"], self.product["owner_field"]]
                    self.catalog.extend(self.client.get_records(table, self.client.settings.product_base_token, fields))
            except Exception:
                self.catalog = None
                raise
            logger.info("年度数据包归属已加载，共 %d 条", len(self.catalog))

        def normalized(value):
            """统一全半角、空白和版本括注，用于对照两个来源的商品名称。"""
            return "".join(ch for ch in unicodedata.normalize("NFKC", value).replace("版)", ")") if ch.isalnum())

        candidates, named = [], []
        for record in self.catalog:
            title = plain(record["fields"].get(self.product["title_field"]))
            if set(pattern.findall(title.upper())) != target:
                continue
            candidates.append(record)
            catalog_name = pattern.sub("", title).strip(" -、+，")
            key = normalized(catalog_name)
            if key and normalized(name).startswith(key):
                named.append((len(key), record, catalog_name))
        if named:
            length = max(item[0] for item in named)
            candidates = [record for size, record, _ in named if size == length]
            best = next(title for size, _, title in named if size == length)
            # 源品名后紧跟操作描述时截断；保留消息明确写出的版本括注。
            if "(" not in name and "（" not in name:
                name = " ".join(best.split())
        owner, reason, _, _ = product_owner({"title": codes, "platform": self.config["platform"]},
                                            candidates, self.product)
        if owner not in self.members and owner not in self.owner_routes:
            logger.info("跳过款号 %s：%s", codes, reason)
            return name, None
        if not name and candidates:
            name = pattern.sub("", plain(candidates[0]["fields"][self.product["title_field"]])).strip(" -")
        return name, owner

    def plan(self, message):
        """功能说明：提取待登记款式。参数 message：一条真实群消息。返回值：已匹配的原认领人、需求字段组成的列表。"""
        cfg = self.config
        mentions = {x["id"] for x in message.get("mentions", [])}
        sender = message.get("sender", {})
        if (message.get("deleted") or sender.get("sender_type") != "user"
                or message["msg_type"] not in {"text", "post"}
                or not mentions.intersection((cfg["always_mention"], cfg["conditional_mention"]))):
            return []
        body = message_text(message)
        date = datetime.fromtimestamp(int(message["create_time"]) / 1000, ZONE)
        date = date.replace(hour=0, minute=0, second=0, microsecond=0)
        rows = []
        link = message_link(message)
        for codes, name, kind in products(message_text(message, include_mentions=False), cfg):
            if not kind and cfg["always_mention"] not in mentions:
                continue
            name, owner = self.resolve(codes, name)
            if not owner:
                continue
            values = dict(code=codes, name=name, body=body, platform=cfg["platform"],
                          sender=[{"id": sender["id"]}],
                          date=int(date.timestamp() * 1000))
            if link:
                values["source_link"] = {"text": "查看原消息", "link": link}
            if kind:
                values["kind"] = kind
            rows.append((owner, {cfg["fields"][k]: v for k, v in values.items()}))
        return rows

    def queue_message(self, db, message):
        """功能说明：按款式去重并确定承接人，与读取进度一并提交。参数 db：同步状态库；message：群消息。返回值：本次新增任务的完整字段列表。"""
        fields = self.config["fields"]
        rows = []
        for owner, row in self.plan(message):
            source = json.dumps([message["message_id"], row[fields["code"]], row[fields["name"]]])
            pool = self.owner_routes.get(owner, [owner])
            # 先取得该款的唯一登记位置。重复事件和并行读取都不会多占轮次。
            slot = db.execute(
                "INSERT INTO jobs (id, source, notification_body) VALUES (?,?,?) "
                "ON CONFLICT(source) DO NOTHING RETURNING rowid",
                (str(uuid.uuid4()), source, message_text(message, include_mentions=False)),
            ).fetchone()
            if slot is None:
                continue
            assignee = pool[0]
            if len(pool) > 1:
                previous = db.execute("SELECT last_member FROM owner_rotation WHERE owner=?", (owner,)).fetchone()
                if previous and previous[0] in pool:
                    assignee = pool[(pool.index(previous[0]) + 1) % len(pool)]
                db.execute("INSERT INTO owner_rotation(owner, last_member) VALUES (?,?) "
                           "ON CONFLICT(owner) DO UPDATE SET last_member=excluded.last_member", (owner, assignee))
            row[fields["owner"]] = [{"id": self.members[assignee]}]
            db.execute("UPDATE jobs SET payload=? WHERE rowid=?", (json.dumps(row, ensure_ascii=False), slot[0]))
            if owner != assignee:
                logger.info("视觉需求承接 款号=%s 原认领人=%s 负责人=%s", row[fields["code"]], owner, assignee)
            rows.append(row)
        if rows:
            self.register_images(db, message)
        return rows

    def messages(self, start=None, end=None, limit=None):
        """功能说明：按时间分页读取群消息。参数 start、end：起止毫秒；limit：历史预览上限。返回值：消息迭代器。"""
        params = dict(container_id_type="chat", container_id=self.chat_id,
                      page_size=min(limit or 50, 50), sort_type="ByCreateTimeDesc" if limit else "ByCreateTimeAsc")
        if start is not None:
            params.update(start_time=str(start // 1000), end_time=str((end + 999) // 1000))
        count = 0
        cursors = set()
        while True:
            data = self.client.material_get("/im/v1/messages", params)
            for message in data["items"]:
                if start is None or start <= int(message["create_time"]) < end:
                    yield message
                    count += 1
                    if limit and count >= limit:
                        return
            if not data.get("has_more"):
                return
            token = data.get("page_token")
            if not token or token in cursors:
                raise RuntimeError("群历史消息分页异常")
            cursors.add(token)
            params["page_token"] = token

    def reminder_card(self, row, record, owner_id, body):
        """生成视觉需求私聊卡片。参数 row：已登记字段；record：记录 ID，空值链接到需求表；owner_id：通知应用下的负责人 Open ID；body：去掉人员提及的原文。返回值：飞书卡片。"""
        fields = self.config["fields"]
        code, name = row[fields["code"]], row[fields["name"]]
        kind = row.get(fields["kind"], "")
        designer = alert_open_id(row[fields["sender"]][0]["id"], self.client, self.bot)
        lines = [f"**商品：** {escape_text(code + ' ' + name)}"]
        if body.strip():
            lines.append(f"**修改内容：**\n{escape_text(body, limit=None)}")
        lines.extend([f'**负责人：** <at id="{owner_id}"></at>', f'**视觉设计：** <at id="{designer}"></at>'])
        url = f"https://feishu.cn/base/{self.client.settings.base_token}?table={self.config['table']}"
        buttons = []
        source = row.get(fields["source_link"], {}).get("link")
        if source:
            buttons.append(("查看原消息", source))
        buttons.append(("查看需求", f"{url}&record={record}") if record else ("查看需求表", url))
        columns = []
        for label, link in buttons:
            button = {"tag": "button", "text": {"tag": "plain_text", "content": label}, "type": "default",
                      "behaviors": [{"type": "open_url", "default_url": link}]}
            columns.append({"tag": "column", "width": "auto", "elements": [button]})
        return {"schema": "2.0",
                "header": {"template": "purple", "title": {"tag": "plain_text", "content": f"数据包{kind} · 负责人提醒"}},
                "body": {"elements": [{"tag": "markdown", "content": "\n".join(lines)},
                                      {"tag": "column_set", "flex_mode": "none", "columns": columns}]}}

    def register_images(self, db, message):
        """保存待同步图片。参数 db：同步状态库；message：命中需求的群消息。返回值：无，随需求一起提交。"""
        db.executemany("INSERT OR IGNORE INTO message_images (message, image, position) VALUES (?,?,?)",
                       [(message["message_id"], key, index) for index, key in enumerate(message_image_keys(message))])

    def attach_images(self, db, message_id, row):
        """填充待登记记录的附件。参数 db：同步状态库；message_id：原消息 ID；row：待填写的记录字段。返回值：无。"""
        attachments = []
        for image, token in db.execute("SELECT image, token FROM message_images WHERE message=? ORDER BY position",
                                       (message_id,)).fetchall():
            if not token:
                token = self.client.upload_message_image(message_id, image)
                db.execute("UPDATE message_images SET token=? WHERE message=? AND image=?", (token, message_id, image))
                db.commit()
                logger.info("视觉群图片已上传 message=%s image=%s", message_id, image)
            attachments.append({"file_token": token})
        if attachments:
            row[self.config["fields"]["attachments"]] = attachments

    def deliver(self, db):
        """功能说明：登记待处理款并通知负责人。参数 db：本地同步状态库。返回值：无，逐步保存记录及提醒结果。"""
        fields = self.config["fields"]
        due = db.execute(
            "SELECT id, payload, record, source, notification_body FROM jobs WHERE sent=0 AND notify_attempts < ? AND notify_after <= ?",
            (NOTIFY_MAX_ATTEMPTS, time.time()),
        ).fetchall()
        for key, payload, record, source, notification_body in due:
            try:
                row = json.loads(payload)
                if not record:
                    self.attach_images(db, json.loads(source)[0], row)
                    record = self.client.create_visual_record(self.config["table"], row, key)
                    db.execute("UPDATE jobs SET record=? WHERE id=?", (record, key))
                    db.commit()
            except Exception as exc:
                db.execute("UPDATE jobs SET notify_after=? WHERE id=?", (time.time() + NOTIFY_RETRY_SECONDS, key))
                db.commit()
                logger.exception("视觉需求登记失败，下轮继续 id=%s", key)
                send_error(record or "", "视觉需求登记", exc, self.client, f"视觉需求 {key}",
                           visual=True, table_id=self.config["table"], incident=f"{key}/register/{type(exc).__name__}")
                continue

            # 先持久化尝试，再请求飞书；即使进程在等待回执时退出，也保留次数与发送间隔。
            claim = db.execute(
                "UPDATE jobs SET notify_attempts=notify_attempts+1, notify_after=? "
                "WHERE id=? AND sent=0 AND notify_attempts < ? AND notify_after <= ? RETURNING notify_attempts",
                (time.time() + NOTIFY_RETRY_SECONDS, key, NOTIFY_MAX_ATTEMPTS, time.time()),
            ).fetchone()
            db.commit()
            if claim is None:
                continue
            attempt, = claim
            try:
                settings = self.client.settings
                if self.bot is None:
                    self.bot = FeishuClient(replace(settings, app_id=settings.alert_app_id,
                                                    app_secret=settings.alert_app_secret))
                if notification_body is None:
                    # 已有待发送任务按原消息 ID 恢复正文，以区分真实提及和普通的 @ 字符。
                    source_id = json.loads(source)[0]
                    original = self.client.material_get(f"/im/v1/messages/{source_id}")["items"][0]
                    notification_body = message_text(original, include_mentions=False)
                    db.execute("UPDATE jobs SET notification_body=? WHERE id=?", (notification_body, key))
                    db.commit()
                recipient = alert_open_id(row[fields["owner"]][0]["id"], self.client, self.bot)
                card = self.reminder_card(row, record, recipient, notification_body)
                message_id = self.bot.send_card(recipient, card, key, retry_transient=False)
                db.execute("UPDATE jobs SET sent=1 WHERE id=?", (key,))
                db.commit()
                logger.info("视觉需求已登记并提醒 %s record=%s message=%s", row[fields["code"]], record, message_id)
            except Exception as exc:
                db.execute("UPDATE jobs SET notify_after=? WHERE id=?", (time.time() + NOTIFY_RETRY_SECONDS, key))
                db.commit()
                outcome = "停止重试" if attempt == NOTIFY_MAX_ATTEMPTS else "30 秒后重试"
                logger.exception("视觉需求通知失败 %s/%s，%s record=%s", attempt, NOTIFY_MAX_ATTEMPTS, outcome, record)
                stage = "发送负责人提醒"
                send_error(record, stage, exc, self.client, title=row.get(fields["code"]),
                           visual=True, table_id=self.config["table"],
                           incident=f"{key}/{stage}/{type(exc).__name__}/{getattr(exc, 'code', '')}")

    @staticmethod
    def prepare_jobs(db):
        """初始化通知状态并升级已有任务。参数 db：同步状态库。返回值：无。"""
        db.execute("CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, source TEXT UNIQUE, payload TEXT, record TEXT, sent INTEGER DEFAULT 0)")
        db.execute("CREATE TABLE IF NOT EXISTS message_images (message TEXT, image TEXT, position INTEGER, token TEXT, "
                   "PRIMARY KEY (message, image))")
        columns = {row[1] for row in db.execute("PRAGMA table_info(jobs)")}
        db.execute("CREATE TABLE IF NOT EXISTS owner_rotation (owner TEXT PRIMARY KEY, last_member TEXT NOT NULL)")
        if "notification_body" not in columns:
            db.execute("ALTER TABLE jobs ADD COLUMN notification_body TEXT")
        if "notify_attempts" not in columns:
            db.execute("ALTER TABLE jobs ADD COLUMN notify_attempts INTEGER NOT NULL DEFAULT 0")
        if "notify_after" not in columns:
            db.execute("ALTER TABLE jobs ADD COLUMN notify_after REAL NOT NULL DEFAULT 0")

    def poll(self, stop, started, wake):
        """功能说明：按连接和消息事件补读群历史，独立处理待重试任务。参数 stop：退出信号；started：首次启用的起始毫秒；wake：连接就绪或目标群新消息信号。返回值：无。"""
        path = self.client.settings.rules_cache_path.parent / "visual_sync.sqlite3"
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path) as db:
            self.prepare_jobs(db)
            db.execute("CREATE TABLE IF NOT EXISTS progress (chat TEXT PRIMARY KEY, cursor INTEGER)")
            chat = self.chat_id
            db.execute("INSERT OR IGNORE INTO progress VALUES (?,?)", (chat, started))
            db.commit()
            # 启动后收到首次握手信号再补读，覆盖握手前的消息；已有任务可立即恢复。
            read_pending = False
            read_incident = None
            while not stop.is_set():
                if read_pending:
                    try:
                        start = db.execute("SELECT cursor FROM progress WHERE chat=?", (chat,)).fetchone()[0]
                        end = int(time.time() * 1000) - 3000
                        if end > start:
                            self.catalog = None
                            for message in self.messages(start, end):
                                rows = self.queue_message(db, message)
                                logger.info("视觉群消息 %s 新增 %d 款", message["message_id"], len(rows))
                            db.execute("UPDATE progress SET cursor=? WHERE chat=?", (end, chat))
                            db.commit()
                        read_incident = None
                    except Exception as exc:
                        db.rollback()
                        logger.exception("视觉群读取失败，下次触发从当前进度补读")
                        read_incident = read_incident or str(uuid.uuid4())
                        send_error("", "读取与解析群消息", exc, self.client, title="视觉共享群同步",
                                   visual=True, incident=read_incident)
                self.deliver(db)
                next_retry, = db.execute(
                    "SELECT MIN(notify_after) FROM jobs WHERE sent=0 AND notify_attempts < ?",
                    (NOTIFY_MAX_ATTEMPTS,),
                ).fetchone()
                wait = max(0, next_retry - time.time()) if next_retry is not None else None
                read_pending = wake.wait(wait)
                if read_pending:
                    wake.clear()
                    # 历史接口按秒分页，等到消息进入上面的三秒可读窗口再取。
                    stop.wait(3.1)


def start_visual_sync(settings):
    """功能说明：启动共享群事件监听和历史补读。参数 settings：运行配置。返回值：停止信号的回调。"""
    worker = VisualSync(FeishuClient(settings))
    context = multiprocessing.get_context("spawn")
    wake = context.Event()
    stop = context.Event()
    started = int(time.time() * 1000)
    listener = context.Process(target=_event_worker, args=(settings, wake, stop), name="visual-events", daemon=True)
    listener.start()

    def run():
        """运行同步线程，意外退出时报告程序错误并从已保存进度恢复。"""
        incident = str(uuid.uuid4())
        while not stop.is_set():
            try:
                worker.poll(stop, started, wake)
                return
            except Exception as exc:
                logger.exception("视觉群处理线程异常，30 秒后恢复")
                send_error("", "视觉群处理线程", exc, worker.client, "视觉共享群同步",
                           visual=True, incident=incident)
                stop.wait(30)
                wake.set()

    threading.Thread(target=run, name="visual-sync", daemon=True).start()
    logger.info("视觉共享群服务端事件监听与历史补读已启动")

    def close():
        """关闭长连接进程，唤醒并结束读取线程。"""
        stop.set()
        wake.set()
        if listener.is_alive():
            listener.terminate()
        listener.join()

    return close


def main():
    """功能说明：只读回放历史消息。参数：从命令行读取样本数。返回值：无，通过日志输出预览。"""
    parser = argparse.ArgumentParser(description="只读回放视觉群历史消息")
    parser.add_argument("--preview", type=int, required=True, metavar="条数")
    args = parser.parse_args()
    if args.preview < 1:
        parser.error("预览条数必须为正数")
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    worker = VisualSync(FeishuClient(Settings.from_env()))
    counts = Counter()
    names = {v: k for k, v in worker.members.items()}
    fields = worker.config["fields"]
    with sqlite3.connect(":memory:") as preview:
        worker.prepare_jobs(preview)
        for message in reversed(list(worker.messages(limit=args.preview))):
            rows = worker.queue_message(preview, message)
            counts["读取"] += 1
            counts["拟入表消息" if rows else "跳过消息"] += 1
            for row in rows:
                kind = row.get(fields["kind"], "类型留空")
                counts[kind] += 1
                logger.info("预览 %s %s %s %s → %s", message["message_id"], kind, row[fields["code"]],
                            row[fields["name"]], names[row[fields["owner"]][0]["id"]])
    logger.info("只读回放完成 %s", dict(counts))


if __name__ == "__main__":
    main()

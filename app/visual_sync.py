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
from app.notifications import alert_open_id
from app.recommendation import product_owner
from app.rules import plain

logger = logging.getLogger(__name__)
ZONE = ZoneInfo("Asia/Shanghai")
NOTIFY_MAX_ATTEMPTS = 3
NOTIFY_RETRY_SECONDS = 30


def listen_visual_events(settings, on_message):
    """通过服务端 SDK 订阅群消息。参数 settings：飞书应用和目标群配置；on_message：接收目标群消息 ID 的回调。返回值：无，持续监听。"""
    import lark_oapi as lark

    def received(data):
        """筛选目标群的人员消息，并通知读取线程。"""
        event = data.event
        if event.message.chat_id == settings.visual_sync_chat_id and event.sender.sender_type == "user":
            on_message(event.message.message_id)

    handler = lark.EventDispatcherHandler.builder("", "").register_p2_im_message_receive_v1(received).build()
    client = lark.ws.Client(settings.app_id, settings.app_secret, event_handler=handler,
                            log_level=lark.LogLevel.WARNING)
    client.start()


def _event_worker(settings, wake, stop):
    """在独立进程运行阻塞式 SDK。参数 settings：应用配置；wake：唤醒同步信号；stop：退出信号。返回值：无。"""
    logging.basicConfig(level=logging.INFO)

    def notify(message_id):
        """记录事件并唤醒处理线程。"""
        logger.info("收到视觉群消息事件 %s", message_id)
        wake.set()

    while not stop.is_set():
        try:
            listen_visual_events(settings, notify)
        except Exception as exc:
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


def message_text(message):
    """功能说明：还原消息的可读正文。参数 message：群消息。返回值：保留原有换行和提及的文字。"""
    body = json.loads(message["body"]["content"])
    mentions = {item["key"]: "@" + item["name"] for item in message.get("mentions", [])}
    if message["msg_type"] == "text":
        return re.sub(r"@_user_\d+", lambda m: mentions.get(m[0], m[0]), body["text"])
    lines = [body["title"]] if body.get("title") else []
    for paragraph in body.get("content", []):
        pieces = []
        for node in paragraph:
            if node["tag"] == "at":
                pieces.append(mentions.get(node.get("user_id"), "@" + node.get("user_name", "")))
            elif node["tag"] == "a" and node.get("href") != node.get("text"):
                pieces.append(f"{node.get('text', '')} {node.get('href', '')}")
            else:
                pieces.append(node.get("text", ""))
        lines.append("".join(pieces))
    return "\n".join(lines).strip("\n")


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
    """功能说明：按商品段落提取独立款及组合。参数 text：消息正文；config：匹配规则。返回值：货号、品名及该款需求类型序列。"""
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
            yield *key, demand_type("\n".join(lines), config) or demand_type("\n".join(header), config)


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
        self.catalog = None

    def resolve(self, codes, name):
        """功能说明：按完整货号组合和品名版本查认领人。参数 codes：货号串；name：通知品名。返回值：品名与负责人标识，未匹配返回空标识。"""
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
        if owner not in self.members:
            logger.info("跳过款号 %s：%s", codes, reason)
            return name, None
        if not name and candidates:
            name = pattern.sub("", plain(candidates[0]["fields"][self.product["title_field"]])).strip(" -")
        return name, self.members[owner]

    def plan(self, message):
        """功能说明：生成可写入的记录。参数 message：一条真实群消息。返回值：飞书字段列表，仅包含已匹配负责人的款。"""
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
        analysis_text = body
        for mention in message.get("mentions", []):
            analysis_text = analysis_text.replace("@" + mention["name"], "")
        for codes, name, kind in products(analysis_text, cfg):
            if not kind and cfg["always_mention"] not in mentions:
                continue
            name, owner_id = self.resolve(codes, name)
            if not owner_id:
                continue
            values = dict(code=codes, name=name, body=body, platform=cfg["platform"],
                          owner=[{"id": owner_id}], sender=[{"id": sender["id"]}],
                          date=int(date.timestamp() * 1000))
            if link:
                values["source_link"] = {"text": "查看原消息", "link": link}
            if kind:
                values["kind"] = kind
            rows.append({cfg["fields"][k]: v for k, v in values.items()})
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

    def deliver(self, db):
        """功能说明：登记待处理款并通知负责人。参数 db：本地同步状态库。返回值：无，逐步保存记录及提醒结果。"""
        fields = self.config["fields"]
        due = db.execute(
            "SELECT id, payload, record FROM jobs WHERE sent=0 AND notify_attempts < ? AND notify_after <= ?",
            (NOTIFY_MAX_ATTEMPTS, time.time()),
        ).fetchall()
        for key, payload, record in due:
            row = json.loads(payload)
            try:
                if not record:
                    record = self.client.create_visual_record(self.config["table"], row, key)
                    db.execute("UPDATE jobs SET record=? WHERE id=?", (record, key))
                    db.commit()
                url = f"https://feishu.cn/base/{self.client.settings.base_token}?table={self.config['table']}&record={record}"
                title = row.get(fields["kind"], "数据包通知")
                label = f"{row[fields['code']]} {row[fields['name']]}"
                card = {"schema": "2.0", "header": {"title": {"tag": "plain_text", "content": title}},
                        "body": {"elements": [{"tag": "div", "text": {"tag": "plain_text", "content": label}},
                                              {"tag": "markdown", "content": f"[查看需求记录]({url})"}]}}
            except Exception:
                logger.exception("视觉需求登记失败，下轮继续 id=%s", key)
                continue

            # 先持久化尝试，再请求飞书；即使进程在等待回执时退出，也保留次数与发送间隔。
            attempt = db.execute(
                "UPDATE jobs SET notify_attempts=notify_attempts+1, notify_after=? "
                "WHERE id=? AND sent=0 AND notify_attempts < ? AND notify_after <= ? RETURNING notify_attempts",
                (time.time() + NOTIFY_RETRY_SECONDS, key, NOTIFY_MAX_ATTEMPTS, time.time()),
            ).fetchone()
            db.commit()
            if attempt is None:
                continue
            try:
                settings = self.client.settings
                if self.bot is None:
                    self.bot = FeishuClient(replace(settings, app_id=settings.alert_app_id,
                                                    app_secret=settings.alert_app_secret))
                recipient = alert_open_id(row[fields["owner"]][0]["id"], self.client, self.bot)
                message_id = self.bot.send_card(recipient, card, key, retry_transient=False)
                db.execute("UPDATE jobs SET sent=1 WHERE id=?", (key,))
                db.commit()
                logger.info("视觉需求已登记并提醒 %s record=%s message=%s", label, record, message_id)
            except Exception:
                db.execute("UPDATE jobs SET notify_after=? WHERE id=?", (time.time() + NOTIFY_RETRY_SECONDS, key))
                db.commit()
                outcome = "停止重试" if attempt[0] == NOTIFY_MAX_ATTEMPTS else "30 秒后重试"
                logger.exception("视觉需求通知失败 %s/%s，%s record=%s", attempt[0], NOTIFY_MAX_ATTEMPTS, outcome, record)

    @staticmethod
    def prepare_jobs(db):
        """初始化通知状态并升级已有任务。参数 db：同步状态库。返回值：无。"""
        db.execute("CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, source TEXT UNIQUE, payload TEXT, record TEXT, sent INTEGER DEFAULT 0)")
        columns = {row[1] for row in db.execute("PRAGMA table_info(jobs)")}
        if "notify_attempts" not in columns:
            db.execute("ALTER TABLE jobs ADD COLUMN notify_attempts INTEGER NOT NULL DEFAULT 0")
        if "notify_after" not in columns:
            db.execute("ALTER TABLE jobs ADD COLUMN notify_after REAL NOT NULL DEFAULT 0")

    def poll(self, stop, started, wake):
        """功能说明：轮询启动后的消息。参数 stop：退出信号；started：本次服务启动毫秒；wake：目标群新消息信号。返回值：无。"""
        path = self.client.settings.rules_cache_path.parent / "visual_sync.sqlite3"
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path) as db:
            self.prepare_jobs(db)
            db.execute("CREATE TABLE IF NOT EXISTS progress (chat TEXT PRIMARY KEY, cursor INTEGER)")
            chat = self.chat_id
            db.execute("INSERT OR IGNORE INTO progress VALUES (?,?)", (chat, started))
            db.commit()
            while not stop.is_set():
                try:
                    start = db.execute("SELECT cursor FROM progress WHERE chat=?", (chat,)).fetchone()[0]
                    end = int(time.time() * 1000) - 3000
                    if end > start:
                        self.catalog = None
                        for message in self.messages(start, end):
                            rows = self.plan(message)
                            logger.info("视觉群消息 %s 匹配 %d 款", message["message_id"], len(rows))
                            for row in rows:
                                fields = self.config["fields"]
                                source = json.dumps([message["message_id"], row[fields["code"]], row[fields["name"]]])
                                db.execute("INSERT OR IGNORE INTO jobs (id,source,payload) VALUES (?,?,?)",
                                           (str(uuid.uuid4()), source, json.dumps(row, ensure_ascii=False)))
                        db.execute("UPDATE progress SET cursor=? WHERE chat=?", (end, chat))
                        db.commit()
                except Exception:
                    db.rollback()
                    logger.exception("视觉群读取失败，将从当前进度重试")
                self.deliver(db)
                next_retry, = db.execute(
                    "SELECT MIN(notify_after) FROM jobs WHERE sent=0 AND notify_attempts BETWEEN 1 AND ?",
                    (NOTIFY_MAX_ATTEMPTS - 1,),
                ).fetchone()
                wait = min(30, max(0, next_retry - time.time())) if next_retry is not None else 30
                if wake.wait(wait):
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
    threading.Thread(target=worker.poll, args=(stop, started, wake), name="visual-sync", daemon=True).start()
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
    for message in worker.messages(limit=args.preview):
        rows = worker.plan(message)
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

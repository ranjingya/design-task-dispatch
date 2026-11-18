# design-task-dispatch

美工需求评级与人员推荐服务，使用 Flask + Gunicorn，业务数据保存在飞书多维表格。

表单新增 → 自动化 HTTP 请求 → 读取需求与材料 → AI 分档 → 按规则及负载推荐 → 回写 → 通知组长。

## 处理规则

- 根据创建人的组织架构确定部门，只匹配适用部门的 ABC 规则；需求人为空时复制创建人。
- 标题、说明明确命中工作关键词时优先匹配，等级按规则默认值及调整条件判断，负载分取命中规则的分数。
- 无法准确匹配时回写候选及待确认原因；人员推荐支持轮流、固定人员、对应人员、人工分配。
- 已指定最终负责人、组长确认、完成或取消的需求保留人工结果，回写前再次检查。
- 支持两列附件和需求文档链接：图片、Excel、Word、PDF，以及飞书文档、Wiki、电子表格、多维表和内嵌画板。图片先生成总览，再按需补图，默认 3 轮、首轮 5 张、后续每轮 10 张。
- 压缩包、PSD、PPT、音视频及其他不可读附件排除后继续判断；在线资料读取缺口会提示待确认。外部网站及网盘链接不读取。本地解析旧 DOC 需要 LibreOffice，Docker 镜像已提供。

京东创意图的运营整表链接会先确定本次记录：已有有效关联则复用，空关联按 SKU → 款号 → 品名及明确规格自动查找。选取不晚于需求提交日期的最新批次，再筛选未安排、未提交的记录；唯一确定后回填关联，只读取本批材料。无法确定时交人工确认，具体记录链接按其明确范围读取。

## 本地运行

使用 Python 3.14 与 uv，按 [.env.example](.env.example) 填写 `.env`；已有文件保留并补齐参数。

```bash
uv sync
uv run flask --app app.main:create_app run --host 127.0.0.1 --port 8000
uv run pytest
```

开发时关闭自动重载。真实模型回归使用 `LIVE_AI_TESTS=1 uv run pytest tests/test_live_rating.py -q`，会调用模型，不回写飞书。

## 配置

| 文件 | 内容 |
| --- | --- |
| `.env` | 数据与通知应用凭证、Base Token、各表 ID、模型接口、超时重试及路径参数，模板见 `.env.example` |
| `config/fields.json`、`config/rule_fields.json` | 需求表与规则表的字段映射 |
| `config/recommendation.json` | 成员、轮换顺序、商品归属路径、店铺主副负责人及别名 |
| `config/material_scope.json` | 运营创意图的来源字段、批次条件及关联列 |
| `config/materials.json` | 文件大小、页数、文本量、域名范围与图片读取轮次等限额 |
| `config/notifications.toml` | 通知开关、固定接收人及动态接收人字段 |
| `prompts/classify.md`、`prompts/select_images.md`、`prompts/identify_shop.md` | 评级、补图与店铺消歧提示词 |

配置和提示词按任务或通知读取，字段映射在接收任务时保存快照。修改 `.env` 后重建容器以加载新值。

数据应用需具备需求表读写、规则及来源表读取、附件下载、资料阅读和通讯录组织架构权限。通知应用需启用机器人及 `im:message:send_as_bot`、`contact:contact.base:readonly` 权限，接收人在应用可用范围内。通知配置使用通知应用下的 `ou_` Open ID。

## 飞书自动化与通知

基础地址：`https://design-dispatch.kktree.cn`。所有 POST 请求填写以下 Headers，Params 留空：

```text
Content-Type: application/json
X-Webhook-Token: <.env 中的 WEBHOOK_TOKEN>
```

新增需求：任务名称非空时调用 `POST /webhook/task`，请求体如下；`record_id` 插入触发记录的记录 ID 变量。

```json
{"record_id":"recXXXXXXXX"}
```

返回 202 表示入队，200 表示重复请求；评级、推荐及回写在后台执行。回写成功后发送「美工任务上新 · 组长确认」，接收人由 `new_task` 配置；当前启用软糖、饭团、鸭腿。

确认与截止提醒调用 `POST /webhook/notify`，按下表替换 `event`：

```json
{"event":"leader_confirmed","record_id":"recXXXXXXXX"}
```

| event | 自动化触发条件 | 接收人 |
| --- | --- | --- |
| `leader_confirmed` | 组长确认勾选，最终负责人非空 | 全部最终负责人 |
| `assignee_confirmed` | 负责人确认勾选，最终负责人非空 | 创建人 |
| `deadline_reminder` | 截止日期当天 09:00，实际完成时间为空 | 全部最终负责人 |

确认通知为紫色，截止提醒为红色，均带查看需求按钮。流程通知同步校验最新状态；返回 `sent_or_duplicate`、`skipped` 或 `disabled`。超时或 503 可重试相同请求，已发送人员自动跳过。

程序处理或通知报错时，通过 `error` 配置私聊鸭腿，发送红色卡片，包含任务、失败环节、简要原因及需求链接；正常待确认沿用业务流程。

## 人员与负载

- 当前负载＝组长已确认任务的最终负责人未完成负载＋尚未确认任务的系统推荐人预占。计算时排除当前需求、已取消、已完成及缺分或负分任务；历史缺分不影响人员参与推荐。
- 负载比较在候选范围内选择当前负载最低者，同分按配置轮换。多人任务按主副顺序推荐，每人计整条任务分数。
- 商品归属先查 2026 数据包，再查 2027；店铺依据配置匹配，歧义时用 AI 辅助识别，负责人取自配置。
- 成员月度负荷表每人每月一行，按最终负责人及提交月份累计已完成、未完成的有效分数；人员和月份人工维护，分数由飞书公式计算。

具体对应关系见 [人员推荐规则](docs/recommendation-rules.md)，月份筛选与统计范围见 [工作看板](docs/work-dashboard.md)。

## 部署

GitHub Actions 在推送 `v*` 标签或手动触发时构建镜像并部署，工作流见 [.github/workflows/cd.yml](.github/workflows/cd.yml)。仓库变量配置 `DEPLOY_DIR`，Secrets 配置 `SSH_HOST`、`SSH_PORT`、`SSH_USER`、`SSH_PRIVATE_KEY`、`SSH_PRIVATE_KEY_PASSPHRASE`。

服务器准备 `.env`、`config/`、`prompts/` 和 Traefik 外部网络 `traefik-network`。容器只读挂载配置和提示词，`logs/` 映射到宿主机，`rules-cache` 卷保存规则、轮换、材料范围和通知记录。自动部署保留已有外置配置，变更文件需单独同步。

```bash
docker compose pull design-task-dispatch
docker compose up -d design-task-dispatch
curl -fsS https://design-dispatch.kktree.cn/health
```

## 日常维护

```bash
# 容器日志与宿主机日志
docker compose logs -f design-task-dispatch
tail -f logs/app.log

# 重新提交识别任务；已有人工安排时保留原结果
docker compose exec design-task-dispatch /app/.venv/bin/python -m app.cli retry recXXXXXXXX

# 仅补发尚未成功的新增通知
docker compose exec design-task-dispatch /app/.venv/bin/python -m app.cli notify recXXXXXXXX

# 清除规则快照，下一条需求重新读取规则表
docker compose exec design-task-dispatch rm -f /app/cache/rules.json
```

规则在每天首条需求时刷新，当天复用；刷新失败使用最后可用快照，无可用快照时报告失败。通知缓存用于防止重复发送，保留 `notifications/`；未知发送结果超过一小时需人工核对。

日志写入 `logs/app.log`，10 MiB 轮转、保留 5 份；任务正常输出摘要，异常保留阶段及堆栈。容器 UID/GID 为 `10001:10001`，日志目录 `2750`、文件 `0640`，属组为部署用户主组，CD 自动设置权限。

服务保持单实例、单进程，后台按顺序处理。内存队列最多 100 条，处理中及成功后五分钟内去重；失败可重新投递，重启后未完成任务需补跑。飞书和模型的瞬时故障最多尝试三次。`GET /health` 检查 HTTP 服务存活。

## 视觉共享群同步

在 `.env` 中将 `VISUAL_SYNC_CHAT_ID` 设为目标群的飞书群 ID（`oc_` 开头）。服务以数据应用身份通过飞书服务端 SDK 长连接订阅「接收消息 v2.0」（`im.message.receive_v1`）。目标群的人员消息触发读取，约 3 秒后通过历史消息 API 按游标获取原文。空闲时每 30 秒补读，按消息和款式去重。首次设置 `VISUAL_SYNC_ENABLED=1` 启动时记录起点，此后重启从已保存的进度继续。提及人员、需求表及字段映射见 `config/visual_sync.json`；服务器挂载的 config 目录需包含此文件。数据包来源及成员映射沿用人员推荐配置。同步进度、已登记的记录及提醒结果保存在规则缓存目录下的 `visual_sync.sqlite3`。

1. **筛选消息。** 对群成员发送的文字和图文消息，凡是 @蛋糕（含同时 @蛋糕、@荞麦）都进入款式匹配。仅 @荞麦时，正文须能识别为上新或优化。
2. **确认款式和负责人。** 按完整货号、品名及版本查数据包负责人。独立款分别登记，共用一个数据包的组合款合并登记；一条消息涉及多款时，只登记能明确匹配负责人的款。
3. **登记到「杭州优化需求」。** 货号、品名分列填写。「修改信息」保留当条消息的文字、路径和 @ 人员，「视觉设计」取发送人，「发布时间」取发送日期，「同步平台」填全平台。需求类型填上新或优化；@蛋糕的消息类型不明确时留空。
4. **关联群内原文。** 在「原消息链接」填写显示为「查看原消息」的超链接，同一条消息拆出的记录共用该链接。若飞书未提供定位信息，该字段留空。「消息附件截图」「已优化」「完成时间」留给人工维护。
5. **提醒负责人。** 登记成功后，鸭腿机器人私聊对应负责人。卡片展示需求类型、货号、品名和「查看需求记录」入口；类型留空时标题为「数据包通知」。负责人可从卡片打开表内记录，再点「查看原消息」查看群聊上下文。若提醒发送失败，程序使用已登记的记录重试。

使用 `uv run python -m app.visual_sync --preview 80` 只读回放最近 80 条历史消息，日志显示拆款、分类、拟提醒人员及跳过结果。预览独立于正式同步进度。

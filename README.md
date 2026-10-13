# design-task-dispatch

美工需求识别与任务分配服务，使用 Flask，单进程运行。

## 当前能力

- 接收 `POST /webhook/task`，校验共享密钥和记录 ID，在单个后台线程执行评级。
- 读取需求及工作类型单选选项；规则表全量分页读取后保存为每日本地快照。
- 支持 PNG、JPEG、WebP 图片附件，下载后以 Base64 交给 DeepSeek。
- 模型返回固定 JSON，程序校验规则 ID、部门范围、类型名称及等级，再回写工作类型、分类等级、匹配结果和任务负载分。
- 信息不足、规则冲突或附件无法完整读取时，匹配结果展示候选规则、可能匹配的依据、未确定原因及待补充信息；工作类型、分类等级和任务负载分清空，避免保留过期评级。
- 模型网络异常、限流和部分服务端错误最多尝试三次；非法输出不会写入工作类型。失败提示写入匹配结果，详细异常留在日志。
- 对处理中及最近五分钟成功完成的记录去重；失败记录可重新投递。
- 字段映射接收任务时读取，提示词调用模型前读取；业务数据及结果保存在飞书。

人员推荐在评级后执行，组长确认最终负责人。PDF、Word、Excel、GIF 等附件当前进入待确认分支，需要补充支持的图片后重新触发。

## 规则快照

每天第一条需求触发规则刷新，当天后续需求使用本地快照，无需求时不请求规则表。自然日按服务所在时区计算，Docker 为 Asia/Shanghai。每次调用模型仍发送本次匹配所需的规则内容。

本地默认保存到 `.cache/rules.json`，可通过 `RULES_CACHE_PATH` 指定。Docker 使用 `rules-cache` 持久化卷中的 `/app/cache/rules.json`，容器重建后保留。快照包含来源 Base、规则表 ID、日期和全部原始规则，来源不一致或文件损坏会重新加载。刷新校验成功后原子替换文件。

刷新失败时使用最后可用快照并记录警告，下一条需求重试；没有可用快照则处理失败。当天修改飞书规则，次日首次需求生效；需立即生效时删除规则快照，下一条需求会重新读取。提示词及字段映射仍按每次任务读取。

## 评级配置

在 `.env` 配置同一 Base 下的 `FEISHU_RULES_TABLE_ID`，以及 `DEEPSEEK_BASE_URL`、`DEEPSEEK_API_KEY`、`DEEPSEEK_MODEL`。接口地址可包含 `/v1`，程序追加 `/chat/completions`。模型使用思考模式进行规则判断，输出上限 8192 tokens。默认超时 90 秒，可通过 `AI_TIMEOUT_SECONDS` 调整；`AI_MAX_ATTEMPTS` 为总尝试次数，范围 1～3。

`config/fields.json` 中 `work_type`、`level` 对应「工作类型」「分类等级」单选字段，`department` 对应「部门」单选字段，`platform` 对应「平台」文本字段，`load_score` 对应「任务负载分」数字字段，`rule_explanation` 对应「匹配结果」文本字段，`attachments` 对应附件字段。`config/rule_fields.json` 映射规则表的类型、内容、部门、默认等级、调整说明、分配方式和人员负载分数，每次任务重新读取。规则表与需求表位于同一 Base。

程序按需求「部门」单选值精确筛选规则，「平台」用于提供业务背景。规则「适用部门」支持多选标签与分隔文本，类型和等级支持单选标签。需求部门缺失或无效时，模型只能列候选并要求补充部门；不根据平台或提交人姓名推测部门。已确认部门后仅向模型发送该部门适用的规则，输出再次校验。规则名称可以相同，规则 ID 必须唯一；回写文本展示具体规则 ID、内容、部门和分配方式。

在适用部门范围内，标题或需求说明明确命中「包含的工作」关键词时优先匹配；更具体的工作描述优先于宽泛相关项。未提供套版等细节时使用规则默认等级，明确符合调整条件才升降档。仅在实际多规则歧义、信息过少或内容冲突时返回候选，不猜测需求未提到的使用场景。

`prompts/classify.md` 保存判断要求及 JSON 输出协议。输出包括 `matched`、`department`、`rule_id`、`level`、`work_type`、`reason`、`candidates`、`unmatched_reason`、`missing_information`。程序只校验结构和引用一致性，具体分档准确性需要业务样本验证。

默认最多 8 张图片，单张 10 MiB，总计 20 MiB，可通过 `MAX_IMAGES`、`MAX_IMAGE_BYTES`、`MAX_TOTAL_IMAGE_BYTES` 调整；编码后的完整模型请求限制为 45 MiB。任一附件不支持、过大或下载失败时，向模型明确传入材料缺口；只允许输出候选，禁止基于部分材料给出确定评级。

模型接口参考：[JSON 输出](https://api-docs.deepseek.com/guides/json_mode/)、[图片输入](https://api-docs.deepseek.com/guides/vision/)。

## 人员负荷统计

准确匹配后，程序根据模型返回并通过校验的规则 ID 读取该条「人员负载分数」，将数字与评级结果一次回写需求表的「任务负载分」。同名工作类型按具体规则区分，分数不由模型生成，也不按分类等级或数量换算。规则分数缺失、为负或不是有效数字时，保留分类结果，负载分留空并在匹配结果说明；0 是有效分数。

「任务负载分」可在日常视图隐藏，由程序维护。它保存本次处理采用的规则分数，重新处理时更新。规则快照按自然日刷新。

成员负荷表按人员自动汇总全部日期的正式任务，排除「是否测试」「是否取消」及已有「实际完成时间」的任务：

- 未完成任务：组长已确认，最终负责人为该成员。
- 待确认推荐：组长尚未确认，系统推荐人为该成员。
- 总负载分：未完成负载分与待确认负载分之和。

某类任务存在缺分或负数时，该类负载分及总负载分留空；没有任务时显示 0。任务数照常显示。程序每次从完整需求记录实时计算同口径负载，排除当前需求已有预占。

## 本地运行

使用 Python 3.14 与 uv：

```bash
uv sync
cp .env.example .env
```

已有 `.env` 时直接补充所缺参数，保留应用凭证。评级配置缺失时，任务会失败并写入匹配结果。`WEBHOOK_TOKEN` 填写随机密钥，并在飞书自动化请求头中填写相同值。

```bash
uv run flask --app app.main:create_app run --host 127.0.0.1 --port 8000
uv run pytest
# 可选：真实模型业务回归，产生 API 调用但不写入飞书
LIVE_AI_TESTS=1 uv run pytest tests/test_live_rating.py -q
```

开发时关闭自动重载，保证内存去重由单个进程维护。

## Docker 部署与外部配置

GitHub Actions 在推送 `v*` 标签或手动触发时构建根目录 Dockerfile，将镜像推送到 GHCR，再通过 SCP 和 SSH 部署。

仓库配置变量 `DEPLOY_DIR` 为本项目独立部署目录；配置 secrets：`SSH_HOST`、`SSH_PORT`、`SSH_USER`、`SSH_PRIVATE_KEY`、`SSH_PRIVATE_KEY_PASSPHRASE`。首次部署前在服务器部署目录准备 `.env`，并确保已有 Traefik 外部网络 `traefik-network`。

镜像地址为 `ghcr.io/ranjingya/design-task-dispatch:latest`，工作流使用仓库所有者作为 GHCR 命名空间；仓库应位于 `ranjingya` 名下，若使用其他组织则同步修改 Compose 镜像前缀。

自动部署仅复制 Compose 文件。首次部署从镜像初始化 `config/fields.json` 和 `prompts/classify.md`，后续部署保留服务器已有配置。上线评级功能前，需同步 `config/fields.json`、`config/rule_fields.json` 和 `prompts/classify.md` 到服务器部署目录，并补齐规则表与模型环境变量。完成首次部署后，可手动执行：

```bash
docker compose pull design-task-dispatch
docker compose up -d design-task-dispatch
docker compose logs -f design-task-dispatch
```

部署目录下的 `./config` 挂载到 `/app/config`，`./prompts` 挂载到 `/app/prompts`，均为容器内只读。字段配置在宿主机的 `config/fields.json`、`config/rule_fields.json` 中维护。提示词在宿主机 `prompts/classify.md` 修改，下次模型调用生效，无需重建镜像。

修改宿主机 `config/fields.json` 后，下一个新任务读取最新映射；处理中任务继续使用接收时的快照。JSON 无效时新请求返回 503，修复文件后重试即可，无需重建镜像。环境变量修改后执行 `docker compose up -d --force-recreate`。

镜像使用 Gunicorn，固定一个进程、四个请求线程和一个后台处理线程。Traefik 通过 `traefik-network` 访问容器的 8000 端口，使用 `websecure` 入口和 `myresolver` 证书解析器提供 HTTPS：

`https://design-dispatch.kktree.cn/webhook/task`

域名需解析到部署服务器；Traefik 的入口、证书解析器及外部网络沿用服务器已有配置。

## 飞书自动化

触发条件：需求任务表添加新记录且任务名称非空。执行动作：发送 HTTP POST。

- 请求头：`Content-Type: application/json`
- 请求头：`X-Webhook-Token: 与 .env 中 WEBHOOK_TOKEN 相同的值`
- 请求体：`{"record_id":"第 1 步新增记录的记录 ID 变量"}`

成功接收返回 202，重复请求返回 200；这些回执只确认请求接收情况。后台评级结果写回多维表，错误记录在服务日志中，不输出需求正文或应用凭证。

`GET /health` 仅检查 HTTP 服务存活。

## 运行边界

任务及去重记录保存在内存，重启后需重新触发未完成任务。后台失败通过日志定位，修复后重新投递。最多同时接收 100 个等待或执行中的任务，超出返回 503。当前只运行一个实例，跨进程、跨实例不共享去重状态。

飞书应用需要需求表的读取及编辑权限、规则表读取权限，以及附件素材下载权限。用户身份可访问表格与自建应用身份可访问表格是两套授权。

## 人员推荐

`config/recommendation.json` 与字段映射放在同一目录，随现有 config 挂载到容器外。每条需求重新读取，包含人员昵称与用户 ID 映射、规则记录 ID 到归属查询方式的映射、轮换顺序、商品字段及货号识别规则、店铺主次负责人和别名。成员名单必须与成员负荷表一致。

- 轮流：全体成员中总负载最低优先，同分按配置顺序轮换。
- 固定人员：解析规则括号内名单，在指定人员中负载优先、同分轮换。
- 人工分配：系统推荐人留空，推荐结果展示成员负载参考，由组长结合文档中的创意方向及平台要求安排。创意图排除原制作人及京东专属名单尚需独立候选配置，当前展示的是全员负载参考。
- 找对应人员：按具体规则 ID 查询商品或店铺归属；未配置路径转人工。

商品归属读取数据包分配表的完整分页记录，提取完整货号，区分单款与拼款；拼款要求完整货号组合，并检查配置中的平台限制。多个匹配记录认领人为同一人时可沿用；人员不同、认领为空、货号不全或平台无法对应时转人工。明确为老款且未查到货号时进入全员负载比较。花菜等排除人员、未配置人员直接转人工。来源读取失败属于推荐失败，不作为无归属兜底。

店铺按部门和名称／别名唯一匹配，推荐主负责人，并展示协作人员负载。归属推荐保留负责关系，负载未知时在说明中提示；负载比较模式下任一候选存在缺分则暂停推荐。已取消、完成、指定最终负责人或组长确认的记录保留原结果，回写前再次读取检查。

评级与推荐字段一次回写；推荐读取失败时保留评级结果、清空系统推荐人，并在推荐结果提示重试。匹配结果负责解释规则，推荐结果负责解释人员选择。推荐结果当前采用分行文本，展示依据、候选负载及异常原因，展示格式可继续调整。

轮换位置保存在规则缓存同目录的 `rotation.json`，按候选人员集合区分轮转池，飞书回写成功后原子保存。重跑同一任务时，原推荐人仍处于最低负载候选中则保留。测试记录不计入正式负载、不推进轮换。服务仍需单实例运行；飞书回写与本地轮换文件不是跨系统事务，极端中断可能让下一次同分轮换重复。回写前检查与飞书编辑之间也存在短暂竞争窗口。

多维表资源参数统一放在 `.env`：

- `FEISHU_BASE_TOKEN`、`FEISHU_TABLE_ID`：需求任务表。
- `FEISHU_RULES_TABLE_ID`：同一 Base 下的规则表。
- `FEISHU_MEMBERS_TABLE_ID`：同一 Base 下的成员负荷表。
- `FEISHU_PRODUCT_BASE_TOKEN`、`FEISHU_PRODUCT_TABLE_ID`：商品数据包分配表。

`.env` 使用简单键值，保存凭证、Base Token、表 ID、模型接口、超时重试及路径等运行参数，`.env.example` 提供模板。`config/recommendation.json` 的 `members` 保存昵称到用户 ID 的映射，`ownership_routes` 保存规则记录 ID 到 `product` 或 `shop` 的映射；字段映射、名单及分配规则也在容器外 JSON 中维护，每次任务重新读取。修改 `.env` 后重建容器以加载新值。

应用需具备成员负荷表和跨 Base 数据包分配表读取权限。部署时同步 `.env`、 `config/recommendation.json` 和 `config/fields.json`；Compose 的现有目录挂载与缓存卷即可使用。

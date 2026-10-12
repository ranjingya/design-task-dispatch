# design-task-dispatch

美工需求识别与任务分配服务，使用 Flask，单进程运行。

## 当前能力

- 接收 `POST /webhook/task`，校验共享密钥和记录 ID，在单个后台线程执行评级。
- 读取需求、规则表全量分页数据及工作类型单选选项，每条需求使用最新规则。
- 支持 PNG、JPEG、WebP 图片附件，下载后以 Base64 交给 DeepSeek。
- 模型返回固定 JSON，程序校验规则 ID、已有选项及等级前缀，再回写工作类型和匹配结果。
- 信息不足、规则冲突或附件无法完整读取时，只写匹配结果中的待确认原因；保留已有工作类型。
- 模型网络异常、限流和部分服务端错误最多尝试三次；非法输出不会写入工作类型。失败提示写入匹配结果，详细异常留在日志。
- 对处理中及最近五分钟成功完成的记录去重；失败记录可重新投递。
- 字段映射接收任务时读取，提示词调用模型前读取；业务数据及结果保存在飞书。

人员推荐和分配属于后续功能。PDF、Word、Excel、GIF 等附件当前进入待确认分支，需要补充支持的图片后重新触发。

## 评级配置

在 `.env` 配置同一 Base 下的 `FEISHU_RULES_TABLE_ID`，以及 `DEEPSEEK_BASE_URL`、`DEEPSEEK_API_KEY`、`DEEPSEEK_MODEL`。接口地址可包含 `/v1`，程序追加 `/chat/completions`。默认超时 90 秒，可通过 `AI_TIMEOUT_SECONDS` 调整；`AI_MAX_ATTEMPTS` 为总尝试次数，范围 1～3。

`config/fields.json` 中 `work_type` 对应单选字段，`rule_explanation` 对应可写文本字段，当前分别为「工作类型」「匹配结果」。`attachments` 对应附件字段。规则字段内容原样交给模型，分类标准和选项由飞书维护，规则名称与选项之间的语义冲突交由人工确认。

`prompts/classify.md` 保存判断要求及 JSON 输出协议。输出为 `rated` 或 `needs_review`，包括 `level`、`work_type`、`matched_rule_id`、`reason`、`missing_information`。程序只校验结构和引用一致性，具体分档准确性需要业务样本验证。

默认最多 8 张图片，单张 10 MiB，总计 20 MiB，可通过 `MAX_IMAGES`、`MAX_IMAGE_BYTES`、`MAX_TOTAL_IMAGE_BYTES` 调整；编码后的完整模型请求限制为 45 MiB。任一附件不支持、过大或下载失败时，本条需求进入待确认，不基于部分材料评级。

模型接口参考：[JSON 输出](https://api-docs.deepseek.com/guides/json_mode/)、[图片输入](https://api-docs.deepseek.com/guides/vision/)。

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
```

开发时关闭自动重载，保证内存去重由单个进程维护。

## Docker 部署与外部配置

GitHub Actions 在推送 `v*` 标签或手动触发时构建根目录 Dockerfile，将镜像推送到 GHCR，再通过 SCP 和 SSH 部署。

仓库配置变量 `DEPLOY_DIR` 为本项目独立部署目录；配置 secrets：`SSH_HOST`、`SSH_PORT`、`SSH_USER`、`SSH_PRIVATE_KEY`、`SSH_PRIVATE_KEY_PASSPHRASE`。首次部署前在服务器部署目录准备 `.env`，并确保已有 Traefik 外部网络 `traefik-network`。

镜像地址为 `ghcr.io/ranjingya/design-task-dispatch:latest`，工作流使用仓库所有者作为 GHCR 命名空间；仓库应位于 `ranjingya` 名下，若使用其他组织则同步修改 Compose 镜像前缀。

自动部署仅复制 Compose 文件。首次部署从镜像初始化 `config/fields.json` 和 `prompts/classify.md`，后续部署保留服务器已有配置。上线评级功能前，需将服务器结果字段映射设为实际字段名、同步完整提示词，并补齐规则表与模型环境变量。完成首次部署后，可手动执行：

```bash
docker compose pull design-task-dispatch
docker compose up -d design-task-dispatch
docker compose logs -f design-task-dispatch
```

部署目录下的 `./config` 挂载到 `/app/config`，`./prompts` 挂载到 `/app/prompts`，均为容器内只读。字段配置在宿主机的 `config/fields.json` 中维护。

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

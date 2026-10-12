# design-task-dispatch

美工需求识别与任务分配服务，使用 Flask，单进程运行。

## 当前能力

- 接收 `POST /webhook/task`，校验共享密钥和记录 ID。
- 在进程内后台线程读取飞书记录，校验任务名称及需求说明。
- 对处理中及最近五分钟成功读取的记录去重，失败记录可重新投递。
- 从外部 JSON 文件加载字段映射，每次接收任务时读取，并为本次任务保存快照。

当前阶段完成需求读取框架。AI 分类、附件内容解析、人员推荐和业务结果回写尚待接入；`app/ai.py` 为模块入口，`FeishuClient.update_record()` 提供后续回写能力。

## 本地运行

使用 Python 3.14 与 uv：

```bash
uv sync
cp .env.example .env
```

已有 `.env` 时直接补充所缺参数，保留应用凭证。`WEBHOOK_TOKEN` 填写随机密钥，并在飞书自动化请求头中填写相同值。

```bash
uv run flask --app app.main:create_app run --host 127.0.0.1 --port 8000
uv run pytest
```

开发时关闭自动重载，保证内存去重由单个进程维护。

## Docker 部署与外部配置

GitHub Actions 在推送 `v*` 标签或手动触发时构建根目录 Dockerfile，将镜像推送到 GHCR，再通过 SCP 和 SSH 部署。

仓库配置变量 `DEPLOY_DIR` 为本项目独立部署目录；配置 secrets：`SSH_HOST`、`SSH_PORT`、`SSH_USER`、`SSH_PRIVATE_KEY`、`SSH_PRIVATE_KEY_PASSPHRASE`。首次部署前在服务器部署目录准备 `.env`，并确保已有 Traefik 外部网络 `traefik-network`。

镜像地址为 `ghcr.io/ranjingya/design-task-dispatch:latest`，工作流使用仓库所有者作为 GHCR 命名空间；仓库应位于 `ranjingya` 名下，若使用其他组织则同步修改 Compose 镜像前缀。

自动部署仅复制 Compose 文件。首次部署从镜像初始化 `config/fields.json` 和 `prompts/classify.md`，后续部署保留服务器已有配置。完成首次部署后，可手动执行：

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

成功接收返回 202，重复请求返回 200；这些回执只确认请求接收情况。后台读取结果与错误记录在服务日志中，不输出需求正文或应用凭证。

`GET /health` 仅检查 HTTP 服务存活。

## 运行边界

任务及去重记录保存在内存，重启后需重新触发未完成任务。后台失败通过日志定位，修复后重新投递。最多同时接收 100 个等待或执行中的任务，超出返回 503。当前只运行一个实例，跨进程、跨实例不共享去重状态。

飞书应用需要目标表的读取权限；接入写回后需要编辑权限。用户身份可访问表格与自建应用身份可访问表格是两套授权。

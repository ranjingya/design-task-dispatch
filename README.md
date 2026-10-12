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

```bash
docker compose up -d --build
docker compose logs -f app
```

宿主机 `./config` 挂载到 `/app/config`，`./prompts` 挂载到 `/app/prompts`，均为容器内只读。可在 `.env` 中设置 `CONFIG_DIR`、`PROMPTS_DIR` 指向其他已存在目录。外部配置目录应包含 `fields.json`。

修改宿主机 `config/fields.json` 后，下一个新任务读取最新映射；处理中任务继续使用接收时的快照。JSON 无效时新请求返回 503，修复文件后重试即可，无需重建镜像。环境变量修改后执行 `docker compose up -d --force-recreate`。

镜像使用 Gunicorn，固定一个进程、四个请求线程和一个后台处理线程。服务仅映射宿主机 `127.0.0.1:8000`，由同机反向代理提供 HTTPS：

`https://design-dispatch.kktree.cn/webhook/task`

域名解析、HTTPS 证书和反向代理需在部署环境配置。

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

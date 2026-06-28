# 部署与数据迁移指南

> 把项目从开发机迁到服务器(local-first、单用户)。本文是**实测过的**流程
> (2026-06-28 已据此把项目部署到 `cortana-box` / 192.168.178.65),含踩过的每个坑。

---

## 0. 当前线上部署(cortana-box)

| 组件 | 地址 | 进程 |
|---|---|---|
| 后端 API | `http://192.168.178.65:8000`(`/docs` 有 Swagger) | tmux `ft-backend` |
| 前端 Web | `http://192.168.178.65:3100`(**3000 被本机另一个 "World Monitor" 占用**) | tmux `ft-frontend` |
| MCP | stdio,经 `/home/jason/finance-tracker/mcp-server/run.sh` | 由客户端(OpenClaw)按需拉起 |

项目根:`/home/jason/finance-tracker`。鉴权:`AUTH_DISABLED=false`,需 token(见 §4)。

---

## 1. 关键认知:GitHub 上只有代码

`.gitignore` 排除了 `data/`(数据库、PDF、备份)和 `.env`(密钥)。所以 **`git clone` 只能拿到代码**,数据和密钥必须手动迁移。三大迁移坑(§3)。

部署方式:**裸机 + venv**(本项目当前用法;`docker-compose.yml` 是占位骨架,`Dockerfile` 尚未编写)。

---

## 2. 服务器先决条件

- Python ≥ 3.11(实测 3.12.3)、`python3-venv`、Node ≥ 18(实测 22)、`tmux`、`git`。
- 无需 sudo(全部 pip 装进项目本地 venv;依赖均有 manylinux wheel)。
- Trade Republic 同步需要的 playwright 浏览器**可选**(仅登录用):`python -m playwright install chromium`(Ubuntu 加 `--with-deps` 需 sudo)。日常 cookie 同步不需要浏览器,可跳过。

---

## 3. 数据迁移(三大坑)

### 坑 ① SQLite 一致性快照(不要直接拷 .db)
开发机有未 checkpoint 的 `-wal`。用 SQLite **在线 backup** 拿一致快照(不必停正在跑的后端):
```bash
# 开发机
python -c "import sqlite3; s=sqlite3.connect('data/finance.db'); d=sqlite3.connect('/tmp/finance_migrate.db'); \
  __import__('contextlib').closing; \
  s.backup(d); s.close(); d.close()"
python -c "import sqlite3; print(sqlite3.connect('/tmp/finance_migrate.db').execute('PRAGMA integrity_check').fetchone())"  # → ok
scp /tmp/finance_migrate.db SERVER:~/finance-tracker/data/finance.db
rsync -az data/pdfs/ SERVER:~/finance-tracker/data/pdfs/   # 已入库账单原件
```

### 坑 ② 加密密钥必须一起搬(最致命)
交易所 API key / 券商 token / TR cookie 都用 `.env` 的 `FINANCE_BANK_ENCRYPTION_KEY` 做 AES-256 加密存库。**换 key = 凭据全部无法解密**。把开发机 `.env` 的这一行原样复制到服务器 `.env`。
> 验证成功标志:后端启动日志出现 `credential_health_ok ok_count=N`(N = 已存凭据数)。

### 坑 ③ PDF 路径是绝对路径
`pdf_imports.storage_path` 存绝对路径(如 `/Users/.../data/pdfs/x.pdf`),换机器会失效(已入库交易不受影响,但「重看原 PDF」会断)。迁移后改前缀:
```bash
# 服务器
python3 -c "import sqlite3; c=sqlite3.connect('data/finance.db'); \
  c.execute(\"UPDATE pdf_imports SET storage_path=REPLACE(storage_path,'/Users/jason/Project/finance-tracker/data','/home/jason/finance-tracker/data')\"); \
  c.commit()"
```

---

## 4. 服务器 `.env`(从开发机复制后改这几项)

复制开发机 `.env` 过去,然后**仅改**网络相关项;密钥/API key 全部保持不变:

| 键 | 服务器值 | 说明 |
|---|---|---|
| `BACKEND_HOST` | `0.0.0.0` | 供局域网/浏览器访问 |
| `BACKEND_PORT` | `8000` | |
| `AUTH_DISABLED` | `false` | **必须**:代码拒绝在非 loopback(0.0.0.0)上 `AUTH_DISABLED=true`(防止裸奔)。所以局域网访问必须开 token |
| `ALLOWED_ORIGINS` | `http://192.168.178.65:3100,http://localhost:3100` | CORS,必须含前端实际地址(端口=前端端口) |
| `NEXT_PUBLIC_API_URL` | `http://192.168.178.65:8000` | **浏览器侧**调后端的地址 |
| `DATABASE_URL` | `sqlite:///./data/finance.db` | 相对路径,锚定项目根,无需改 |

> ⚠️ `NEXT_PUBLIC_*` 是 **build-time 注入**,写在 **`frontend/.env.local`**(Next 不读项目根 `.env`)。rsync 会把开发机的 `frontend/.env.local`(localhost)带过去,**build 前务必改成服务器地址**:
> ```bash
> echo 'NEXT_PUBLIC_API_URL=http://192.168.178.65:8000' > frontend/.env.local
> ```

**Token**:`AUTH_DISABLED=false` 下,浏览器首次打开需在 **Settings → API Token** 粘贴 `FINANCE_TRACKER_API_TOKEN` 的值(存浏览器 localStorage,一次即可)。`/health`、`/version` 公开;其余端点要 token。

---

## 5. 安装与启动

```bash
# 后端依赖(装进项目本地 venv,无需 sudo)
cd ~/finance-tracker
python3 -m venv .venv && .venv/bin/pip install -e "backend/[dev]"

# 前端依赖 + 构建(NEXT_PUBLIC_API_URL 必须已在 frontend/.env.local 设好)
cd frontend && npm install && npm run build && cd ..

# 启动(tmux 持久化,详见下)
bash deploy/start.sh          # 起 ft-backend(:8000) + ft-frontend(:3100)
bash deploy/stop.sh           # 停
```

### 为什么用 tmux
直接用 `nohup ... &` 经一次性 SSH 启动长进程**不可靠**(子进程绑定 SSH channel,SSH 退出即被杀)。`deploy/start.sh` 用 detached tmux 会话,SSH 断开仍存活。
- 查看日志:`tmux attach -t ft-backend`(Ctrl-B D 脱离)或 `tail -f /tmp/ft-backend.log`。
- **tmux 不扛重启**:服务器重启后重跑 `bash deploy/start.sh`。要开机自启见 §7。

---

## 6. MCP / OpenClaw 接入(同机)

本项目 MCP 是 **stdio 传输**:客户端把 server 当**子进程**拉起,双方通过同一个 `finance.db` 文件共享数据,**不走网络**。所以:

- ✅ **同机 OpenClaw** → 配置它执行服务器上的 `run.sh` 即可。`.mcp.json`(项目根)已写好:
  ```json
  { "mcpServers": { "finance-tracker": {
      "command": "/home/jason/finance-tracker/mcp-server/run.sh", "args": [] } } }
  ```
  把这段合并进 OpenClaw 的 MCP 配置(或让它读项目根 `.mcp.json`)。`run.sh` 会自动把 `mcp[cli]` 装进项目 venv,并注入 `PYTHONPATH`。
- ❌ **跨机器/跨容器不行**:stdio 非网络协议。远程需改 HTTP/SSE 传输(未实现)。
- 21 个工具(19 读 + 2 写),读数与 REST/Web 完全一致。详见 `docs/API.md §17`。
- 冒烟自测(服务器上,真实 stdio 握手):见 `docs/API.md` 或用 MCP 客户端 SDK 连 `run.sh`,`initialize` → `list_tools` 应返回 21 个。

---

## 7. 开机自启(可选,需 sudo)

tmux 方案重启后需手动 `deploy/start.sh`。要持久化,加 systemd unit(需 sudo):

```ini
# /etc/systemd/system/finance-backend.service
[Unit]
After=network.target
[Service]
User=jason
WorkingDirectory=/home/jason/finance-tracker
ExecStart=/home/jason/finance-tracker/.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
Restart=on-failure
[Install]
WantedBy=multi-user.target
```
前端类似(`ExecStart=/usr/bin/npx next start -H 0.0.0.0 -p 3100`,`WorkingDirectory=.../frontend`)。
`sudo systemctl enable --now finance-backend finance-frontend`。

---

## 8. 排障速查

| 现象 | 原因 / 解 |
|---|---|
| 后端启动即退,日志 `Refusing to start: AUTH_DISABLED=true ... loopback` | 绑了 0.0.0.0 却开着 `AUTH_DISABLED=true`。设 `AUTH_DISABLED=false`(§4)。 |
| 浏览器页面打开但数据 401 | 没贴 token。Settings → API Token 粘贴(§4)。 |
| 浏览器请求打到 `localhost` 失败 | `frontend/.env.local` 的 `NEXT_PUBLIC_API_URL` 没改成服务器地址就 build 了。改后 `npm run build` 重建。 |
| 前端 `EADDRINUSE :3000` | 3000 被占(本机 World Monitor)。用 3100(`deploy/start.sh` 默认)。 |
| 同步报凭据解密失败 | `FINANCE_BANK_ENCRYPTION_KEY` 没和数据一起迁(坑 ②)。 |
| CORS 报错 | `ALLOWED_ORIGINS` 没含前端实际 `IP:端口`。 |

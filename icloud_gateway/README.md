# iCloud Mail Gateway

这个服务部署在能访问 iCloud IMAP 的服务器上。Apple 专用密码只保存在服务器，主项目仅持有网关 API Key。

## 1. 配置

```bash
cp icloud_gateway/accounts.json.example icloud_gateway/accounts.json
export ICLOUD_GATEWAY_API_KEY='replace-with-a-long-random-key'
export ICLOUD_ACCOUNTS_FILE="$PWD/icloud_gateway/accounts.json"
```

账号支持两种导入格式：

```text
# 注册邮箱就是 IMAP 登录邮箱
email@icloud.com----xxxx-xxxx-xxxx-xxxx

# 注册邮箱是 iCloud 别名/隐藏地址，username 是实际 IMAP 登录名
alias@icloud.com----icloud-login-name----xxxx-xxxx-xxxx-xxxx
```

Apple 账户需要开启双重认证并生成 App 专用密码。不要填写 Apple 账户主密码。

## 2. 启动

```bash
python -m icloud_gateway.app
```

生产环境建议由反向代理提供 HTTPS；默认服务监听 `127.0.0.1:8789`。

Docker 部署（在项目根目录执行）：

```bash
cp icloud_gateway/docker-compose.example.yml icloud_gateway/docker-compose.yml
docker compose -f icloud_gateway/docker-compose.yml up -d --build
```

示例 Compose 只把端口发布到服务器本机 `127.0.0.1:8789`，再由 Nginx/Caddy 反向代理并终止 HTTPS。JSON 租约存储使用单个 Gunicorn worker，线程内更新由锁保护。

## 3. 导入账号与检查

```bash
curl -X POST http://127.0.0.1:8789/api/v1/accounts/import \
  -H "X-API-Key: $ICLOUD_GATEWAY_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"text":"email@icloud.com----xxxx-xxxx-xxxx-xxxx"}'

curl http://127.0.0.1:8789/api/v1/accounts/summary \
  -H "X-API-Key: $ICLOUD_GATEWAY_API_KEY"
```

## 4. 主项目配置

主项目 `.env`：

```dotenv
USE_EMAIL_SERVICE=True
EMAIL_SOURCE=icloud
ICLOUD_API_BASE=https://your-server.example.com
ICLOUD_API_KEY=replace-with-a-long-random-key
```

接口契约：

- `POST /api/v1/mailboxes/acquire`：原子领取邮箱，返回 `email` 和 `lease_id`；传入 `email + reuse=true` 可为已注册账号重新申请 OTP 租约
- `POST /api/v1/mailboxes/otp`：按 `email + lease_id + after_ts` 查询最新验证码；未到达返回 HTTP 202
- `POST /api/v1/mailboxes/release`：回收或结束邮箱租约
- `POST /api/v1/accounts/import`：在服务器导入 iCloud 邮箱素材
- `GET /api/v1/accounts/summary`：查看服务器邮箱池状态

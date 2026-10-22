# JMComic 自用 QQ 机器人

把工具箱的内核接到 QQ 上：**只有你自己的 QQ 号、只在私聊里**，可以用手机发个车号让电脑去下载 / 打包 PDF，再把文件发回你自己的私聊。

> ⚠️ **边界说明（请务必读完）**
> - 这个机器人**只服务白名单里的账号、只处理私聊**，代码里没有任何"发到群"的路径。
> - **不要**把它改成群机器人或给别人用：向他人传播成人/盗版内容是违法行为（《刑法》363/364 条、著作权法），QQ 协议也不允许。
> - 用的是非官方协议（NapCatQQ + OneBot 11），**建议用 QQ 小号**，有被风控/封号的概率。
> - 内容与站点均为第三方来源，请自行判断合法性，仅作个人备份/阅读。

---

## 1. 它能做什么

| 指令 | 作用 |
|---|---|
| `帮助` | 命令列表 |
| `查询 350234` | 标题 / 作者 / 页数 / 话数（章节号带 `p` 会自动关联整本） |
| `下载 350234` | 后台下载整本，完成后回你路径 |
| `导出 350234` | 把已下载的本子打成 **PDF + ZIP**，直接把文件发到你私聊 |
| `搜索 关键词` | 站内搜索，回前 10 条（车号 + 标题 + 作者） |
| `状态` | 保存位置 / 导出目录 / 队列 |
| 直接发车号 | 等于 `查询 车号` |

特点：

- **零额外依赖**：只用 Python 标准库 + 工具箱已有的 jmcomic/Pillow/PyYAML
- **单队列**：一次只干一件事，不会把站点打爆
- **本地导出**：PDF/ZIP 落在 `保存位置\exports\`，同时也发给你一份
- 日志写在 `bot/bot.log`

---

## 2. 安装 NapCatQQ（一次性）

1. 下载并安装 **NapCatQQ**（QQ NT 的协议端）：<https://github.com/NapNeko/NapCatQQ>
   - 装好后用**你的 QQ 小号**登录（不建议用主号）
2. 打开 NapCat 的 **WebUI**（默认 <http://127.0.0.1:6099>，登录 token 在启动日志/配置里）
3. 在「网络配置」里加两项：

   **① HTTP 服务端**（机器人用它发消息）
   ```
   端口：3000
   token：（可留空；如果设了，要填到 bot_config.yml 的 api_token）
   ```

   **② HTTP 上报 / 反向 HTTP**（消息推送给机器人）
   ```
   URL：http://127.0.0.1:8099/report?token=change-me-please
   ```
   > `change-me-please` 换成你自己的随机字符串，并和 `bot_config.yml` 的 `report_token` 保持一致。

4. 保存并启用这两个网络配置。

> NapCat 版本不同，界面文字可能是「HTTP 客户端(上报)」「Http Post」等；本质就是**让 NapCat 把事件 POST 到 `http://127.0.0.1:8099/report?token=...`**。

---

## 3. 配置机器人

编辑 `bot/bot_config.yml`：

```yaml
owners:
  - 你的QQ号          # 必须改！填你自己的号（数字，不要引号也行）
api: 'http://127.0.0.1:3000'
api_token: ''          # NapCat HTTP 服务端有 token 就填
report_port: 8099
report_token: 'change-me-please'   # 和 NapCat 上报地址里的 token 一致
send_zip: true         # 导出时是否同时发 ZIP
max_send_mb: 120       # 单文件超过这个大小就不发给 QQ
max_download_pages: 400
```

> `owners` 留空时机器人**不会理任何人**（安全默认值）。

---

## 4. 启动

双击 **`bot/启动机器人.cmd`**（或 `python bot/jmqq.py`）。

看到这样的日志就成功了：

```
[10-19 21:50:01] 上报服务已启动：http://127.0.0.1:8099/report?token=change-me-please
[10-19 21:50:01] 机器人已就绪（只响应私聊 + 白名单）。Ctrl+C 退出。
```

然后用你自己的 QQ 私聊**机器人所在的那个号**，发「帮助」试试。

---

## 5. 常见问题

| 现象 | 原因 / 处理 |
|---|---|
| 发消息没反应 | 检查 ①`owners` 里是不是你的号 ②NapCat 的「HTTP 上报」地址/token 对不对 ③看 `bot/bot.log` 有没有「忽略非白名单/非私聊消息」 |
| 日志里 `上报 token 不对` | NapCat 上报地址的 token 和 `report_token` 不一致 |
| 发文件失败 | 文件太大（调 `max_send_mb`）、或 NapCat 版本不支持 `upload_private_file`（可改用 `upload_file`，或把 `send_zip` 关掉只发 PDF） |
| 下载报错 | 和桌面版一样，站点/网络/代理问题；看 `bot/bot.log` 里的异常 |
| 想确认没对外泄露 | 整个机器人只在 `127.0.0.1` 监听；代码只实现私聊接口 |

---

## 6. 离线自检

不装 NapCat 也能验证逻辑是否正确：

```bat
python bot\jmqq.py --selftest
```

它会启动一个**假的 NapCat API 服务**，模拟"主人私聊 / 陌生人私聊 / 群消息 / 坏 token"四种事件，检查：

- 坏 token → 403，不处理
- 陌生人 / 群消息 → 完全忽略（不调用任何发送接口）
- 主人私聊「帮助」→ 只调 `send_private_msg`，且目标就是主人
- 命令解析、PDF/ZIP 导出（3 页 → 3 条 zip 条目）、文件只发私聊

---

## 7. 文件说明

```
bot/
├── jmqq.py            机器人本体（单文件，纯标准库）
├── bot_config.yml     配置（QQ 号白名单 / 端口 / token）
├── 启动机器人.cmd      双击启动（ASCII 脚本，避免中文路径解析问题）
├── README-机器人.md    本文件
└── bot.log            运行日志（自动生成）
```

导出文件位置：`<保存位置>\exports\<本子标题>.pdf` / `.zip`

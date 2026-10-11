# hjmd-monitor — 获奖名单页面更新监控

监控 **全国大学生数学建模竞赛江西赛区官网「获奖名单」页面**：

> https://sxjm.jxust.edu.cn/hjmd.htm

页面一有更新（新公告、条目修改等），自动发邮件提醒。程序运行在 **GitHub Actions 云端**，与本机无关——电脑关机、换网络都不影响监控。

## 工作原理

```
GitHub Actions（每 5 分钟）
    └─ 运行 src/monitor.py
         ├─ 抓取页面 → 提取可见文本 → 计算 SHA256 摘要
         ├─ 与 state/state.json 中保存的上次摘要对比
         │    ├─ 无变化 → 结束（长期无变化则空提交保活）
         │    └─ 有变化 → 解析公告列表、生成差异 → 发邮件 → 更新状态并提交回仓库
         └─ 连续抓取失败达到阈值 → 发一封异常提醒邮件（恢复后自动复位）
```

- 对比基于**页面可见文本的哈希**（已剔除脚本/样式、压平空白），日期导航等真实内容变化才会触发。
- 邮件内容：新增公告（标题+日期+可点击链接）、标题修改、移除条目、完整最新列表。
- 邮件发送**先于**状态更新：发信失败时状态不推进，下一轮自动重发，不会漏报。
- 对方服务器临时挂掉不会误报为"更新"，连续失败 30 分钟才发一封异常提醒。

## 目录结构

```
hjmd-monitor/
├── .github/workflows/monitor.yml   # GitHub Actions 定时工作流
├── config/monitor.json             # 监控配置（页面列表、阈值等，无敏感信息）
├── src/monitor.py                  # 监控脚本
├── state/state.json                # 运行状态（各页面哈希、公告列表、失败计数），由脚本自动维护
├── requirements.txt
└── README.md
```

## 部署（一次性，已由助手完成的部分见对话记录）

1. 在 GitHub 创建**公开**仓库（public 仓库 Actions 免费额度不限量；私有仓库每月 2000 分钟不够用）。
2. 上传本目录全部文件。
3. 添加仓库 Secrets（Settings → Secrets and variables → Actions）：见下表。
4. 手动触发一次工作流验证（Actions → 网页更新监控 → Run workflow）。

### Secrets 一览

| 名称 | 说明 | 示例 |
| --- | --- | --- |
| `SMTP_HOST` | SMTP 服务器地址 | `smtp.qq.com` / `smtp.163.com` / `smtp.gmail.com` |
| `SMTP_PORT` | 端口，SSL 用 `465`，STARTTLS 用 `587` | `465` |
| `SMTP_USER` | 发件邮箱账号 | `xxx@qq.com` |
| `SMTP_PASS` | 邮箱 SMTP **授权码**（不是登录密码） | QQ/163 在邮箱设置里开启 SMTP 后生成 |
| `MAIL_TO` | 收件邮箱，多个用英文逗号分隔 | `xxx@qq.com` |

代码与状态文件中不含任何凭据，全部走 GitHub 加密 Secrets，公开仓库无泄露风险。

## 日常维护

- **手动触发检查**：Actions → 网页更新监控 → Run workflow。
- **改检查频率**：改 `monitor.yml` 中 `cron`（GitHub 限制最短 `*/5`）。注意 Actions 定时触发可能延迟几分钟，属正常现象。
- **增加监控页面**：在 `config/monitor.json` 的 `urls` 数组里加一项即可，通知、状态、差异全部自动支持。
- **收不到邮件**：先检查垃圾箱；再把发件邮箱加入联系人/白名单。
- **本地调试**（可选）：`python src/monitor.py --dry-run`——不发邮件、不推送，邮件内容打印到终端。

## 为什么不会被 GitHub 停用

GitHub 会把 **60 天无活动**的仓库定时工作流自动停用。本项目有两道保险：

1. 每次检测到更新都会提交状态文件，本身就是仓库活动；
2. 页面长期无变化时，脚本每 7 天自动做一次空提交（keepalive）。

## 已知边界

- 抓取的是列表页本身；新公告发布后，邮件里直接给出该公告的详情链接。
- GitHub Actions 定时任务在高峰期偶尔跳过/延迟一次，下个周期自动补上，不影响正确性（状态对比与上次成功结果进行，不依赖连续性）。

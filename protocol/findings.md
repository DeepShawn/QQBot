# QQ + NapCat 协议端部署记录（Android proot Ubuntu 24.04 / aarch64）

> 位置：`/root/workspace/qqbot/protocol/`（服务本体在系统目录 `/opt/QQ`、`/opt/napcat`）
> 最后更新：2026-11-07 13:20（Asia/Singapore）

## 一、版本与安装位置

| 组件 | 版本 | 位置 |
|---|---|---|
| QQ Linux (NT) | **3.2.34-53644**（buildVersion 53644） | `/opt/QQ/qq`（apt 安装，包 linuxqq_3.2.34-53644_arm64.deb） |
| NapCat Shell | **4.18.42** | `/opt/napcat/napcat/`（NapCat.Shell.zip 解压） |
| launcher | napcat-linux-launcher 编译产物 | `/opt/napcat/libnapcat_launcher.so`（源码 `/opt/napcat/launcher.cpp`） |
| 启动包装 | xvfb-run + LD_PRELOAD | `/opt/napcat/launcher.sh` |

安装方式：先用 apt 装 curl/jq/procps/g++/gtk3 等依赖，再运行官方非侵入式安装器
`bash napcat-linux-install.sh --skip-deps --github-proxy 0`（QQ 已装则自动跳过下载）。
`ldd /opt/QQ/qq` 无 `not found`。

## 二、服务地址 / 凭据

| 项目 | 值 |
|---|---|
| WebUI | `http://127.0.0.1:6099/webui?token=<WebUI token>` |
| WebUI token | `<WebUI token>`（在 `/opt/napcat/napcat/config/webui.json`） |
| OneBot v11 正向 WS | `ws://127.0.0.1:3001`（登录成功后监听） |
| WS token | `change-me-token` |
| 完整 WS 地址 | `ws://127.0.0.1:3001?access_token=change-me-token`（也支持 `Authorization: Bearer change-me-token`） |
| 反向 WS（备用，默认关闭） | `ws://127.0.0.1:3002`，token 同上；要启用把 config 里 `websocketClients` 对应项 `enable` 改 true |
| HTTP 服务 | 关闭（`httpServers: []`） |
| 进程监听 | 全部只绑 127.0.0.1（WebUI host 为 `::`，仅本机） |

## 三、扫码入口 / 二维码来源

1. **推荐**：手机浏览器/App 内打开 WebUI 登录页
   `http://127.0.0.1:6099/webui?token=<WebUI token>`，页面直接显示二维码，可刷新。
2. 二维码图片文件：`/opt/napcat/napcat/cache/qrcode.png`（每次刷新会覆盖）。
3. 日志里同时打印"二维码解码 URL"（`https://txz.qq.com/p?k=...`），可复制到二维码生成网站再扫：
   `grep -E "二维码解码URL" logs/napcat.log | tail -1`
4. 二维码有效期约 1–2 分钟；过期后刷新 WebUI 登录页，或重启 NapCat（`stop.sh && start.sh`）生成新码。

## 四、日志路径

- 统一日志：`/root/workspace/qqbot/protocol/logs/napcat.log`（NapCat + QQ 的 stdout/stderr）
- 清空日志：`> /root/workspace/qqbot/protocol/logs/napcat.log`（不影响进程）
- 扫码/登录/WS 关键行：`grep -E "二维码|登录|WebUi|Websocket|OneBot" logs/napcat.log`

## 五、常用命令

```bash
P=/root/workspace/qqbot/protocol

bash $P/start.sh      # 启动（幂等；已在运行则直接返回）；自动等 WebUI 就绪，打印 WebUI/QR 信息
bash $P/stop.sh       # 停止 QQ + Xvfb（SIGTERM→15s→SIGKILL），可重复执行
bash $P/status.sh     # 进程/端口/内存/token/登录状态/最新二维码/日志尾部
bash $P/apply-onebot-config.sh   # 登录成功后：写入 3001 WS 配置并重启 + 校验握手
bash $P/verify-ws.sh  # 手写 WebSocket 握手校验 127.0.0.1:3001（无需第三方库）
```

重启 NapCat（最简）：

```bash
bash /root/workspace/qqbot/protocol/stop.sh && bash /root/workspace/qqbot/protocol/start.sh
```

`start.sh` 会自动检测 `config/onebot11_<QQ号>.json`：存在则认为已登录过，用 `-q <QQ号>` 快速登录；
不存在则走扫码登录。

## 六、OneBot 配置

- 配置模板：`/opt/napcat/napcat/config/onebot11.json`（同时备份于 `protocol/onebot11.template.json`）
  - 首次登录时 NapCat 以它为模板生成 `onebot11_<QQ号>.json`，其中已带好 3001 WS + token。
- 账号配置：`/opt/napcat/napcat/config/onebot11_<QQ号>.json`（登录后生成）
- 其它相关：`config/webui.json`（WebUI 端口/token）、`config/napcat.json`
- 改完 JSON 必须重启 NapCat 生效；`apply-onebot-config.sh` 会自动处理（保留其它字段，只改 network）。
- 关键配置内容：

```json
{
  "network": {
    "httpServers": [],
    "websocketServers": [{
      "name": "ws", "enable": true, "host": "127.0.0.1", "port": 3001,
      "messagePostFormat": "array", "reportSelfMessage": false,
      "token": "change-me-token", "enableForcePushEvent": true,
      "debug": false, "heartInterval": 30000
    }],
    "websocketClients": [{ "name": "onebot-ws-reverse", "enable": false, "url": "ws://127.0.0.1:3002", "token": "change-me-token" }]
  }
}
```

## 七、已知问题 / proot 限制

1. **`ps aux` 不可用**：proot 下 `/proc/stat` 的 btime 不可读，会报
   `ps: Unable to get system boot time`（输出错乱）。这不是故障，用 `pgrep -af`、
   `/proc/<pid>/status` 或 `status.sh` 即可。
2. **无 systemd**：没有开机自启服务，需要外层启动脚本/手动调用 `start.sh`；进程用
   `setsid nohup` 脱离终端。手机重启或被杀进程后需重新 `start.sh`。
3. **WS 3001 在登录后才监听**：OneBot 网络适配器跟账号绑定，扫码登录成功（且配置就绪）后才会
   bind 3001；未登录时 6099 可用、3001 端口是 closed，属正常。
4. **Electron 必须 `--no-sandbox`**：launcher.sh 已带该参数；本机 proot 未发现需要额外
   `PROOT_NO_SECCOMP=1`（外层手机环境如遇崩溃再加）。
5. **Xvfb 由 `xvfb-run -a` 自动挑空闲 display**（本次是 `:99`），异常退出可能留下
   `/tmp/.X*-lock` 与 `/tmp/xvfb-run.*`；`stop.sh`/`start.sh` 已做清理。
6. **内存**：QQ 全进程 RSS 合计约 600–650MB（含 Electron 子进程，共享内存有重复计入），
   手机可用内存约 1.6GB，同时不要跑其它重型任务；如被 OOM 杀掉会出现在 `dmesg`/系统日志。
7. **二维码约 1–2 分钟过期**：以 WebUI 登录页为准（可刷新）；`qrcode.png` 只是最近一次快照。
8. **重启后需要重新登录**：已登录过时 `start.sh` 用 `-q <QQ号>` 快速登录（依赖 QQ 保存的登录态）；
   若快速登录失效，会回退成二维码，重新扫码即可。
9. 登录态/数据目录：`/root/.config/QQ/`（重装 QQ 前保留可免重新登录；本部署未清理过）。
10. 首次启动（冷缓存）WebUI 约 30–60s 起来；热启动 6s 左右。`start.sh` 最多等 180s。

## 八、部署进度（截至记录时间）

- [x] QQ 3.2.34 arm64 安装完成，`ldd` 无缺失
- [x] NapCat Shell 4.18.42 + launcher.so 编译安装完成
- [x] Xvfb + QQ + NapCat 常驻运行，WebUI 6099 可访问
- [x] start.sh / stop.sh / status.sh / apply-onebot-config.sh / verify-ws.sh 完成
- [x] OneBot 3001 WS 配置预置（登录后自动生效）
- [ ] 扫码登录（主线程 + 用户协作）
- [ ] 登录后验证 `ws://127.0.0.1:3001?access_token=change-me-token` 握手 101

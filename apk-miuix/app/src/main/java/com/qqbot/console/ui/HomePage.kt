package com.qqbot.console.ui

import android.content.Intent
import android.net.Uri
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.qqbot.console.ProotService
import com.qqbot.console.Store
import com.qqbot.console.patchOf
import kotlinx.coroutines.launch
import top.yukonga.miuix.kmp.basic.ButtonDefaults
import top.yukonga.miuix.kmp.basic.ScrollBehavior
import top.yukonga.miuix.kmp.basic.Text
import top.yukonga.miuix.kmp.basic.TextButton
import top.yukonga.miuix.kmp.preference.ArrowPreference
import top.yukonga.miuix.kmp.preference.SwitchPreference

@Composable
fun HomePage(padding: PaddingValues, scrollBehavior: ScrollBehavior) {
    val ctx = LocalContext.current
    val scope = rememberCoroutineScope()
    val bot = Store.obj("bot")
    val proto = Store.obj("protocol")
    val running = bot?.optBoolean("running") == true
    val connected = bot?.optBoolean("connected") == true
    val qqUp = proto?.optBoolean("qq_running") == true
    val wsOpen = proto?.optBoolean("ws_open") == true
    val needLogin = proto?.optBoolean("need_login") == true
    val webui = proto?.optStr("webui_url").orEmpty()

    // 后台希望服务在跑、但实际还没起来：多半是刚重启/被系统清理后正在恢复，给个提示免得用户反复点
    val desired = Store.obj("control", "desired")
    val wantBot = desired?.optStr("bot") == "running"
    val wantProtocol = desired?.optStr("protocol") == "running"
    val recovering = (wantBot && !running) || (wantProtocol && !qqUp)

    var confirmOff by remember { mutableStateOf(false) }

    val (title, sub, color) = when {
        running && connected -> Triple("运行中", "机器人已连接 QQ，正在工作", Good)
        needLogin -> Triple("需要登录", "QQ 还没登录，机器人收不到消息", Warn)
        running -> Triple("连接中", "机器人在运行，正在连接 QQ…", Warn)
        qqUp -> Triple("机器人已关闭", "QQ 在线，机器人没有运行", Warn)
        else -> Triple("已停止", "所有服务都没有运行", Bad)
    }

    PageList(padding, scrollBehavior) {
        group {
            BigStatus(title, sub, color)
        }

        if (recovering) {
            group("正在恢复") {
                Column(Modifier.fillMaxWidth().padding(16.dp)) {
                    Text(
                        "服务正在启动或自动恢复中，约 1–2 分钟，请稍候；不要重复点「开启服务」。",
                        fontSize = 14.sp,
                        color = Warn,
                    )
                }
            }
        }

        if (needLogin) {
            group("需要登录") {
                ArrowPreference(
                    title = "去登录 QQ",
                    summary = "打开 NapCat 登录页扫码或密码登录，只需一次",
                    onClick = {
                        if (webui.isNotBlank()) ctx.startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(webui)))
                    },
                )
            }
        }

        group("服务") {
            Row(
                Modifier.fillMaxWidth().padding(16.dp),
                horizontalArrangement = Arrangement.spacedBy(12.dp),
            ) {
                TextButton(
                    text = "开启服务",
                    modifier = Modifier.weight(1f),
                    colors = ButtonDefaults.textButtonColorsPrimary(),
                    onClick = { scope.launch { Store.action("all_start", "服务已开启"); Store.refresh() } },
                )
                TextButton(
                    text = "关闭机器人",
                    modifier = Modifier.weight(1f),
                    onClick = { scope.launch { Store.action("all_stop", "机器人已关闭，QQ 保持在线"); Store.refresh() } },
                )
            }
            ArrowPreference(
                title = "重启机器人",
                summary = "改了代码或卡住时用",
                onClick = { scope.launch { Store.action("bot_restart", "机器人已重启") } },
            )
            ArrowPreference(
                title = "重启 QQ 协议层",
                summary = "QQ 掉线时用，可能需要重新登录",
                onClick = { scope.launch { Store.action("protocol_restart", "协议层已重启") } },
            )
            ArrowPreference(
                title = "彻底关闭",
                summary = "连 QQ 一起停掉并退出后台，省电",
                onClick = { confirmOff = true },
            )
        }

        group("状态") {
            StatusRow("机器人", if (running) "运行中" else "已停止", if (running) Good else Bad)
            StatusRow("QQ 连接", if (connected) "已连接" else "未连接", if (connected) Good else Warn)
            StatusRow("QQ 进程", if (qqUp) "运行中" else "未运行", if (qqUp) Good else Bad)
            StatusRow("OneBot 通道", if (wsOpen) "已打开" else "未打开", if (wsOpen) Good else Warn)
            InfoRow("QQ 号", proto?.optStr("login_uin")?.ifBlank { "—" } ?: "—")
            InfoRow("运行时长", fmtUptime(bot?.optInt("uptime_seconds") ?: 0))
            val disk = Store.obj("disk")
            if (disk != null) InfoRow("存储剩余", "${disk.optDouble("free_gb")} GB")
        }

        group("自动守护") {
            ConfigSwitch("机器人挂了自动拉起", null, "control", "auto_restart_bot", default = true)
            ConfigSwitch("QQ 挂了自动拉起", null, "control", "auto_restart_protocol", default = true)
        }
        item { Hint("服务跑在这个 App 里，不依赖其它 App。熄屏会保持运行（会稍微费电）。") }
    }

    ConfirmDialog(
        show = confirmOff,
        title = "彻底关闭？",
        summary = "机器人和 QQ 都会停止。下次开启 QQ 可能要重新登录。",
        okText = "关闭",
        onDismiss = { confirmOff = false },
        onOk = {
            scope.launch {
                Store.action("all_off", "已彻底关闭")
                ProotService.stop(ctx)
            }
        },
    )
}

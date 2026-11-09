package com.qqbot.console.ui

import androidx.activity.compose.BackHandler
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.qqbot.console.Store
import com.qqbot.console.patchOf
import org.json.JSONObject
import top.yukonga.miuix.kmp.basic.ScrollBehavior
import top.yukonga.miuix.kmp.basic.Text
import top.yukonga.miuix.kmp.preference.ArrowPreference
import top.yukonga.miuix.kmp.theme.MiuixTheme

/** 官方 QQ 机器人通道：用 q.qq.com 的官方接口，不登个人号。 */
@Composable
fun OfficialBotPage(padding: PaddingValues, scrollBehavior: ScrollBehavior, onBack: () -> Unit) {
    val official = Store.obj("official") ?: JSONObject()
    val enabled = official.optBoolean("enabled", false)
    val bot = Store.obj("bot")
    val connected = bot?.optBoolean("connected") == true
    val selfId = bot?.optStr("self_id").orEmpty()

    BackHandler { onBack() }

    PageList(padding, scrollBehavior) {
        group {
            ArrowPreference(title = "← 返回更多", onClick = onBack)
        }

        group("官方机器人通道") {
            ConfigSwitch(
                "使用官方 QQ 机器人通道",
                "不走个人号（NapCat），改用 q.qq.com 的官方机器人接口",
                "official", "enabled", default = false,
            )
            EditPreference(
                "AppID",
                official.optStr("app_id"),
                summary = "在 q.qq.com 机器人资料页查看",
            ) { text ->
                Store.patch(patchOf("official", "app_id", value = text.trim()))
            }
            EditPreference(
                "AppSecret",
                official.optStr("app_secret"),
                summary = "官方机器人密钥；留空不修改",
                secret = true,
            ) { text ->
                if (text.isNotBlank()) {
                    Store.patch(patchOf("official", "app_secret", value = text.trim()), "AppSecret 已保存")
                }
            }
            ConfigSwitch(
                "沙箱环境",
                "开发调试时开；正式使用时关掉",
                "official", "sandbox", default = false,
            )
        }

        group("当前状态") {
            val (connText, connColor) = when {
                !enabled -> "官方通道未启用" to MiuixTheme.colorScheme.onSurfaceVariantSummary
                connected -> ("已连接" + (if (selfId.isBlank()) "" else "（$selfId）")) to Good
                else -> "未连接" to Warn
            }
            StatusRow("机器人连接", connText, connColor)
            InfoRow("当前通道", if (enabled) "官方 QQ 机器人" else "个人号（NapCat）")
            InfoRow("生效方式", "切换通道后需重启服务")
        }

        group("切换前必读") {
            Column(Modifier.fillMaxWidth().padding(16.dp)) {
                Text(
                    "官方通道不会封个人号，但有平台限制：",
                    fontSize = 14.sp,
                    color = Warn,
                )
                Text(
                    "· 只能被动回复（定时主动消息不可用）\n" +
                        "· 群里只能收到 @ 机器人的消息\n" +
                        "· 没有群成员 / 禁言 / 踢人 / 撤回接口",
                    fontSize = 13.sp,
                    color = MiuixTheme.colorScheme.onSurfaceVariantSummary,
                )
            }
        }

        item { Hint("需要先在 q.qq.com 注册机器人，把机器人拉进群，再填上面的 AppID / AppSecret。") }
        item { Hint("切换通道后到概览页点「彻底关闭」→「开启服务」，让新通道生效。") }
        item { Hint("官方通道使用机器人自己的 token 收发消息，不需要扫码登录 QQ；个人号相关设置（NapCat 登录）在官方通道下不生效。") }
    }
}

package com.qqbot.console.ui

import android.content.Intent
import android.net.Uri
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.lazy.LazyColumn
import top.yukonga.miuix.kmp.basic.BasicComponent
import top.yukonga.miuix.kmp.overlay.OverlayDialog
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.qqbot.console.Api
import com.qqbot.console.AppUpdates
import com.qqbot.console.BotUpdates
import com.qqbot.console.Env
import com.qqbot.console.Installer
import com.qqbot.console.Store
import com.qqbot.console.objects
import com.qqbot.console.patchOf
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import org.json.JSONArray
import org.json.JSONObject
import top.yukonga.miuix.kmp.basic.ScrollBehavior
import top.yukonga.miuix.kmp.basic.Text
import top.yukonga.miuix.kmp.preference.ArrowPreference
import top.yukonga.miuix.kmp.preference.SwitchPreference
import top.yukonga.miuix.kmp.theme.MiuixTheme

@Composable
fun MorePage(padding: PaddingValues, scrollBehavior: ScrollBehavior) {
    val ctx = LocalContext.current
    val scope = rememberCoroutineScope()
    var logs by remember { mutableStateOf<String?>(null) }
    var confirmReinstall by remember { mutableStateOf(false) }

    // ---------- 更新 ----------
    var autoCheck by remember { mutableStateOf(AppUpdates.autoCheckEnabled(ctx)) }
    var botCheck by remember { mutableStateOf<JSONObject?>(null) }
    var botChecking by remember { mutableStateOf(false) }
    var botUpdating by remember { mutableStateOf(false) }
    var botPhase by remember { mutableStateOf("") }
    var botMessage by remember { mutableStateOf("") }
    var botRestartRequired by remember { mutableStateOf(false) }

    fun botStatusText(): String {
        val phase = when (botPhase) {
            "checking" -> "检查中"
            "downloading" -> "下载中"
            "extracting" -> "解压中"
            "syncing" -> "正在同步"
            "done" -> "已完成"
            "failed" -> "失败"
            else -> if (botUpdating) "进行中" else ""
        }
        return listOf(phase, botMessage).filter { it.isNotBlank() }.joinToString(" · ")
    }

    fun botSummaryText(): String = when {
        botUpdating -> botStatusText().ifBlank { "正在更新…" }
        botChecking -> "检查中…"
        botPhase == "done" -> "已更新，重启服务后生效"
        botPhase == "failed" -> "上次更新失败，可重试"
        botCheck == null -> "点击检查并更新到最新代码"
        botCheck?.optBoolean("behind") == true -> "有新版本代码，点击更新"
        else -> "已是最新代码"
    }

    suspend fun refreshBotCheck() {
        botChecking = true
        val r = BotUpdates.check()
        botChecking = false
        if (r != null) botCheck = r
    }

    // 进入页面时查一次机器人代码有没有更新
    LaunchedEffect(Unit) { refreshBotCheck() }

    // 更新进行中时轮询状态
    LaunchedEffect(botUpdating) {
        if (!botUpdating) return@LaunchedEffect
        while (true) {
            delay(1500)
            val st = BotUpdates.status() ?: continue
            botPhase = st.optStr("phase")
            botMessage = st.optStr("message")
            botRestartRequired = st.optBoolean("restart_required")
            if (botPhase == "failed") {
                Store.toast = "更新失败：" + st.optStr("error").ifBlank { botMessage.ifBlank { "未知原因" } }
                break
            }
            if (botPhase == "done" || botPhase == "idle") {
                if (botPhase == "done") Store.toast = "机器人代码已更新，重启服务后生效"
                break
            }
        }
        // 结束后刷新一次检查结果，再结束轮询
        BotUpdates.check()?.let { botCheck = it }
        botUpdating = false
    }

    val kw = Store.cfg("plugins", "keyword") ?: JSONObject()
    val rules = kw.optJSONArray("rules")?.objects().orEmpty()
    val ig = Store.cfg("plugins", "image_gen") ?: JSONObject()

    PageList(padding, scrollBehavior) {
        group("关键词回复") {
            ConfigSwitch("启用", null, "plugins", "keyword", "enabled", default = true)
            ConfigSwitch("群里必须 @ 才回", null, "plugins", "keyword", "group_require_at", default = true)
            rules.forEachIndexed { i, r ->
                val words = r.optJSONArray("keywords")
                val wordText = (0 until (words?.length() ?: 0)).joinToString("，") { words!!.optString(it) }
                EditPreference(
                    title = r.optStr("name").ifBlank { "规则 ${i + 1}" },
                    summary = "关键词：$wordText",
                    current = r.optStr("reply"),
                    multiline = true,
                ) { reply ->
                    val arr = JSONArray()
                    rules.forEachIndexed { j, x -> arr.put(if (j == i) JSONObject(x.toString()).put("reply", reply) else x) }
                    Store.patch(patchOf("plugins", "keyword", "rules", value = arr))
                }
            }
        }
        item { Hint("增删规则、改匹配方式请用网页控制台。") }

        group("画图") {
            ConfigSwitch("启用", "说「画一张…」就生成图片发到群里", "plugins", "image_gen", "enabled")
            EditPreference("模型", ig.optStr("model")) {
                if (it.isNotBlank()) Store.patch(patchOf("plugins", "image_gen", "model", value = it))
            }
            EditPreference("接口地址", ig.optStr("base_url"), summary = "留空跟随 AI 接口") {
                Store.patch(patchOf("plugins", "image_gen", "base_url", value = it))
            }
            EditPreference("API Key", ig.optStr("api_key"), secret = true) {
                if (it.isNotBlank()) Store.patch(patchOf("plugins", "image_gen", "api_key", value = it))
            }
        }

        group("日志") {
            ArrowPreference(
                title = if (logs == null) "查看运行日志" else "刷新日志",
                onClick = {
                    scope.launch {
                        val arr = Api.get("/api/logs?lines=60")?.optJSONArray("lines")
                        logs = if (arr == null) "（读取失败）" else (0 until arr.length()).joinToString("\n") { arr.optString(it) }
                    }
                },
            )
            ArrowPreference(
                title = "清理日志",
                onClick = {
                    scope.launch {
                        val r = Api.post("/api/logs/clean")
                        Store.toast = "已清理 ${r?.optDouble("freed_mb") ?: 0.0} MB"
                    }
                },
            )
            logs?.let { text ->
                Column(Modifier.fillMaxWidth().padding(16.dp)) {
                    Text(
                        text.takeLast(6000),
                        fontSize = 11.sp,
                        fontFamily = FontFamily.Monospace,
                        color = MiuixTheme.colorScheme.onSurfaceVariantSummary,
                    )
                }
            }
        }

        group("更新") {
            ArrowPreference(
                title = if (AppUpdates.checking) "正在检查更新…" else "检查更新",
                summary = "当前版本 ${AppUpdates.currentVersion}",
                onClick = { scope.launch { AppUpdates.checkAndPrompt() } },
            )
            SwitchPreference(
                title = "自动检查更新",
                summary = "每天最多检查一次，有新版时提醒",
                checked = autoCheck,
                onCheckedChange = { on ->
                    autoCheck = on
                    AppUpdates.setAutoCheck(ctx, on)
                },
            )
            ArrowPreference(
                title = "更新机器人代码",
                summary = botSummaryText(),
                onClick = {
                    scope.launch {
                        if (botUpdating) {
                            Store.toast = "正在更新，请稍候"
                            return@launch
                        }
                        val c = BotUpdates.check()
                        if (c == null) {
                            Store.toast = "检查失败：管理服务没有响应"
                            return@launch
                        }
                        botCheck = c
                        if (!c.optBoolean("behind")) {
                            Store.toast = "机器人代码已是最新"
                            return@launch
                        }
                        val err = BotUpdates.start()
                        if (err.isBlank()) {
                            botRestartRequired = false
                            botPhase = "checking"
                            botMessage = "正在开始更新…"
                            botUpdating = true
                        } else {
                            Store.toast = "更新没能开始：$err"
                        }
                    }
                },
            )
            if (botUpdating || botPhase == "done" || botPhase == "failed") {
                InfoRow("更新状态", botStatusText().ifBlank { "—" })
            }
            if (botRestartRequired || botPhase == "done") {
                ArrowPreference(
                    title = "重启服务",
                    summary = "让更新后的机器人代码生效",
                    onClick = { scope.launch { Store.action("bot_restart", "机器人已重启，更新已生效") } },
                )
            }
        }
        if (botPhase == "done") {
            item { Hint("机器人代码已经更新。重启服务后新代码生效；如果更新了管理台，重启 App 里的服务才会生效。") }
        }

        group("其它") {
            ArrowPreference(
                title = "网页控制台",
                summary = "完整设置：规则增删、多接口、画图等",
                onClick = { ctx.startActivity(Intent(Intent.ACTION_VIEW, Uri.parse("http://127.0.0.1:8080/"))) },
            )
            ArrowPreference(
                title = "App 运行日志",
                summary = Env.PUBLIC_LOG.absolutePath,
            )
            ArrowPreference(
                title = "重新安装运行环境",
                summary = "环境损坏时用，会覆盖现有环境",
                onClick = { confirmReinstall = true },
            )
        }
    }

    ConfirmDialog(
        show = confirmReinstall,
        title = "重新安装运行环境？",
        summary = "会停止服务并重新解压安装包。机器人配置和 QQ 登录状态以安装包里的为准。",
        okText = "重新安装",
        onDismiss = { confirmReinstall = false },
        onOk = {
            com.qqbot.console.ProotService.stop(ctx)
            Installer.reinstall()
        },
    )
}

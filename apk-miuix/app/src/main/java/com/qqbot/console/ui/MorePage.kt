package com.qqbot.console.ui

import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.PowerManager
import android.provider.Settings
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
import com.qqbot.console.AccountsStore
import com.qqbot.console.Api
import com.qqbot.console.AppUpdates
import com.qqbot.console.BotUpdates
import com.qqbot.console.Env
import com.qqbot.console.Installer
import com.qqbot.console.ProotService
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
import top.yukonga.miuix.kmp.preference.OverlayDropdownPreference
import top.yukonga.miuix.kmp.preference.SwitchPreference
import top.yukonga.miuix.kmp.theme.MiuixTheme

@Composable
fun MorePage(padding: PaddingValues, scrollBehavior: ScrollBehavior) {
    val ctx = LocalContext.current
    val scope = rememberCoroutineScope()
    var logs by remember { mutableStateOf<String?>(null) }
    var confirmReinstall by remember { mutableStateOf(false) }
    var showPython by remember { mutableStateOf(false) }
    var showKeywords by remember { mutableStateOf(false) }
    var showPlugins by remember { mutableStateOf(false) }
    var showTasks by remember { mutableStateOf(false) }
    var showSkills by remember { mutableStateOf(false) }
    var showScheduled by remember { mutableStateOf(false) }
    var showSafety by remember { mutableStateOf(false) }
    var showOfficial by remember { mutableStateOf(false) }

    // 全屏管理页：提前返回，页面自己带「返回更多」
    if (showKeywords) {
        KeywordManagerPage(padding, scrollBehavior) { showKeywords = false }
        return
    }
    if (showPlugins) {
        PluginManagerPage(padding, scrollBehavior) { showPlugins = false }
        return
    }
    if (showTasks) {
        TaskManagerPage(padding, scrollBehavior) { showTasks = false }
        return
    }
    if (showSkills) {
        SkillManagerPage(padding, scrollBehavior) { showSkills = false }
        return
    }
    if (showScheduled) {
        ScheduledMessagePage(padding, scrollBehavior) { showScheduled = false }
        return
    }
    if (showSafety) {
        SafetyPage(padding, scrollBehavior) { showSafety = false }
        return
    }
    if (showOfficial) {
        OfficialBotPage(padding, scrollBehavior) { showOfficial = false }
        return
    }

    // ---------- 后台运行 ----------
    var keepAlive by remember { mutableStateOf(ProotService.keepAliveEnabled(ctx)) }
    // 每次重组都读一次，用户从系统设置回来时能立刻看到新状态（调用很轻）
    val batteryIgnored = isIgnoringBatteryOptimizations(ctx)

    // 画图账号要用到 ChatGPT 账号的连接状态
    LaunchedEffect(Unit) { AccountsStore.refresh() }

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

            val drawAuth = ig.optStr("auth")
            val chatgptAccount = AccountsStore.find("chatgpt")
            val useChatgpt = drawAuth == "chatgpt"
            val apiModels = listOf("gpt-image-1", "dall-e-3", "dall-e-2")
            val modelOptions = if (useChatgpt) listOf("gpt-image-2") else apiModels
            val currentModel = ig.optStr("model").ifBlank { modelOptions.first() }

            OverlayDropdownPreference(
                title = "画图账号",
                summary = when {
                    !useChatgpt -> "不使用账号（用图片 API Key）"
                    chatgptAccount.signedIn ->
                        "ChatGPT 账号" + if (chatgptAccount.account.isBlank()) "" else "（${chatgptAccount.account}）"
                    else -> "ChatGPT 账号 · 未连接，先去 AI 账号连接"
                },
                items = listOf("不使用（用图片 API Key）", "ChatGPT 账号"),
                selectedIndex = if (useChatgpt) 1 else 0,
                onSelectedIndexChange = { i ->
                    val toChatgpt = i == 1
                    val model = if (toChatgpt) {
                        "gpt-image-2"
                    } else {
                        ig.optStr("model").takeIf { it in apiModels } ?: "gpt-image-1"
                    }
                    val values = JSONObject()
                        .put("auth", if (toChatgpt) "chatgpt" else "")
                        .put("model", model)
                    scope.launch {
                        Store.patch(
                            patchOf("plugins", "image_gen", value = values),
                            if (toChatgpt) "画图改用 ChatGPT 账号" else "画图改用图片 API Key",
                        )
                    }
                },
            )

            OverlayDropdownPreference(
                title = "模型",
                summary = currentModel,
                items = modelOptions,
                selectedIndex = modelOptions.indexOf(currentModel).coerceAtLeast(0),
                onSelectedIndexChange = { i ->
                    scope.launch {
                        Store.patch(patchOf("plugins", "image_gen", "model", value = modelOptions[i]), "画图模型已改为 ${modelOptions[i]}")
                    }
                },
            )
            EditPreference("手动填写模型名", ig.optStr("model"), summary = "下拉里没有的模型可以在这里填") {
                if (it.isNotBlank()) Store.patch(patchOf("plugins", "image_gen", "model", value = it))
            }

            EditPreference(
                "接口地址",
                ig.optStr("base_url"),
                summary = if (useChatgpt) "留空 = OpenAI 官方；账号模式下不需要填" else "留空 = OpenAI 官方",
            ) {
                Store.patch(patchOf("plugins", "image_gen", "base_url", value = it))
            }
            EditPreference("API Key", ig.optStr("api_key"), secret = true) {
                if (it.isNotBlank()) Store.patch(patchOf("plugins", "image_gen", "api_key", value = it))
            }
        }
        item { Hint("画图账号和主对话账号独立：主对话可以用 Claude，画图用 ChatGPT 账号。账号模式需先在 AI 账号连接 ChatGPT。") }

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

        group("后台运行") {
            val serviceRunning = ProotService.isRunning()
            ArrowPreference(
                title = "服务状态",
                summary = if (serviceRunning) "运行中" else "已停止 · 点一下立即拉起服务",
                onClick = {
                    if (ProotService.isWanted(ctx)) ProotService.ensureRunning(ctx) else ProotService.start(ctx)
                    Store.toast = "正在拉起服务，请稍候…"
                },
            )
            SwitchPreference(
                title = "后台常驻（关掉 App 也继续跑）",
                summary = "关掉界面后机器人继续运行，通知栏保留「机器人控制台」",
                checked = keepAlive,
                onCheckedChange = { on ->
                    keepAlive = on
                    ProotService.setKeepAlive(ctx, on)
                    if (on) ProotService.scheduleKeepAlive(ctx) else ProotService.cancelKeepAlive(ctx)
                    Store.toast = if (on) "已开启后台常驻" else "已关闭后台常驻"
                },
            )
            ArrowPreference(
                title = "电池优化",
                summary = if (batteryIgnored) "已忽略电池优化，不会被省电限制" else "未忽略，点一下去设置",
                onClick = { requestIgnoreBatteryOptimizations(ctx) },
            )
            ArrowPreference(
                title = "小米自启动 / 省电策略",
                summary = "MIUI 需允许自启动，省电策略设为「无限制」",
                onClick = { openAutoStartSettings(ctx) },
            )
        }
        item { Hint("关掉 App 后通知栏会保留「机器人控制台」常驻通知，服务继续在后台运行。") }
        item { Hint("小米 / 红米：需要允许自启动，并把省电策略设为「无限制」，否则后台可能被系统清理。") }
        item { Hint("在最近任务里上滑会清理 App；可以在最近任务里长按本 App 锁定，防止被清掉。") }

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

        group("工具") {
            ArrowPreference(
                title = "关键词管理",
                summary = "关键词回复规则增删改、联网搜索 / 画图触发词",
                onClick = { showKeywords = true },
            )
            ArrowPreference(
                title = "插件管理",
                summary = "开关各插件，配置 Python 工具（是否让 AI 自己运行 / 超时）",
                onClick = { showPlugins = true },
            )
            ArrowPreference(
                title = "任务（终端）",
                summary = "在机器人运行环境里执行命令，看输出 / 停止",
                onClick = { showTasks = true },
            )
            ArrowPreference(
                title = "技能",
                summary = "管理 AI 技能（Markdown 文件），可新建 / 编辑 / 删除",
                onClick = { showSkills = true },
            )
            ArrowPreference(
                title = "定时消息",
                summary = "每隔一段时间自动发一条问候（语言模型 + 画图模型）",
                onClick = { showScheduled = true },
            )
            ArrowPreference(
                title = "防封模式",
                summary = "随机延迟、发送限频、群白名单、隐藏链接、敏感词",
                onClick = { showSafety = true },
            )
            ArrowPreference(
                title = "官方机器人",
                summary = "用 q.qq.com 官方机器人通道（不登个人号），注意平台限制",
                onClick = { showOfficial = true },
            )
            ArrowPreference(
                title = "运行 Python 代码",
                summary = "在机器人运行环境里跑一段临时脚本，看输出 / 错误",
                onClick = { showPython = true },
            )
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

    if (showPython) {
        PythonRunnerDialog(onDismiss = { showPython = false })
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

// ============================================================ 后台运行相关的小工具

private fun isIgnoringBatteryOptimizations(ctx: Context): Boolean = try {
    val pm = ctx.getSystemService(PowerManager::class.java)
    pm?.isIgnoringBatteryOptimizations(ctx.packageName) == true
} catch (_: Exception) {
    false
}

private fun requestIgnoreBatteryOptimizations(ctx: Context) {
    val pkg = ctx.packageName
    try {
        ctx.startActivity(
            Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS, Uri.parse("package:$pkg"))
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK),
        )
        return
    } catch (_: Exception) {
    }
    try {
        ctx.startActivity(Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
        Store.toast = "在列表里把「机器人控制台」设为不限制"
    } catch (e: Exception) {
        Store.toast = "打不开电池优化设置：${e.message ?: "系统不支持"}"
    }
}

/** MIUI 的自启动 / 省电策略入口；没有就退回应用详情页。 */
private fun openAutoStartSettings(ctx: Context) {
    val pkg = ctx.packageName
    val candidates = listOf(
        Intent("miui.intent.action.OP_AUTO_START").putExtra("extra_pkgname", pkg).putExtra("package", pkg),
        Intent("miui.intent.action.APP_PERM_EDITOR").putExtra("extra_pkgname", pkg).putExtra("package", pkg),
    )
    for (intent in candidates) {
        try {
            ctx.startActivity(intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
            Store.toast = "在设置里允许自启动，并把省电策略设为「无限制」"
            return
        } catch (_: Exception) {
            // 不是 MIUI 或入口不存在，试下一个
        }
    }
    try {
        ctx.startActivity(
            Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS, Uri.parse("package:$pkg"))
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK),
        )
        Store.toast = "在应用详情里找到自启动 / 省电策略并设为允许"
    } catch (e: Exception) {
        Store.toast = "打不开应用设置：${e.message ?: "系统不支持"}"
    }
}

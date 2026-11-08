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
import com.qqbot.console.Env
import com.qqbot.console.Installer
import com.qqbot.console.Store
import com.qqbot.console.objects
import com.qqbot.console.patchOf
import kotlinx.coroutines.launch
import org.json.JSONArray
import org.json.JSONObject
import top.yukonga.miuix.kmp.basic.ScrollBehavior
import top.yukonga.miuix.kmp.basic.Text
import top.yukonga.miuix.kmp.preference.ArrowPreference
import top.yukonga.miuix.kmp.theme.MiuixTheme

@Composable
fun MorePage(padding: PaddingValues, scrollBehavior: ScrollBehavior) {
    val ctx = LocalContext.current
    val scope = rememberCoroutineScope()
    var logs by remember { mutableStateOf<String?>(null) }
    var sessions by remember { mutableStateOf<List<JSONObject>?>(null) }
    var showSessions by remember { mutableStateOf(false) }
    var detailSession by remember { mutableStateOf<JSONObject?>(null) }
    var confirmReinstall by remember { mutableStateOf(false) }
    var confirmClearContext by remember { mutableStateOf(false) }

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

        group("对话记忆") {
            ArrowPreference(
                title = "查看最近的对话",
                summary = when {
                    sessions == null -> "点开查看每个会话的完整内容"
                    sessions!!.isEmpty() -> "暂无对话"
                    else -> "${sessions!!.size} 个会话，点开看全文"
                },
                onClick = {
                    scope.launch {
                        sessions = Api.get("/api/context")?.optJSONArray("sessions")?.objects().orEmpty()
                        if (sessions!!.isEmpty()) Store.toast = "还没有对话记录" else showSessions = true
                    }
                },
            )
            ArrowPreference(
                title = "清空所有对话记忆",
                summary = "清空后它会忘记之前的聊天",
                onClick = { confirmClearContext = true },
            )
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

    ConfirmDialog(
        show = confirmClearContext,
        title = "清空所有对话记忆？",
        summary = "它会忘掉所有群和私聊里聊过的内容，不可恢复。",
        okText = "清空",
        onDismiss = { confirmClearContext = false },
        onOk = {
            scope.launch {
                Api.post("/api/context/clear")
                sessions = emptyList()
                Store.toast = "已清空对话记忆"
            }
        },
    )

    // 会话列表
    SessionListDialog(
        show = showSessions && detailSession == null,
        sessions = sessions.orEmpty(),
        onDismiss = { showSessions = false },
        onPick = { detailSession = it },
    )

    // 单个会话的完整内容
    SessionDetailDialog(
        session = detailSession,
        onDismiss = { detailSession = null },
    )
}

/** 会话标题：群 1048589141 · 2153654562 / 私聊 2153654562 */
private fun sessionTitle(key: String): String = when {
    key.startsWith("group") -> {
        val rest = key.removePrefix("group")
        "群 " + rest.substringBefore(":") + " · " + rest.substringAfter(":", "?")
    }
    key.startsWith("private") -> "私聊 " + key.substringAfter(":")
    else -> key
}

private fun fmtTime(sec: Double): String {
    if (sec <= 0) return ""
    return java.text.SimpleDateFormat("MM-dd HH:mm", java.util.Locale.CHINA)
        .format(java.util.Date((sec * 1000).toLong()))
}

@Composable
private fun SessionListDialog(
    show: Boolean,
    sessions: List<JSONObject>,
    onDismiss: () -> Unit,
    onPick: (JSONObject) -> Unit,
) {
    OverlayDialog(show = show, title = "对话记忆", onDismissRequest = onDismiss) {
        LazyColumn(Modifier.fillMaxWidth().heightIn(max = 380.dp)) {
            items(sessions.size) { i ->
                val s = sessions[i]
                val msgs = s.optJSONArray("messages")?.objects().orEmpty()
                BasicComponent(
                    title = sessionTitle(s.optStr("key")),
                    summary = "${msgs.size} 条 · " + fmtTime(s.optDouble("updated_at", 0.0)),
                    onClick = { onPick(s) },
                )
            }
        }
        Spacer(Modifier.height(12.dp))
        DialogButtons(onCancel = onDismiss, onOk = onDismiss, okText = "关闭")
    }
}

@Composable
private fun SessionDetailDialog(session: JSONObject?, onDismiss: () -> Unit) {
    if (session == null) return
    val msgs = session.optJSONArray("messages")?.objects().orEmpty()
    val botName = Store.str("config", "bot", "name").ifBlank { "机器人" }
    OverlayDialog(show = true, title = sessionTitle(session.optStr("key")), onDismissRequest = onDismiss) {
        LazyColumn(Modifier.fillMaxWidth().heightIn(max = 420.dp)) {
            items(msgs.size) { i ->
                val m = msgs[i]
                val isUser = m.optStr("role") == "user"
                Column(Modifier.fillMaxWidth().padding(vertical = 6.dp)) {
                    Text(
                        if (isUser) "对方" else botName,
                        fontSize = 12.sp,
                        color = if (isUser) MiuixTheme.colorScheme.onSurfaceVariantSummary else MiuixTheme.colorScheme.primary,
                    )
                    Text(m.optStr("content"), fontSize = 14.sp)
                }
            }
            if (msgs.isEmpty()) item { Text("这个会话没有内容", fontSize = 14.sp) }
        }
        Spacer(Modifier.height(12.dp))
        DialogButtons(onCancel = onDismiss, onOk = onDismiss, okText = "关闭")
    }
}

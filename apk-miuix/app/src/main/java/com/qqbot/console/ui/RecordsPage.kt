package com.qqbot.console.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.qqbot.console.Api
import com.qqbot.console.Store
import com.qqbot.console.objects
import kotlinx.coroutines.launch
import org.json.JSONObject
import top.yukonga.miuix.kmp.basic.BasicComponent
import top.yukonga.miuix.kmp.basic.ScrollBehavior
import top.yukonga.miuix.kmp.basic.Text
import top.yukonga.miuix.kmp.basic.TextField
import top.yukonga.miuix.kmp.overlay.OverlayDialog
import top.yukonga.miuix.kmp.preference.ArrowPreference
import top.yukonga.miuix.kmp.theme.MiuixTheme

private val COUNT_OPTIONS = listOf("20 条", "50 条", "100 条", "200 条", "300 条")
private val COUNT_VALUES = listOf(20, 50, 100, 200, 300)

@Composable
fun RecordsPage(padding: PaddingValues, scrollBehavior: ScrollBehavior) {
    val scope = rememberCoroutineScope()

    // ---- 群消息 ----
    var groups by remember { mutableStateOf<List<JSONObject>>(emptyList()) }
    var groupIndex by remember { mutableIntStateOf(0) }
    var countIndex by remember { mutableIntStateOf(1) }
    var messages by remember { mutableStateOf<List<JSONObject>?>(null) }
    var loadingMsg by remember { mutableStateOf(false) }
    var showMsgs by remember { mutableStateOf(false) }

    // ---- 文件 ----
    var files by remember { mutableStateOf<List<JSONObject>?>(null) }
    var openFile by remember { mutableStateOf<JSONObject?>(null) }
    var fileText by remember { mutableStateOf("") }
    var fileLoading by remember { mutableStateOf(false) }

    // ---- 上下文 ----
    var sessions by remember { mutableStateOf<List<JSONObject>?>(null) }
    var showSessions by remember { mutableStateOf(false) }
    var detailSession by remember { mutableStateOf<JSONObject?>(null) }
    var confirmClear by remember { mutableStateOf(false) }

    fun loadGroups() {
        scope.launch {
            val g = Api.get("/api/groups")?.optJSONArray("groups")?.objects().orEmpty()
            groups = g
            if (g.isEmpty()) Store.toast = "读不到群列表（QQ 可能没登录）"
        }
    }

    fun loadMessages() {
        val g = groups.getOrNull(groupIndex) ?: return
        val gid = g.opt("id") ?: return
        val count = COUNT_VALUES[countIndex]
        loadingMsg = true
        scope.launch {
            val r = Api.get("/api/messages?group=$gid&count=$count")
            val list = r?.optJSONArray("messages")?.objects()
            loadingMsg = false
            if (list == null) {
                Store.toast = "读取失败：QQ 可能没登录"
            } else {
                messages = list
                showMsgs = true
            }
        }
    }

    fun loadFiles() {
        scope.launch {
            files = Api.get("/api/files")?.optJSONArray("files")?.objects().orEmpty()
        }
    }

    LaunchedEffect(Unit) {
        if (groups.isEmpty()) loadGroups()
    }

    PageList(padding, scrollBehavior) {
        group("群内消息") {
            if (groups.isEmpty()) {
                ItemRow("群列表", "点这里加载") { loadGroups() }
            } else {
                GlassSelectRow(
                    title = "选择群",
                    items = groups.map { it.optStr("name") },
                    selectedIndex = groupIndex.coerceIn(0, groups.size - 1),
                    onSelectedIndexChange = { groupIndex = it },
                )
            }
            GlassSelectRow(
                title = "读取多少条",
                items = COUNT_OPTIONS,
                selectedIndex = countIndex,
                onSelectedIndexChange = { countIndex = it },
            )
            Row(Modifier.fillMaxWidth().padding(16.dp), horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                GlassButton(text = if (loadingMsg) "读取中…" else "读取消息", onClick = { loadMessages() }, modifier = Modifier.weight(1f), primary = true, enabled = groups.isNotEmpty() && !loadingMsg)
                GlassButton(text = "刷新群列表", onClick = { loadGroups() }, modifier = Modifier.weight(1f), primary = false)
            }
        }

        group("机器人写的文件") {
            Row(Modifier.fillMaxWidth().padding(16.dp), horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                GlassButton(text = "刷新文件列表", onClick = { loadFiles() }, modifier = Modifier.weight(1f), primary = true)
            }
            val list = files
            when {
                list == null -> ItemRow("文件", "点「刷新文件列表」加载") { loadFiles() }
                list.isEmpty() -> ItemRow("文件", "还没有生成过文件") { }
                else -> list.take(30).forEach { f ->
                    val name = f.optStr("name")
                    val size = f.optLong("size")
                    ItemRow(name, humanSize(size) + " · " + fmtClock(f.optDouble("mtime", 0.0))) {
                        openFile = f
                        fileText = ""
                        fileLoading = true
                        scope.launch {
                            val r = Api.get("/api/files/get?name=" + java.net.URLEncoder.encode(name, "UTF-8"))
                            fileLoading = false
                            if (r?.optBoolean("ok") == true) {
                                fileText = if (r.optBoolean("binary")) "（二进制文件，无法预览）" else r.optStr("text")
                            } else {
                                fileText = "读取失败：" + (r?.optStr("error") ?: "无响应")
                            }
                        }
                    }
                }
            }
        }

        group("AI 上下文（对话记忆）") {
            ItemRow(
                "查看最近的对话",
                when {
                    sessions == null -> "点开查看完整内容"
                    sessions!!.isEmpty() -> "还没有对话"
                    else -> "${sessions!!.size} 个会话，点开看全文"
                },
            ) {
                scope.launch {
                    sessions = Api.get("/api/context")?.optJSONArray("sessions")?.objects().orEmpty()
                    if (sessions!!.isEmpty()) Store.toast = "还没有对话记录" else showSessions = true
                }
            }
            ItemRow("清空所有对话记忆", "清空后它会忘记之前的聊天") { confirmClear = true }
        }

        item {
            Text(
                "群消息和文件都来自手机上正在运行的机器人服务。",
                fontSize = 13.sp,
                color = MiuixTheme.colorScheme.onSurfaceVariantSummary,
                modifier = Modifier.padding(horizontal = 24.dp, vertical = 8.dp),
            )
        }
    }

    // 群消息详情
    MsgDialog(
        show = showMsgs && messages != null,
        title = groups.getOrNull(groupIndex)?.optStr("name") ?: "群消息",
        messages = messages.orEmpty(),
        onDismiss = { showMsgs = false },
    )

    // 文件内容
    if (openFile != null) {
        GlassDialog(
            show = true,
            title = openFile!!.optStr("name"),
            onDismissRequest = { openFile = null },
        ) {
            if (fileLoading) {
                Text("读取中…", fontSize = 14.sp)
            } else {
                LazyColumn(Modifier.fillMaxWidth().heightIn(max = 420.dp)) {
                    item {
                        Text(
                            fileText.ifBlank { "（空文件）" },
                            fontSize = 11.sp,
                            fontFamily = FontFamily.Monospace,
                        )
                    }
                }
            }
            Spacer(Modifier.height(12.dp))
            Row(horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                GlassButton(text = "关闭", onClick = { openFile = null }, modifier = Modifier.weight(1f), primary = false)
            }
        }
    }

    SessionListDialog(
        show = showSessions && detailSession == null,
        sessions = sessions.orEmpty(),
        onDismiss = { showSessions = false },
        onPick = { detailSession = it },
    )
    SessionDetailDialog(session = detailSession, onDismiss = { detailSession = null })

    ConfirmDialog(
        show = confirmClear,
        title = "清空所有对话记忆？",
        summary = "它会忘掉所有群和私聊里聊过的内容，不可恢复。",
        okText = "清空",
        onDismiss = { confirmClear = false },
        onOk = {
            scope.launch {
                Api.post("/api/context/clear")
                sessions = emptyList()
                Store.toast = "已清空对话记忆"
            }
        },
    )
}

@Composable
private fun ItemRow(title: String, summary: String, onClick: () -> Unit) {
    ArrowPreference(title = title, summary = summary, onClick = onClick)
}

@Composable
private fun MsgDialog(
    show: Boolean,
    title: String,
    messages: List<JSONObject>,
    onDismiss: () -> Unit,
) {
    GlassDialog(show = show, title = title, summary = "${messages.size} 条", onDismissRequest = onDismiss) {
        if (messages.isEmpty()) {
            Text("这个群最近没有读到消息", fontSize = 14.sp)
        } else {
            LazyColumn(Modifier.fillMaxWidth().heightIn(max = 460.dp)) {
                items(messages.size) { i ->
                    val m = messages[i]
                    Column(Modifier.fillMaxWidth().padding(vertical = 5.dp)) {
                        Row(verticalAlignment = Alignment.CenterVertically) {
                            Text(
                                fmtClock(m.optDouble("time", 0.0)),
                                fontSize = 11.sp,
                                color = MiuixTheme.colorScheme.onSurfaceVariantSummary,
                            )
                            Spacer(Modifier.padding(start = 8.dp))
                            Text(
                                m.optStr("name"),
                                fontSize = 12.sp,
                                color = MiuixTheme.colorScheme.primary,
                            )
                        }
                        val body = m.optStr("text")
                        Text(body.ifBlank { "（图片或卡片消息）" }, fontSize = 14.sp)
                    }
                }
            }
        }
        Spacer(Modifier.height(12.dp))
        Row(horizontalArrangement = Arrangement.spacedBy(12.dp)) {
            GlassButton(text = "关闭", onClick = onDismiss, modifier = Modifier.weight(1f), primary = false)
        }
    }
}

fun humanSize(bytes: Long): String = when {
    bytes < 1024 -> "$bytes B"
    bytes < 1024 * 1024 -> "${bytes / 1024} KB"
    else -> String.format("%.1f MB", bytes / 1048576.0)
}

fun fmtClock(sec: Double): String {
    if (sec <= 0) return ""
    val fmt = if (System.currentTimeMillis() / 1000 - sec < 86400) "HH:mm" else "MM-dd HH:mm"
    return java.text.SimpleDateFormat(fmt, java.util.Locale.CHINA).format(java.util.Date((sec * 1000).toLong()))
}

fun sessionTitle(key: String): String = when {
    key.startsWith("group") -> {
        val rest = key.removePrefix("group")
        "群 " + rest.substringBefore(":") + " · " + rest.substringAfter(":", "?")
    }
    key.startsWith("private") -> "私聊 " + key.substringAfter(":")
    else -> key
}

@Composable
fun SessionListDialog(
    show: Boolean,
    sessions: List<JSONObject>,
    onDismiss: () -> Unit,
    onPick: (JSONObject) -> Unit,
) {
    GlassDialog(show = show, title = "对话记忆", onDismissRequest = onDismiss) {
        LazyColumn(Modifier.fillMaxWidth().heightIn(max = 380.dp)) {
            items(sessions.size) { i ->
                val s = sessions[i]
                val msgs = s.optJSONArray("messages")?.objects().orEmpty()
                BasicComponent(
                    title = sessionTitle(s.optStr("key")),
                    summary = "${msgs.size} 条 · " + fmtClock(s.optDouble("updated_at", 0.0)),
                    onClick = { onPick(s) },
                )
            }
        }
        Spacer(Modifier.height(12.dp))
        DialogButtons(onCancel = onDismiss, onOk = onDismiss, okText = "关闭")
    }
}

@Composable
fun SessionDetailDialog(session: JSONObject?, onDismiss: () -> Unit) {
    if (session == null) return
    val msgs = session.optJSONArray("messages")?.objects().orEmpty()
    val botName = Store.str("config", "bot", "name").ifBlank { "机器人" }
    GlassDialog(show = true, title = sessionTitle(session.optStr("key")), onDismissRequest = onDismiss) {
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

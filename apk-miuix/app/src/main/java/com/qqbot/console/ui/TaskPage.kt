package com.qqbot.console.ui

import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import androidx.activity.compose.BackHandler
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.qqbot.console.Api
import com.qqbot.console.Store
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import org.json.JSONObject
import java.net.URLEncoder
import top.yukonga.miuix.kmp.basic.ButtonDefaults
import top.yukonga.miuix.kmp.basic.InfiniteProgressIndicator
import top.yukonga.miuix.kmp.basic.ScrollBehavior
import top.yukonga.miuix.kmp.basic.Text
import top.yukonga.miuix.kmp.basic.TextButton
import top.yukonga.miuix.kmp.basic.TextField
import top.yukonga.miuix.kmp.overlay.OverlayDialog
import top.yukonga.miuix.kmp.preference.ArrowPreference
import top.yukonga.miuix.kmp.theme.MiuixTheme

// ============================================================ 任务（终端）

private val TASK_TIMEOUT_DEFAULT = 300

private fun taskStatusText(status: String): String = when (status) {
    "running" -> "运行中"
    "done" -> "已完成"
    "failed" -> "失败"
    "stopped" -> "已停止"
    else -> if (status.isBlank()) "未知" else status
}

private fun taskStatusColor(status: String): Color = when (status) {
    "running" -> Warn
    "done" -> Good
    "failed", "stopped" -> Bad
    else -> Color(0xFF8E8E93)   // 未知状态用中和的灰色
}

private fun fmtDuration(ms: Long): String = when {
    ms <= 0 -> "—"
    ms < 1000 -> "${ms} 毫秒"
    ms < 60_000 -> String.format(java.util.Locale.US, "%.1f 秒", ms / 1000.0)
    else -> "${ms / 60000} 分 ${(ms % 60000) / 1000} 秒"
}

private fun taskCommandSummary(command: String): String {
    val oneLine = command.replace("\n", " ").trim()
    return if (oneLine.length <= 42) oneLine else oneLine.take(42) + "…"
}

/** 任务（终端）：在机器人运行环境里执行命令，看输出 / 停止。 */
@Composable
fun TaskManagerPage(padding: PaddingValues, scrollBehavior: ScrollBehavior, onBack: () -> Unit) {
    val ctx = LocalContext.current
    val scope = rememberCoroutineScope()

    var tasks by remember { mutableStateOf<List<JSONObject>>(emptyList()) }
    var error by remember { mutableStateOf("") }
    var command by remember { mutableStateOf("") }
    var timeoutText by remember { mutableStateOf(TASK_TIMEOUT_DEFAULT.toString()) }
    var starting by remember { mutableStateOf(false) }
    var detail by remember { mutableStateOf<JSONObject?>(null) }

    suspend fun refreshTasks() {
        val r = Api.get("/api/tasks")
        if (r == null) {
            error = "管理服务没有响应"
            return
        }
        if (!r.optBoolean("ok")) {
            error = r.optStr("error").ifBlank { "读取任务列表失败" }
            return
        }
        error = ""
        val arr = r.optJSONArray("tasks")
        tasks = (0 until (arr?.length() ?: 0)).mapNotNull { arr?.optJSONObject(it) }
    }

    suspend fun refreshDetail(id: String) {
        if (id.isBlank()) return
        val r = Api.get("/api/tasks/detail?id=" + URLEncoder.encode(id, "UTF-8")) ?: return
        if (r.optBoolean("ok")) r.optJSONObject("task")?.let { detail = it }
    }

    fun runCommand() {
        if (starting) return
        val cmd = command.trim()
        if (cmd.isBlank()) {
            Store.toast = "先输入要执行的命令"
            return
        }
        starting = true
        error = ""
        scope.launch {
            val timeout = timeoutText.toIntOrNull()?.coerceIn(1, 3600) ?: TASK_TIMEOUT_DEFAULT
            val r = Api.post(
                "/api/tasks/run",
                JSONObject().put("command", cmd).put("timeout", timeout),
            )
            starting = false
            when {
                r == null -> error = "管理服务没有响应，任务可能没有启动"
                !r.optBoolean("ok") -> error = r.optStr("error").ifBlank { "任务启动失败" }
                else -> {
                    Store.toast = "任务已启动"
                    val task = r.optJSONObject("task")
                    if (task != null) {
                        detail = task
                    } else {
                        val id = r.optStr("id")
                        if (id.isNotBlank()) {
                            refreshDetail(id)
                            if (detail == null) detail = JSONObject().put("id", id).put("command", cmd).put("status", "running")
                        }
                    }
                    refreshTasks()
                }
            }
        }
    }

    BackHandler { onBack() }

    // 列表每 2 秒刷新（运行中的任务状态会变）
    LaunchedEffect(Unit) {
        while (true) {
            refreshTasks()
            delay(2000)
        }
    }

    // 详情里的任务还在跑时也每 2 秒刷新
    val detailId = detail?.optStr("id").orEmpty()
    LaunchedEffect(detailId) {
        if (detailId.isBlank()) return@LaunchedEffect
        while (true) {
            delay(2000)
            if (detail?.optStr("status") != "running") break
            refreshDetail(detailId)
        }
    }

    PageList(padding, scrollBehavior) {
        group {
            ArrowPreference(title = "← 返回更多", onClick = onBack)
        }

        group("运行命令") {
            GlassTextField(
                value = command,
                onValueChange = { command = it },
                label = "命令（在机器人运行环境里执行）",
                singleLine = false,
                minLines = 3,
                maxLines = 8,
                mono = true,
                modifier = Modifier.fillMaxWidth().padding(12.dp),
            )
            EditPreference(
                "超时（秒）",
                timeoutText,
                summary = "默认 $TASK_TIMEOUT_DEFAULT 秒，最多 3600 秒",
                numeric = true,
            ) {
                it.toIntOrNull()?.let { v -> timeoutText = v.coerceIn(1, 3600).toString() }
            }
            Row(
                Modifier.fillMaxWidth().padding(16.dp),
                horizontalArrangement = Arrangement.spacedBy(12.dp),
            ) {
                TextButton(
                    text = if (starting) "正在启动…" else "运行",
                    onClick = { runCommand() },
                    modifier = Modifier.weight(1f),
                    colors = ButtonDefaults.textButtonColorsPrimary(),
                )
                TextButton(
                    text = "刷新列表",
                    onClick = { scope.launch { refreshTasks(); Store.toast = "已刷新" } },
                    modifier = Modifier.weight(1f),
                )
            }
            if (starting) {
                Column(Modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 8.dp)) {
                    InfiniteProgressIndicator()
                }
            }
        }

        if (error.isNotBlank()) {
            item { Hint(error) }
        }

        group("最近任务") {
            if (tasks.isEmpty()) {
                InfoRow("还没有任务", "在上面运行一条命令")
            }
            tasks.forEach { task ->
                val status = task.optStr("status")
                val color = taskStatusColor(status)
                ArrowPreference(
                    title = taskCommandSummary(task.optStr("command")),
                    summary = fmtDuration(task.optLong("duration_ms")),
                    endActions = {
                        Row(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                            Dot(color)
                            Text(taskStatusText(status), color = color, fontSize = 14.sp)
                        }
                    },
                    onClick = {
                        val id = task.optStr("id")
                        if (id.isNotBlank()) {
                            scope.launch {
                                detail = task
                                refreshDetail(id)
                            }
                        }
                    },
                )
            }
        }
        item { Hint("命令在机器人的运行环境里执行；运行中的任务可以点开详情停止。输出会保留在服务端。") }
    }

    val shown = detail
    if (shown != null) {
        TaskDetailDialog(
            task = shown,
            onClose = { detail = null },
            onRefresh = {
                scope.launch { refreshDetail(shown.optStr("id")) }
            },
            onStop = { id ->
                scope.launch {
                    val r = Api.post("/api/tasks/stop", JSONObject().put("id", id))
                    if (r?.optBoolean("ok") == true) {
                        Store.toast = "已请求停止"
                        refreshDetail(id)
                        refreshTasks()
                    } else {
                        Store.toast = "停止失败：" + (r?.optStr("error").orEmpty().ifBlank { "管理服务没有响应" })
                    }
                }
            },
        )
    }
}

@Composable
private fun TaskDetailDialog(
    task: JSONObject,
    onClose: () -> Unit,
    onRefresh: () -> Unit,
    onStop: (String) -> Unit,
) {
    val ctx = LocalContext.current
    val status = task.optStr("status")
    val id = task.optStr("id")
    val stdout = task.optStr("stdout")
    val stderr = task.optStr("stderr")
    val timedOut = task.optBoolean("timed_out")
    val exitCode = task.optInt("exit_code", 0)
    val duration = task.optLong("duration_ms")

    fun copyAll() {
        val text = buildString {
            if (stdout.isNotBlank()) append(stdout)
            if (stderr.isNotBlank()) {
                if (isNotEmpty()) append("\n")
                append(stderr)
            }
        }
        copyToClipboard(ctx, text.ifBlank { "（没有输出）" })
        Store.toast = "已复制输出"
    }

    GlassDialog(show = true, title = "任务详情", onDismissRequest = onClose) {
        Column(
            Modifier.fillMaxWidth().heightIn(max = 580.dp).verticalScroll(rememberScrollState()),
        ) {
            Text("命令", fontSize = 13.sp, fontWeight = FontWeight.Medium)
            Spacer(Modifier.height(4.dp))
            Text(
                task.optStr("command"),
                fontSize = 12.sp,
                fontFamily = FontFamily.Monospace,
                color = MiuixTheme.colorScheme.onSurfaceVariantSummary,
            )
            Spacer(Modifier.height(10.dp))
            Text(
                "状态：${taskStatusText(status)} · 耗时 ${fmtDuration(duration)}" +
                    if (status == "done" || status == "failed" || status == "stopped") " · 退出码 $exitCode" else "",
                fontSize = 14.sp,
                color = taskStatusColor(status),
                fontWeight = FontWeight.Medium,
            )
            if (timedOut) {
                Spacer(Modifier.height(4.dp))
                Text("运行超时，已被终止", fontSize = 13.sp, color = Warn)
            }

            Spacer(Modifier.height(12.dp))
            Text("输出", fontSize = 13.sp, fontWeight = FontWeight.Medium)
            Spacer(Modifier.height(4.dp))
            InkBox(Modifier.fillMaxWidth()) {
                Text(
                    stdout.ifBlank { "（还没有输出）" }.takeLast(20000),
                    modifier = Modifier.padding(12.dp),
                    fontSize = 11.sp,
                    fontFamily = FontFamily.Monospace,
                    color = GlassText.mono,
                )
            }
            if (stderr.isNotBlank()) {
                Spacer(Modifier.height(10.dp))
                Text("错误输出", fontSize = 13.sp, fontWeight = FontWeight.Medium, color = Bad)
                Spacer(Modifier.height(4.dp))
                InkBox(Modifier.fillMaxWidth()) {
                    Text(
                        stderr.takeLast(8000),
                        modifier = Modifier.padding(12.dp),
                        fontSize = 11.sp,
                        fontFamily = FontFamily.Monospace,
                        color = Bad,
                    )
                }
            }

            Spacer(Modifier.height(14.dp))
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                TextButton(
                    text = "刷新",
                    onClick = onRefresh,
                    modifier = Modifier.weight(1f),
                )
                if (status == "running") {
                    TextButton(
                        text = "停止",
                        onClick = { if (id.isNotBlank()) onStop(id) },
                        modifier = Modifier.weight(1f),
                    )
                }
                TextButton(
                    text = "复制输出",
                    onClick = { copyAll() },
                    modifier = Modifier.weight(1f),
                )
                TextButton(
                    text = "关闭",
                    onClick = onClose,
                    modifier = Modifier.weight(1f),
                    colors = ButtonDefaults.textButtonColorsPrimary(),
                )
            }
            Spacer(Modifier.height(4.dp))
        }
    }
}

// ============================================================ 技能

private fun skillSummary(skill: JSONObject): String {
    val desc = skill.optStr("description").ifBlank { "—" }
    val size = skill.optLong("size")
    return if (size > 0) "$desc · ${size} 字节" else desc
}

/** 技能：列出 / 查看 / 新建 / 编辑 / 删除（Markdown 文件）。 */
@Composable
fun SkillManagerPage(padding: PaddingValues, scrollBehavior: ScrollBehavior, onBack: () -> Unit) {
    val scope = rememberCoroutineScope()

    var skills by remember { mutableStateOf<List<JSONObject>>(emptyList()) }
    var loading by remember { mutableStateOf(true) }
    var error by remember { mutableStateOf("") }
    var viewName by remember { mutableStateOf<String?>(null) }
    var viewContent by remember { mutableStateOf("") }
    var editName by remember { mutableStateOf<String?>(null) }
    var editContent by remember { mutableStateOf("") }
    var editIsNew by remember { mutableStateOf(false) }

    suspend fun refreshSkills() {
        loading = true
        val r = Api.get("/api/skills")
        loading = false
        if (r == null) {
            error = "管理服务没有响应"
            return
        }
        if (!r.optBoolean("ok")) {
            error = r.optStr("error").ifBlank { "读取技能列表失败" }
            return
        }
        error = ""
        val arr = r.optJSONArray("skills")
        skills = (0 until (arr?.length() ?: 0)).mapNotNull { arr?.optJSONObject(it) }
    }

    fun openSkill(name: String) {
        if (name.isBlank()) return
        scope.launch {
            val r = Api.get("/api/skills/get?name=" + URLEncoder.encode(name, "UTF-8"))
            when {
                r == null -> Store.toast = "读取技能失败：管理服务没有响应"
                !r.optBoolean("ok") -> Store.toast = "读取失败：" + r.optStr("error").ifBlank { "未知错误" }
                else -> {
                    viewContent = r.optStr("content")
                    viewName = name
                }
            }
        }
    }

    fun saveSkill(name: String, content: String) {
        scope.launch {
            val r = Api.post("/api/skills/save", JSONObject().put("name", name).put("content", content))
            if (r?.optBoolean("ok") == true) {
                Store.toast = "技能已保存"
                refreshSkills()
            } else {
                Store.toast = "保存失败：" + (r?.optStr("error").orEmpty().ifBlank { "管理服务没有响应" })
            }
        }
    }

    fun deleteSkill(name: String) {
        scope.launch {
            val r = Api.post("/api/skills/delete", JSONObject().put("name", name))
            if (r?.optBoolean("ok") == true) {
                Store.toast = "技能已删除"
                refreshSkills()
            } else {
                Store.toast = "删除失败：" + (r?.optStr("error").orEmpty().ifBlank { "管理服务没有响应" })
            }
        }
    }

    BackHandler { onBack() }

    LaunchedEffect(Unit) { refreshSkills() }

    PageList(padding, scrollBehavior) {
        group {
            ArrowPreference(title = "← 返回更多", onClick = onBack)
            ArrowPreference(
                title = "新建技能",
                summary = "新建一个 Markdown 技能文件",
                onClick = {
                    editName = ""
                    editContent = ""
                    editIsNew = true
                    viewName = null
                },
            )
            ArrowPreference(
                title = "刷新列表",
                onClick = { scope.launch { refreshSkills(); Store.toast = "已刷新" } },
            )
        }

        group("技能列表") {
            if (skills.isEmpty()) {
                InfoRow(if (loading) "加载中…" else "还没有技能", if (loading) "" else "点「新建技能」写一个")
            }
            skills.forEach { skill ->
                val name = skill.optStr("name")
                ArrowPreference(
                    title = name,
                    summary = skillSummary(skill),
                    onClick = { openSkill(name) },
                )
            }
        }

        if (error.isNotBlank()) {
            item { Hint(error) }
        }
        item { Hint("技能是保存在机器人运行环境里的 Markdown 文件；AI 会根据名称和描述决定什么时候用。") }
    }

    val viewing = viewName
    if (viewing != null) {
        SkillViewDialog(
            name = viewing,
            content = viewContent,
            onEdit = {
                editName = viewing
                editContent = viewContent
                editIsNew = false
                viewName = null
            },
            onDelete = {
                deleteSkill(viewing)
                viewName = null
            },
            onClose = { viewName = null },
        )
    }

    val editing = editName
    if (editing != null) {
        SkillEditDialog(
            initialName = editing,
            initialContent = editContent,
            isNew = editIsNew,
            onSave = { name, content ->
                saveSkill(name, content)
                editName = null
            },
            onDismiss = { editName = null },
        )
    }
}

@Composable
private fun SkillViewDialog(
    name: String,
    content: String,
    onEdit: () -> Unit,
    onDelete: () -> Unit,
    onClose: () -> Unit,
) {
    var confirmDelete by remember { mutableStateOf(false) }

    GlassDialog(show = true, title = name, onDismissRequest = onClose) {
        Column(
            Modifier.fillMaxWidth().heightIn(max = 580.dp).verticalScroll(rememberScrollState()),
        ) {
            InkBox(Modifier.fillMaxWidth()) {
                Text(
                    content.ifBlank { "（这个技能还没有内容）" }.take(40000),
                    modifier = Modifier.padding(12.dp),
                    fontSize = 12.sp,
                    fontFamily = FontFamily.Monospace,
                    color = GlassText.mono,
                )
            }
            Spacer(Modifier.height(14.dp))
            if (confirmDelete) {
                Text("确定删除技能「$name」？", fontSize = 14.sp, color = Bad)
                Spacer(Modifier.height(10.dp))
                Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                    TextButton(text = "取消", onClick = { confirmDelete = false }, modifier = Modifier.weight(1f))
                    TextButton(
                        text = "确认删除",
                        onClick = onDelete,
                        modifier = Modifier.weight(1f),
                        colors = ButtonDefaults.textButtonColorsPrimary(),
                    )
                }
            } else {
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    TextButton(text = "编辑", onClick = onEdit, modifier = Modifier.weight(1f))
                    TextButton(text = "删除", onClick = { confirmDelete = true }, modifier = Modifier.weight(1f))
                    TextButton(
                        text = "关闭",
                        onClick = onClose,
                        modifier = Modifier.weight(1f),
                        colors = ButtonDefaults.textButtonColorsPrimary(),
                    )
                }
            }
            Spacer(Modifier.height(4.dp))
        }
    }
}

@Composable
private fun SkillEditDialog(
    initialName: String,
    initialContent: String,
    isNew: Boolean,
    onSave: (String, String) -> Unit,
    onDismiss: () -> Unit,
) {
    var name by remember { mutableStateOf(initialName) }
    var content by remember { mutableStateOf(initialContent) }
    var error by remember { mutableStateOf("") }

    GlassDialog(
        show = true,
        title = if (isNew) "新建技能" else "编辑技能",
        onDismissRequest = onDismiss,
    ) {
        Column(
            Modifier.fillMaxWidth().heightIn(max = 600.dp).verticalScroll(rememberScrollState()),
        ) {
            GlassTextField(
                value = name,
                onValueChange = { name = it },
                label = "技能名称（文件名，如 my-skill.md）",
                singleLine = true,
                modifier = Modifier.fillMaxWidth().padding(bottom = 10.dp),
            )
            GlassTextField(
                value = content,
                onValueChange = { content = it },
                label = "技能内容（Markdown）",
                singleLine = false,
                minLines = 8,
                maxLines = 18,
                mono = true,
                modifier = Modifier.fillMaxWidth().padding(bottom = 10.dp),
            )
            if (error.isNotBlank()) {
                Text(error, fontSize = 13.sp, color = Bad)
                Spacer(Modifier.height(8.dp))
            }
            Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                TextButton(text = "取消", onClick = onDismiss, modifier = Modifier.weight(1f))
                TextButton(
                    text = "保存",
                    onClick = {
                        val n = name.trim()
                        if (n.isBlank()) {
                            error = "技能名称不能为空"
                        } else {
                            onSave(n, content)
                        }
                    },
                    modifier = Modifier.weight(1f),
                    colors = ButtonDefaults.textButtonColorsPrimary(),
                )
            }
            Spacer(Modifier.height(4.dp))
        }
    }
}

private fun copyToClipboard(ctx: Context, text: String) {
    try {
        val cm = ctx.getSystemService(ClipboardManager::class.java) ?: return
        cm.setPrimaryClip(ClipData.newPlainText("任务输出", text))
    } catch (_: Exception) {
    }
}

package com.qqbot.console.ui

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
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.qqbot.console.Store
import com.qqbot.console.objects
import com.qqbot.console.patchOf
import kotlinx.coroutines.launch
import org.json.JSONArray
import org.json.JSONObject
import top.yukonga.miuix.kmp.basic.ButtonDefaults
import top.yukonga.miuix.kmp.basic.ScrollBehavior
import top.yukonga.miuix.kmp.basic.Text
import top.yukonga.miuix.kmp.basic.TextButton
import top.yukonga.miuix.kmp.basic.TextField
import top.yukonga.miuix.kmp.overlay.OverlayDialog
import top.yukonga.miuix.kmp.preference.ArrowPreference
import top.yukonga.miuix.kmp.preference.OverlayDropdownPreference
import top.yukonga.miuix.kmp.theme.MiuixTheme

// ============================================================ 关键词管理页

private val MATCH_IDS = listOf("contains", "exact", "regex")
private val MATCH_LABELS = listOf("包含关键词", "完全匹配", "正则表达式")
private val SCOPE_IDS = listOf("all", "private", "group")
private val SCOPE_LABELS = listOf("所有会话", "仅私聊", "仅群聊")

private fun JSONArray?.joinText(): String =
    if (this == null) "" else (0 until length()).joinToString("，") { optString(it) }

private fun splitWords(s: String): JSONArray {
    val arr = JSONArray()
    s.split(",", "，", "、", "\n").map(String::trim).filter(String::isNotBlank).forEach(arr::put)
    return arr
}

private fun matchLabel(match: String): String = when (match.lowercase()) {
    "exact" -> "完全匹配"
    "regex" -> "正则"
    else -> "包含关键词"
}

private fun scopeLabel(scope: String): String = when (scope.lowercase()) {
    "private" -> "仅私聊"
    "group" -> "仅群聊"
    else -> "所有会话"
}

private fun ruleSummary(rule: JSONObject): String {
    val match = rule.optStr("match").ifBlank { "contains" }
    val scope = scopeLabel(rule.optStr("scope"))
    val body = if (match == "regex") {
        val regex = rule.optStr("regex")
        if (regex.isBlank()) "（还没填正则）" else "正则：${regex.take(24)}"
    } else {
        val kw = rule.optJSONArray("keywords").joinText()
        if (kw.isBlank()) "（还没填关键词）" else "关键词：${kw.take(24)}"
    }
    return "$body · ${matchLabel(match)} · $scope"
}

/** 关键词管理：关键词回复规则增删改、联网搜索 / 画图触发词。 */
@Composable
fun KeywordManagerPage(padding: PaddingValues, scrollBehavior: ScrollBehavior, onBack: () -> Unit) {
    val scope = rememberCoroutineScope()
    val keyword = Store.cfg("plugins", "keyword") ?: JSONObject()
    val rules = keyword.optJSONArray("rules")?.objects().orEmpty()
    val search = Store.cfg("plugins", "ai_chat", "search") ?: JSONObject()
    val imageGen = Store.cfg("plugins", "image_gen") ?: JSONObject()

    // -1 关闭；否则是要编辑/新增的规则下标（rules.size = 新增）
    var editing by remember { mutableStateOf(-1) }

    fun saveRules(list: List<JSONObject>) {
        val arr = JSONArray()
        list.forEach(arr::put)
        scope.launch { Store.patch(patchOf("plugins", "keyword", "rules", value = arr), "关键词规则已保存") }
    }

    BackHandler { onBack() }

    PageList(padding, scrollBehavior) {
        group {
            ArrowPreference(title = "← 返回更多", onClick = onBack)
        }

        group("关键词回复") {
            ConfigSwitch("启用关键词回复", "命中规则自动回复；长消息会交给 AI", "plugins", "keyword", "enabled", default = true)
            rules.forEachIndexed { i, rule ->
                ArrowPreference(
                    title = rule.optStr("name").ifBlank { "规则 ${i + 1}" },
                    summary = ruleSummary(rule),
                    onClick = { editing = i },
                )
            }
            ArrowPreference(
                title = "＋ 新增关键词规则",
                summary = if (rules.isEmpty()) "还没有规则，点这里加一条" else null,
                onClick = { editing = rules.size },
            )
        }
        item { Hint("命中后会按规则的回复内容自动发送；规则顺序就是匹配顺序。") }

        group("联网搜索触发词") {
            EditPreference(
                "搜索触发词",
                search.optJSONArray("trigger_keywords").joinText(),
                summary = "AI 看到这些词会先联网搜索，用逗号分隔",
                multiline = true,
            ) { text ->
                Store.patch(
                    patchOf("plugins", "ai_chat", "search", "trigger_keywords", value = splitWords(text)),
                    "搜索触发词已保存",
                )
            }
        }

        group("画图触发词") {
            EditPreference(
                "画图触发词",
                imageGen.optJSONArray("triggers").joinText(),
                summary = "消息里出现这些词就生成图片，用逗号分隔",
                multiline = true,
            ) { text ->
                Store.patch(
                    patchOf("plugins", "image_gen", "triggers", value = splitWords(text)),
                    "画图触发词已保存",
                )
            }
        }
        item { Hint("改完立即生效（配置会热加载），不需要重启机器人。") }
    }

    if (editing >= 0) {
        val isNew = editing >= rules.size
        val base = if (isNew) newRule() else (rules.getOrNull(editing) ?: newRule())
        RuleEditDialog(
            initial = base,
            isNew = isNew,
            onSave = { updated ->
                val list = rules.toMutableList()
                if (isNew || editing >= list.size) list.add(updated) else list[editing] = updated
                saveRules(list)
                editing = -1
            },
            onDelete = {
                val list = rules.toMutableList()
                if (editing in list.indices) list.removeAt(editing)
                saveRules(list)
                editing = -1
            },
            onDismiss = { editing = -1 },
        )
    }
}

private fun newRule(): JSONObject = JSONObject()
    .put("name", "新规则")
    .put("keywords", JSONArray())
    .put("match", "contains")
    .put("scope", "all")
    .put("reply", "")

@Composable
private fun RuleEditDialog(
    initial: JSONObject,
    isNew: Boolean,
    onSave: (JSONObject) -> Unit,
    onDelete: () -> Unit,
    onDismiss: () -> Unit,
) {
    var name by remember { mutableStateOf(initial.optStr("name")) }
    var keywords by remember { mutableStateOf(initial.optJSONArray("keywords").joinText()) }
    var regex by remember { mutableStateOf(initial.optStr("regex")) }
    var reply by remember { mutableStateOf(initial.optStr("reply")) }
    var match by remember { mutableStateOf(initial.optStr("match").ifBlank { "contains" }) }
    var scope by remember { mutableStateOf(initial.optStr("scope").ifBlank { "all" }) }
    var confirmDelete by remember { mutableStateOf(false) }

    fun build(): JSONObject {
        val obj = JSONObject(initial.toString())
            .put("name", name.trim().ifBlank { "未命名规则" })
            .put("keywords", splitWords(keywords))
            .put("match", match)
            .put("scope", scope)
            .put("reply", reply)
        val trimmedRegex = regex.trim()
        if (trimmedRegex.isBlank()) obj.remove("regex") else obj.put("regex", trimmedRegex)
        return obj
    }

    GlassDialog(
        show = true,
        title = if (isNew) "新增关键词规则" else "编辑关键词规则",
        onDismissRequest = onDismiss,
    ) {
        Column(
            Modifier.fillMaxWidth().heightIn(max = 540.dp).verticalScroll(rememberScrollState()),
        ) {
            GlassTextField(
                value = name,
                onValueChange = { name = it },
                label = "规则名字",
                singleLine = true,
                modifier = Modifier.fillMaxWidth().padding(bottom = 10.dp),
            )

            OverlayDropdownPreference(
                title = "匹配方式",
                items = MATCH_LABELS,
                selectedIndex = MATCH_IDS.indexOf(match).coerceAtLeast(0),
                onSelectedIndexChange = { match = MATCH_IDS[it] },
            )

            if (match == "regex") {
                GlassTextField(
                    value = regex,
                    onValueChange = { regex = it },
                    label = "正则表达式",
                    singleLine = true,
                    modifier = Modifier.fillMaxWidth().padding(bottom = 10.dp),
                )
                Text(
                    "正则模式用这个表达式匹配消息，下面的关键词不生效。",
                    fontSize = 12.sp,
                    color = MiuixTheme.colorScheme.onSurfaceVariantSummary,
                    modifier = Modifier.padding(bottom = 10.dp),
                )
            } else {
                GlassTextField(
                    value = keywords,
                    onValueChange = { keywords = it },
                    label = "关键词（用逗号分隔）",
                    singleLine = false,
                    minLines = 2,
                    maxLines = 4,
                    modifier = Modifier.fillMaxWidth().padding(bottom = 10.dp),
                )
            }

            OverlayDropdownPreference(
                title = "生效范围",
                items = SCOPE_LABELS,
                selectedIndex = SCOPE_IDS.indexOf(scope).coerceAtLeast(0),
                onSelectedIndexChange = { scope = SCOPE_IDS[it] },
            )

            GlassTextField(
                value = reply,
                onValueChange = { reply = it },
                label = "回复内容（可用 {nickname} {bot_name} {at}）",
                singleLine = false,
                minLines = 3,
                maxLines = 6,
                modifier = Modifier.fillMaxWidth().padding(top = 10.dp, bottom = 12.dp),
            )

            if (confirmDelete) {
                Text("确定删除这条规则？", fontSize = 14.sp, color = Bad)
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
                    TextButton(text = "取消", onClick = onDismiss, modifier = Modifier.weight(1f))
                    if (!isNew) {
                        TextButton(text = "删除", onClick = { confirmDelete = true }, modifier = Modifier.weight(1f))
                    }
                    TextButton(
                        text = "保存",
                        onClick = { onSave(build()) },
                        modifier = Modifier.weight(1f),
                        colors = ButtonDefaults.textButtonColorsPrimary(),
                    )
                }
            }
            Spacer(Modifier.height(4.dp))
        }
    }
}

// ============================================================ 插件管理页

/** 插件管理：各插件总开关 + Python 工具配置。 */
@Composable
fun PluginManagerPage(padding: PaddingValues, scrollBehavior: ScrollBehavior, onBack: () -> Unit) {
    val pythonTimeout = Store.int("config", "plugins", "python_run", "timeout_seconds", def = 30)

    BackHandler { onBack() }

    PageList(padding, scrollBehavior) {
        group {
            ArrowPreference(title = "← 返回更多", onClick = onBack)
        }

        group("插件开关") {
            ConfigSwitch("关键词回复", "命中关键词自动回复", "plugins", "keyword", "enabled", default = true)
            ConfigSwitch("群管理", "入群欢迎、违禁词、防刷屏", "plugins", "group_admin", "enabled", default = true)
            ConfigSwitch("AI 聊天", "@ 机器人聊天、记忆、画像", "plugins", "ai_chat", "enabled", default = true)
            ConfigSwitch("画图", "消息命中触发词时生成图片", "plugins", "image_gen", "enabled", default = true)
            ConfigSwitch("联网搜索", "AI 回答前先联网搜索", "plugins", "ai_chat", "search", "enabled", default = true)
            ConfigSwitch("Python 运行", "允许 /py、/run 或代码块执行", "plugins", "python_run", "enabled", default = true)
            ConfigSwitch("技能", "AI 技能（Markdown 文件）", "plugins", "skills", "enabled", default = true)
        }

        group("Python 工具") {
            ConfigSwitch(
                "让 AI 自己决定何时运行",
                "AI 需要计算或跑代码时会自己调用 Python；关掉后只响应 /py、/run",
                "plugins", "python_run", "ai_tool", default = true,
            )
            EditPreference("执行超时（秒）", pythonTimeout.toString(), summary = "1–120 秒", numeric = true) {
                it.toIntOrNull()?.let { v ->
                    Store.patch(
                        patchOf("plugins", "python_run", "timeout_seconds", value = v.coerceIn(1, 120)),
                        "超时已改为 ${v.coerceIn(1, 120)} 秒",
                    )
                }
            }
        }

        group("开发者工具") {
            val devTimeout = Store.int("config", "plugins", "dev_tools", "timeout_seconds", def = 300)
            ConfigSwitch(
                "启用开发者工具",
                "在运行环境里执行命令、操作仓库；AI 也可以调用",
                "plugins", "dev_tools", "enabled", default = true,
            )
            ConfigSwitch(
                "让 AI 自己决定何时使用",
                "AI 需要跑命令或看仓库时会自己调用开发者工具",
                "plugins", "dev_tools", "ai_tool", default = true,
            )
            EditPreference("任务超时（秒）", devTimeout.toString(), summary = "默认 300 秒，1–3600", numeric = true) {
                it.toIntOrNull()?.let { v ->
                    Store.patch(
                        patchOf("plugins", "dev_tools", "timeout_seconds", value = v.coerceIn(1, 3600)),
                        "超时已改为 ${v.coerceIn(1, 3600)} 秒",
                    )
                }
            }
            EditPreference(
                "仓库路径",
                Store.str("config", "plugins", "dev_tools", "repo_path"),
                summary = "git 操作的默认目录，可留空",
            ) { text ->
                Store.patch(patchOf("plugins", "dev_tools", "repo_path", value = text.trim()))
            }
            EditPreference(
                "GitHub Token",
                Store.str("config", "plugins", "dev_tools", "git_token"),
                summary = "用于私有仓库操作；留空不修改",
                secret = true,
            ) { text ->
                if (text.isNotBlank()) {
                    Store.patch(patchOf("plugins", "dev_tools", "git_token", value = text.trim()), "Token 已保存")
                }
            }
        }

        item { Hint("开关会立即写入配置（热加载生效）；改坏了可以到网页控制台恢复。") }
        item { Hint("Python 工具的「让 AI 自己决定何时运行」关掉后，AI 不会主动执行代码，只能用 /py、/run 手动触发。") }
    }
}

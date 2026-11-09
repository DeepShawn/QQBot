package com.qqbot.console.ui

import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import com.qqbot.console.AccountsStore
import com.qqbot.console.PROVIDER_NAMES
import com.qqbot.console.PROVIDER_ORDER
import com.qqbot.console.Store
import com.qqbot.console.objects
import com.qqbot.console.patchOf
import kotlinx.coroutines.launch
import org.json.JSONArray
import org.json.JSONObject
import top.yukonga.miuix.kmp.basic.ScrollBehavior
import top.yukonga.miuix.kmp.preference.ArrowPreference
import top.yukonga.miuix.kmp.preference.OverlayDropdownPreference

private val PROVIDERS = listOf("openai", "claude", "gemini")
private val PROVIDER_LABELS = listOf("OpenAI 兼容（DeepSeek / ChatGPT / Grok / 中转站）", "Claude 原生", "Gemini 原生")

/** 认证方式：空串 = API Key，其余是账号 id。 */
private val AUTH_IDS = listOf("", "chatgpt", "claude", "gemini", "deepseek")
private val AUTH_LABELS = listOf(
    "API Key",
    "ChatGPT 账号",
    "Claude 账号",
    "Gemini 账号",
    "DeepSeek 账号",
)

@Composable
fun AiPage(padding: PaddingValues, scrollBehavior: ScrollBehavior) {
    val scope = rememberCoroutineScope()
    val ai = Store.cfg("plugins", "ai_chat") ?: JSONObject()
    val profiles = ai.optJSONArray("profiles")?.objects().orEmpty()
    val activeId = ai.optStr("active_profile")
    val active = profiles.firstOrNull { it.optStr("id") == activeId } ?: profiles.firstOrNull()
    var editingId by remember { mutableStateOf<String?>(null) }
    val editing = profiles.firstOrNull { it.optStr("id") == editingId } ?: active
    var accountDialog by remember { mutableStateOf<String?>(null) }
    var modelPicker by remember { mutableStateOf(false) }

    // 进页面时拉一次账号状态（登录 / 退出后也会刷新）
    LaunchedEffect(Unit) { AccountsStore.refresh() }

    /** 改选中那一套 profile 的一个字段，整份 profiles 数组一起提交（后端会保留掩码 key）。 */
    fun saveProfileField(field: String, value: Any) {
        val target = editing ?: return
        val arr = JSONArray()
        profiles.forEach { p ->
            val copy = JSONObject(p.toString())
            if (p.optStr("id") == target.optStr("id")) copy.put(field, value)
            arr.put(copy)
        }
        scope.launch { Store.patch(patchOf("plugins", "ai_chat", "profiles", value = arr)) }
    }

    PageList(padding, scrollBehavior) {
        group("AI 账号") {
            PROVIDER_ORDER.forEach { id ->
                val acc = AccountsStore.find(id)
                ArrowPreference(
                    title = PROVIDER_NAMES[id] ?: acc.name,
                    summary = when {
                        acc.signedIn -> acc.account.ifBlank { "已连接" }
                        acc.error == "unsupported" -> "这个版本还不支持"
                        else -> "未连接"
                    },
                    onClick = { accountDialog = id },
                )
            }
        }
        if (AccountsStore.error.isNotBlank()) {
            item { Hint("账号状态读取失败：${AccountsStore.error}") }
        }
        item { Hint("用浏览器登录账号后，聊天走账号额度，不用填 API Key。") }

        group("当前接口") {
            if (profiles.isEmpty()) {
                InfoRow("还没有接口", "在网页控制台添加")
            } else {
                OverlayDropdownPreference(
                    title = "使用",
                    summary = active?.optStr("model"),
                    items = profiles.map { it.optStr("name").ifBlank { it.optStr("id") } },
                    selectedIndex = profiles.indexOf(active).coerceAtLeast(0),
                    onSelectedIndexChange = { i ->
                        val id = profiles[i].optStr("id")
                        editingId = id
                        scope.launch { Store.patch(patchOf("plugins", "ai_chat", "active_profile", value = id), "已切换到 ${profiles[i].optStr("name")}") }
                    },
                )
            }
        }

        if (editing != null) {
            group("接口设置 · ${editing.optStr("name")}") {
                // 认证方式放最前面：到底是 API Key 还是某个账号，先选清楚
                val auth = editing.optStr("auth")
                val authIdx = AUTH_IDS.indexOf(auth).coerceAtLeast(0)
                OverlayDropdownPreference(
                    title = "认证方式（API Key / 账号）",
                    summary = if (auth.isBlank()) "API Key" else "${PROVIDER_NAMES[auth] ?: auth} 账号",
                    items = AUTH_LABELS,
                    selectedIndex = authIdx,
                    onSelectedIndexChange = { i -> saveProfileField("auth", AUTH_IDS[i]) },
                )
                if (auth.isNotBlank()) {
                    val acc = AccountsStore.find(auth)
                    if (acc.signedIn) {
                        Hint(
                            "已连接 ${PROVIDER_NAMES[auth] ?: auth} 账号" +
                                if (acc.account.isBlank()) "。" else "（${acc.account}）。",
                        )
                    } else {
                        Hint("这个账号还没连接，先去上方「AI 账号」里连接，否则这条接口用不了。")
                    }
                }
                val pIdx = PROVIDERS.indexOf(editing.optStr("provider").ifBlank { "openai" }).coerceAtLeast(0)
                OverlayDropdownPreference(
                    title = "协议",
                    items = PROVIDER_LABELS,
                    selectedIndex = pIdx,
                    onSelectedIndexChange = { saveProfileField("provider", PROVIDERS[it]) },
                )
                EditPreference("接口地址", editing.optStr("base_url")) { saveProfileField("base_url", it) }
                ArrowPreference(
                    title = "模型",
                    summary = editing.optStr("model").ifBlank { "跟随账号默认" },
                    onClick = { modelPicker = true },
                )
                EditPreference(
                    "API Key",
                    editing.optStr("api_key"),
                    summary = if (auth.isNotBlank()) "已改用账号登录" else null,
                    secret = true,
                ) { if (it.isNotBlank()) saveProfileField("api_key", it) }
                if (auth.isBlank()) {
                    EditPreference("温度", editing.opt("temperature")?.toString() ?: "0.7", numeric = true) {
                        it.toDoubleOrNull()?.let { v -> saveProfileField("temperature", v.coerceIn(0.0, 2.0)) }
                    }
                    EditPreference("最大输出 tokens", editing.opt("max_tokens")?.toString() ?: "4096", numeric = true) {
                        it.toIntOrNull()?.let { v -> saveProfileField("max_tokens", v.coerceIn(64, 8192)) }
                    }
                } else {
                    // 账号模式由账号接口决定输出长度，这两个参数不生效（值保留，切回 API Key 还能用）
                    Hint("账号模式按账号默认输出：温度 / 最大输出 tokens 不生效（不限制输出长度）。")
                }
            }
        }

        group("人格") {
            EditPreference("机器人名字", Store.str("config", "bot", "name")) {
                if (it.isNotBlank()) Store.patch(patchOf("bot", "name", value = it))
            }
            EditPreference("人格提示词", ai.optStr("system_prompt"), multiline = true) {
                Store.patch(patchOf("plugins", "ai_chat", "system_prompt", value = it))
            }
        }

        group("什么时候回复") {
            ConfigSwitch("私聊直接回复", null, "plugins", "ai_chat", "trigger", "private", default = true)
            ConfigSwitch("群里 @ 才回", null, "plugins", "ai_chat", "trigger", "group_at", default = true)
            ConfigSwitch("引用它的消息也回", null, "plugins", "ai_chat", "trigger", "group_quote", default = true)
            val follow = Store.int("config", "plugins", "ai_chat", "trigger", "group_followup_seconds")
            top.yukonga.miuix.kmp.preference.SwitchPreference(
                title = "@ 一次后继续对话",
                summary = if (follow > 0) "唤醒后 $follow 秒内，这个人接着说也会回" else "关闭：每次都要 @ / 叫名字 / 引用才回",
                checked = follow > 0,
                onCheckedChange = { on ->
                    scope.launch { Store.patch(patchOf("plugins", "ai_chat", "trigger", "group_followup_seconds", value = if (on) 120 else 0), null) }
                },
            )
            val aliases = ai.optJSONObject("trigger")?.optJSONArray("group_name_aliases")
            EditPreference("叫名字也回（别名）", (0 until (aliases?.length() ?: 0)).joinToString("，") { aliases!!.optString(it) }) {
                val arr = JSONArray(); it.split(",", "，", " ").map(String::trim).filter(String::isNotBlank).forEach(arr::put)
                Store.patch(patchOf("plugins", "ai_chat", "trigger", "group_name_aliases", value = arr))
            }
        }

        group("能力") {
            ConfigSwitch("联网搜索", "提到「最新/今天/查一下」时先搜再答", "plugins", "ai_chat", "search", "enabled", default = true)
            ConfigSwitch("识别图片和文件", "发图 / 发文档 / 引用它们时读取内容", "plugins", "ai_chat", "attachments", "enabled", default = true)
            ConfigSwitch("长代码发成文件", "代码超过 15 行自动存文件发到群里", "plugins", "ai_chat", "files", "enabled", default = true)
        }

        group("记忆") {
            // 0 = 不限制轮数 / 永不忘记（后端 T20 支持）；两个都为 0 就是「永久记忆」
            val maxHistory = Store.int("config", "plugins", "ai_chat", "max_history", def = 8)
            val ttlMinutes = Store.int("config", "plugins", "ai_chat", "history_ttl_minutes", def = 30)
            val forever = maxHistory == 0 && ttlMinutes == 0

            top.yukonga.miuix.kmp.preference.SwitchPreference(
                title = "永久记忆（不限制轮数、不遗忘）",
                summary = if (forever) "已开启：记住所有轮次，永不自动遗忘" else "关闭：默认记住 8 轮、30 分钟后忘记",
                checked = forever,
                onCheckedChange = { on ->
                    scope.launch {
                        val values = JSONObject()
                        if (on) {
                            values.put("max_history", 0)
                            values.put("history_ttl_minutes", 0)
                        } else {
                            values.put("max_history", 8)
                            values.put("history_ttl_minutes", 30)
                        }
                        Store.patch(
                            patchOf("plugins", "ai_chat", value = values),
                            if (on) "已开启永久记忆" else "已恢复默认记忆（8 轮 / 30 分钟）",
                        )
                    }
                },
            )
            EditPreference(
                "记住几轮对话",
                maxHistory.toString(),
                summary = "0 = 不限制轮数（永久记住）",
                numeric = true,
            ) {
                it.toIntOrNull()?.let { v ->
                    Store.patch(patchOf("plugins", "ai_chat", "max_history", value = v.coerceIn(0, 1000)))
                }
            }
            EditPreference(
                "多久后忘掉（分钟）",
                ttlMinutes.toString(),
                summary = "0 = 永不忘记",
                numeric = true,
            ) {
                it.toIntOrNull()?.let { v ->
                    Store.patch(patchOf("plugins", "ai_chat", "history_ttl_minutes", value = v.coerceIn(0, 525600)))
                }
            }
            Hint("对话记忆按「群 / 私聊的人」保存，和当前模型无关；换模型后仍记得前面聊过什么。清空记忆用下面的按钮。")
            ArrowPreference(
                title = "清空所有对话记忆",
                onClick = { scope.launch { com.qqbot.console.Api.post("/api/context/clear"); Store.toast = "已清空" } },
            )
        }
    }

    accountDialog?.let { id ->
        AccountDialog(
            providerId = id,
            onDismiss = { accountDialog = null },
            onSwitchToApiKey = {
                accountDialog = null
                Store.toast = "把「接口设置 → 认证方式」改成 API Key，再填上 API Key"
            },
        )
    }

    val pickerProfile = editing
    if (modelPicker && pickerProfile != null) {
        ModelPickerDialog(
            profileId = pickerProfile.optStr("id"),
            profileName = pickerProfile.optStr("name"),
            current = pickerProfile.optStr("model"),
            onPick = { value ->
                saveProfileField("model", value)
                Store.toast = if (value.isBlank()) "模型改为跟随账号默认" else "模型已改为 $value"
            },
            onDismiss = { modelPicker = false },
        )
    }
}

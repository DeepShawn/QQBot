package com.qqbot.console.ui

import android.graphics.BitmapFactory
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.PickVisualMediaRequest
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.Image
import androidx.compose.foundation.border
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyRow
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
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.ImageBitmap
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.compose.foundation.shape.RoundedCornerShape
import com.qqbot.console.AccountsStore
import com.qqbot.console.AvatarApi
import com.qqbot.console.PROVIDER_NAMES
import com.qqbot.console.PROVIDER_ORDER
import com.qqbot.console.Store
import com.qqbot.console.objects
import com.qqbot.console.patchOf
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.json.JSONArray
import org.json.JSONObject
import kotlin.math.max
import top.yukonga.miuix.kmp.basic.ButtonDefaults
import top.yukonga.miuix.kmp.basic.ScrollBehavior
import top.yukonga.miuix.kmp.basic.Text
import top.yukonga.miuix.kmp.basic.TextButton
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

    val ctx = LocalContext.current
    val avatar = ai.optJSONObject("avatar") ?: JSONObject()
    val presets = ai.optJSONArray("presets")?.objects().orEmpty()
    val activePresetId = ai.optStr("active_preset").ifBlank { "custom" }
    val activePreset = if (activePresetId == "custom") null else presets.firstOrNull { it.optStr("id") == activePresetId }
    var presetDetail by remember { mutableStateOf<JSONObject?>(null) }

    fun switchPreset(id: String, name: String) {
        scope.launch {
            Store.patch(patchOf("plugins", "ai_chat", "active_preset", value = id), "已切换人格：$name")
        }
    }

    /** 把预设内容复制到顶层字段并切到「自定义」，之后可以自由编辑。 */
    fun copyPresetToCustom(preset: JSONObject) {
        val values = JSONObject()
            .put("active_preset", "custom")
            .put("system_prompt", preset.optStr("system_prompt"))
            .put("avatar", JSONObject().put("prompt", preset.optStr("avatar_prompt")))
        scope.launch {
            Store.patch(patchOf("plugins", "ai_chat", value = values), "已复制为自定义人格，可继续编辑")
        }
    }
    var refBytes by remember { mutableStateOf<ByteArray?>(null) }
    var refError by remember { mutableStateOf("") }
    var refBusy by remember { mutableStateOf(false) }

    val refPicker = rememberLauncherForActivityResult(ActivityResultContracts.PickVisualMedia()) { uri ->
        if (uri != null) {
            scope.launch {
                refBusy = true
                refError = ""
                var picked: ByteArray? = null
                val err = withContext(Dispatchers.IO) {
                    try {
                        val bytes = ctx.contentResolver.openInputStream(uri)?.use { it.readBytes() }
                        when {
                            bytes == null -> "读取图片失败"
                            bytes.size > 8 * 1024 * 1024 -> "图片太大（超过 8MB），换一张小一点的"
                            else -> {
                                picked = bytes
                                AvatarApi.upload(bytes, "reference.png")
                            }
                        }
                    } catch (e: Exception) {
                        "读取图片失败：${e.message ?: "未知错误"}"
                    }
                }
                refBusy = false
                val bytes = picked
                if (err == null && bytes != null) {
                    refBytes = bytes
                    Store.toast = "参考图已保存"
                } else {
                    refError = err ?: "上传参考图失败"
                    Store.toast = refError
                }
            }
        }
    }

    // 进页面时拉一次账号状态（登录 / 退出后也会刷新），顺便探测有没有参考图（404 = 没有）
    LaunchedEffect(Unit) {
        AccountsStore.refresh()
        val ref = AvatarApi.fetch()
        refBytes = ref.bytes
        refError = ref.error
    }

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
            val preset = activePreset
            if (preset == null) {
                EditPreference("人格提示词", ai.optStr("system_prompt"), multiline = true) {
                    Store.patch(patchOf("plugins", "ai_chat", "system_prompt", value = it))
                }
                if (activePresetId != "custom") {
                    Hint("当前预设「$activePresetId」不可用，先按自定义人格显示。")
                }
            } else {
                ArrowPreference(
                    title = "人格提示词（跟随预设：${preset.optStr("name")}）",
                    summary = preset.optStr("system_prompt").replace("\n", " ").take(40)
                        .ifBlank { "（这个预设没有单独的人格提示词）" },
                    onClick = { presetDetail = preset },
                )
                TextButton(
                    text = "编辑为自定义",
                    onClick = { copyPresetToCustom(preset) },
                    modifier = Modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 8.dp),
                )
                Hint("当前人格由预设「${preset.optStr("name")}」提供；要自由修改，点「编辑为自定义」把内容复制过来。")
            }
        }

        group("人格预设") {
            if (presets.isEmpty()) {
                Hint("预设暂时不可用（等后端接入）；先使用下面的「自定义」人格。")
            }
            LazyRow(
                Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.spacedBy(10.dp),
                contentPadding = PaddingValues(horizontal = 16.dp, vertical = 12.dp),
            ) {
                item(key = "custom") {
                    PresetCard(
                        emoji = "✨",
                        name = "自定义",
                        desc = ai.optStr("system_prompt").replace("\n", " ").take(30)
                            .ifBlank { "使用你自己写的提示词" },
                        selected = activePresetId == "custom",
                        onClick = { switchPreset("custom", "自定义") },
                        onLongPress = {
                            presetDetail = JSONObject()
                                .put("name", "自定义")
                                .put("emoji", "✨")
                                .put("system_prompt", ai.optStr("system_prompt"))
                                .put("avatar_prompt", avatar.optStr("prompt"))
                        },
                    )
                }
                items(presets.size) { index ->
                    val p = presets[index]
                    val id = p.optStr("id")
                    val pName = p.optStr("name").ifBlank { id }
                    PresetCard(
                        emoji = p.optStr("emoji"),
                        name = pName,
                        desc = p.optStr("system_prompt").replace("\n", " ").take(30),
                        selected = id == activePresetId,
                        onClick = { switchPreset(id, pName) },
                        onLongPress = { presetDetail = p },
                    )
                }
            }
            Hint("点击卡片立即切换人格；长按查看该预设的完整人格 / 形象提示词。")
        }

        group("机器人形象") {
            val preset = activePreset
            if (preset == null) {
                EditPreference(
                    "形象提示词",
                    avatar.optStr("prompt"),
                    summary = "生成机器人头像 / 立绘时作为外观约束（最多 2000 字）",
                    multiline = true,
                ) { text ->
                    val trimmed = text.trim()
                    Store.patch(
                        patchOf("plugins", "ai_chat", "avatar", "prompt", value = trimmed.take(2000)),
                        "形象提示词已保存",
                    )
                    if (trimmed.length > 2000) Store.toast = "最多 2000 字，已截断保存"
                }
                Hint("例：蓝白配色、鲸鱼娘、圆脸、戴耳机。生成机器人形象 / 头像时会作为外观约束。")
            } else {
                ArrowPreference(
                    title = "形象提示词（跟随预设：${preset.optStr("name")}）",
                    summary = preset.optStr("avatar_prompt").replace("\n", " ").take(40)
                        .ifBlank { "（这个预设没有单独的形象提示词）" },
                    onClick = { presetDetail = preset },
                )
                TextButton(
                    text = "编辑为自定义",
                    onClick = { copyPresetToCustom(preset) },
                    modifier = Modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 8.dp),
                )
                Hint("形象提示词跟随预设；点「编辑为自定义」可复制到自定义后修改。参考图仍按下面单独设置。")
            }

            val thumb = remember(refBytes) { refBytes?.let { decodeThumbnail(it) } }
            if (thumb != null) {
                Image(
                    bitmap = thumb,
                    contentDescription = "参考图",
                    contentScale = ContentScale.Crop,
                    modifier = Modifier
                        .fillMaxWidth()
                        .padding(horizontal = 16.dp)
                        .height(180.dp)
                        .clip(RoundedCornerShape(16.dp))
                        .border(1.dp, GlassText.divider, RoundedCornerShape(16.dp)),
                )
                Row(
                    Modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 12.dp),
                    horizontalArrangement = Arrangement.spacedBy(10.dp),
                ) {
                    TextButton(
                        text = if (refBusy) "处理中…" else "更换参考图",
                        onClick = {
                            refPicker.launch(
                                PickVisualMediaRequest(ActivityResultContracts.PickVisualMedia.ImageOnly),
                            )
                        },
                        modifier = Modifier.weight(1f),
                    )
                    TextButton(
                        text = "移除",
                        onClick = {
                            scope.launch {
                                refBusy = true
                                refError = ""
                                val err = AvatarApi.delete()
                                refBusy = false
                                if (err == null) {
                                    refBytes = null
                                    Store.toast = "参考图已移除"
                                } else {
                                    refError = err
                                    Store.toast = err
                                }
                            }
                        },
                        modifier = Modifier.weight(1f),
                    )
                }
            } else {
                TextButton(
                    text = if (refBusy) "处理中…" else "选择参考图",
                    onClick = {
                        refPicker.launch(
                            PickVisualMediaRequest(ActivityResultContracts.PickVisualMedia.ImageOnly),
                        )
                    },
                    modifier = Modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 12.dp),
                    colors = ButtonDefaults.textButtonColorsPrimary(),
                )
            }
            if (refError.isNotBlank()) {
                Text(
                    refError,
                    fontSize = 13.sp,
                    color = Warn,
                    modifier = Modifier.padding(horizontal = 16.dp, vertical = 4.dp),
                )
            }
            Hint("参考图只在生成机器人形象 / 头像时作参考，不会公开；生成功能在「画图」工具里生效。")
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

    presetDetail?.let { preset ->
        PresetDetailDialog(preset) { presetDetail = null }
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

/** 解码参考图缩略图（限制到约 720px，避免大图占用内存）。 */
private fun decodeThumbnail(bytes: ByteArray, maxSide: Int = 720): ImageBitmap? = try {
    val bounds = BitmapFactory.Options().apply { inJustDecodeBounds = true }
    BitmapFactory.decodeByteArray(bytes, 0, bytes.size, bounds)
    val w = bounds.outWidth
    val h = bounds.outHeight
    var sample = 1
    if (w > 0 && h > 0) {
        while (max(w, h) / sample > maxSide * 2) sample *= 2
    }
    val opts = BitmapFactory.Options().apply { inSampleSize = sample }
    BitmapFactory.decodeByteArray(bytes, 0, bytes.size, opts)?.asImageBitmap()
} catch (_: Throwable) {
    null
}

/** 人格预设卡片：emoji + 名称 + 简介；选中高亮。点按切换、长按看详情。 */
@Composable
private fun PresetCard(
    emoji: String,
    name: String,
    desc: String,
    selected: Boolean,
    onClick: () -> Unit,
    onLongPress: () -> Unit,
) {
    val accent = LocalAccent.current
    val shape = RoundedCornerShape(20.dp)
    Column(
        Modifier
            .width(150.dp)
            .glass(
                level = if (selected) GlassLevel.Regular else GlassLevel.Thin,
                shape = shape,
                tint = if (selected) accent.primary else Color.Unspecified,
            )
            .then(
                if (selected) Modifier.border(1.dp, accent.primary.copy(alpha = 0.75f), shape) else Modifier,
            )
            .pointerInput(name, selected) {
                detectTapGestures(onTap = { onClick() }, onLongPress = { onLongPress() })
            }
            .padding(14.dp),
    ) {
        Text(emoji.ifBlank { "🤖" }, fontSize = 24.sp)
        Spacer(Modifier.height(6.dp))
        Text(
            name,
            fontSize = 16.sp,
            fontWeight = FontWeight.SemiBold,
            color = if (selected) accent.primary else GlassText.primary,
        )
        Spacer(Modifier.height(4.dp))
        Text(desc, fontSize = 12.sp, color = GlassText.secondary, maxLines = 3)
    }
}

/** 预设详情（只读）：完整人格提示词 + 形象提示词。 */
@Composable
private fun PresetDetailDialog(preset: JSONObject, onClose: () -> Unit) {
    val name = preset.optStr("name").ifBlank { "预设" }
    val emoji = preset.optStr("emoji")
    val system = preset.optStr("system_prompt")
    val avatarPrompt = preset.optStr("avatar_prompt")
    GlassDialog(
        show = true,
        title = (emoji.ifBlank { "🤖" }) + " " + name,
        onDismissRequest = onClose,
    ) {
        Column(
            Modifier.fillMaxWidth().heightIn(max = 480.dp).verticalScroll(rememberScrollState()),
        ) {
            Text("人格提示词", fontSize = 14.sp, fontWeight = FontWeight.Medium, color = GlassText.primary)
            Spacer(Modifier.height(6.dp))
            Text(
                system.ifBlank { "（没有单独的人格提示词，使用自定义内容）" },
                fontSize = 13.sp,
                color = GlassText.secondary,
            )
            Spacer(Modifier.height(14.dp))
            Text("形象提示词", fontSize = 14.sp, fontWeight = FontWeight.Medium, color = GlassText.primary)
            Spacer(Modifier.height(6.dp))
            Text(
                avatarPrompt.ifBlank { "（没有单独的形象提示词）" },
                fontSize = 13.sp,
                color = GlassText.secondary,
            )
            Spacer(Modifier.height(16.dp))
            GlassButton(text = "关闭", onClick = onClose, modifier = Modifier.fillMaxWidth())
        }
    }
}

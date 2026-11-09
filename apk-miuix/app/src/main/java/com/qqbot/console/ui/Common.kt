package com.qqbot.console.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ColumnScope
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.LazyListScope
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.input.nestedscroll.nestedScroll
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.qqbot.console.Store
import kotlinx.coroutines.launch
import org.json.JSONObject
import top.yukonga.miuix.kmp.basic.BasicComponent
import top.yukonga.miuix.kmp.basic.BasicComponentDefaults
import top.yukonga.miuix.kmp.basic.ScrollBehavior
import top.yukonga.miuix.kmp.basic.SmallTitle
import top.yukonga.miuix.kmp.basic.Text
import top.yukonga.miuix.kmp.basic.SwitchDefaults
import top.yukonga.miuix.kmp.basic.TextField
import top.yukonga.miuix.kmp.basic.TextFieldDefaults
import top.yukonga.miuix.kmp.preference.SwitchPreference
import top.yukonga.miuix.kmp.theme.MiuixTheme
import top.yukonga.miuix.kmp.utils.overScrollVertical

val Good = GlassSuccess
val Warn = GlassWarning
val Bad = GlassDanger

/** 带 MIUI 回弹 + 折叠大标题联动的页面列表。 */
@Composable
fun PageList(
    padding: PaddingValues,
    scrollBehavior: ScrollBehavior,
    content: LazyListScope.() -> Unit,
) {
    LazyColumn(
        modifier = Modifier
            .fillMaxSize()
            .overScrollVertical()
            .scrollEdgeFade()
            .nestedScroll(scrollBehavior.nestedScrollConnection),
        contentPadding = PaddingValues(top = padding.calculateTopPadding(), bottom = padding.calculateBottomPadding() + 24.dp),
        content = content,
    )
}

/** 分组卡片：小标题 + Thin 玻璃圆角卡片。 */
fun LazyListScope.group(title: String? = null, content: @Composable ColumnScope.() -> Unit) {
    item {
        if (title != null) {
            SmallTitle(
                text = title,
                textColor = GlassText.secondary,
                insideMargin = PaddingValues(start = 28.dp, end = 16.dp, top = 20.dp, bottom = 8.dp),
            )
        }
        GlassCard(modifier = Modifier.padding(horizontal = 16.dp).padding(bottom = 12.dp)) { content() }
    }
}

@Composable
fun Dot(color: Color) {
    GlassDot(color)
}

/** 状态行：左侧标题，右侧彩色小圆点 + 文字。 */
@Composable
fun StatusRow(title: String, value: String, color: Color) {
    BasicComponent(
        title = title,
        endActions = {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Dot(color)
                Spacer(Modifier.padding(start = 6.dp))
                Text(value, color = color, fontSize = 15.sp)
            }
        },
    )
}

@Composable
fun InfoRow(title: String, value: String) {
    BasicComponent(
        title = title,
        endActions = { Text(value, color = GlassText.secondary, fontSize = 15.sp) },
    )
}

/** 绑定到配置字段的开关（立即保存）。 */
@Composable
fun ConfigSwitch(title: String, summary: String? = null, vararg path: String, default: Boolean = false) {
    val scope = rememberCoroutineScope()
    val checked = Store.bool("config", *path, def = default)
    SwitchPreference(
        title = title,
        titleColor = BasicComponentDefaults.titleColor(color = GlassText.primary),
        summary = summary,
        summaryColor = BasicComponentDefaults.summaryColor(color = GlassText.secondary),
        checked = checked,
        switchColors = SwitchDefaults.switchColors(
            checkedThumbColor = Color.White,
            uncheckedThumbColor = Color(0xFFE8EBF2),
            disabledCheckedThumbColor = Color(0x99FFFFFF),
            disabledUncheckedThumbColor = Color(0x99E8EBF2),
            checkedTrackColor = GlassAccent,
            uncheckedTrackColor = Color(0x29FFFFFF),
            disabledCheckedTrackColor = Color(0x665B8CFF),
            disabledUncheckedTrackColor = Color(0x14FFFFFF),
        ),
        onCheckedChange = { v -> scope.launch { Store.patch(com.qqbot.console.patchOf(*path, value = v), null) } },
    )
}

/**
 * 点一下弹出编辑框的配置项（文本 / 数字 / 多行 / 密钥）。
 * secret = true 时只显示掩码，不回填明文；留空保存表示不修改。
 */
@Composable
fun EditPreference(
    title: String,
    current: String,
    summary: String? = null,
    multiline: Boolean = false,
    secret: Boolean = false,
    numeric: Boolean = false,
    onSave: suspend (String) -> Unit,
) {
    var show by remember { mutableStateOf(false) }
    var text by remember { mutableStateOf("") }
    val scope = rememberCoroutineScope()
    val shown = when {
        secret -> if (current.isBlank()) "未设置" else current
        current.isBlank() -> "未设置"
        multiline -> current.replace("\n", " ").take(28) + if (current.length > 28) "…" else ""
        else -> current.take(28) + if (current.length > 28) "…" else ""
    }
    top.yukonga.miuix.kmp.preference.ArrowPreference(
        title = title,
        titleColor = BasicComponentDefaults.titleColor(color = GlassText.primary),
        summary = summary ?: shown,
        summaryColor = BasicComponentDefaults.summaryColor(color = GlassText.secondary),
        endActions = { if (summary != null) Text(shown.take(16), color = GlassText.secondary, fontSize = 14.sp) },
        onClick = {
            text = if (secret) "" else current
            show = true
        },
    )
    GlassDialog(
        show = show,
        title = title,
        summary = if (secret) "输入新的值；留空则不修改" else null,
        onDismissRequest = { show = false },
    ) {
        TextField(
            value = text,
            onValueChange = { text = it },
            label = if (secret) "新的 $title" else title,
            singleLine = !multiline,
            minLines = if (multiline) 5 else 1,
            maxLines = if (multiline) 10 else 1,
            cornerRadius = GlassRadius.md,
            colors = TextFieldDefaults.textFieldColors(
                backgroundColor = GlassTokens.insetFill,
                labelColor = GlassText.tertiary,
                borderColor = GlassText.divider,
            ),
            keyboardOptions = if (numeric) androidx.compose.foundation.text.KeyboardOptions(
                keyboardType = androidx.compose.ui.text.input.KeyboardType.Number,
            ) else androidx.compose.foundation.text.KeyboardOptions.Default,
            modifier = Modifier.fillMaxWidth().padding(bottom = 16.dp),
        )
        DialogButtons(
            onCancel = { show = false },
            onOk = {
                show = false
                scope.launch { onSave(text.trim()) }
            },
        )
    }
}

@Composable
fun DialogButtons(onCancel: () -> Unit, onOk: () -> Unit, okText: String = "保存") {
    Row(horizontalArrangement = Arrangement.spacedBy(12.dp)) {
        GlassButton(text = "取消", onClick = onCancel, modifier = Modifier.weight(1f), primary = false)
        GlassButton(text = okText, onClick = onOk, modifier = Modifier.weight(1f), primary = true)
    }
}

/** 二次确认对话框。 */
@Composable
fun ConfirmDialog(show: Boolean, title: String, summary: String, okText: String, onDismiss: () -> Unit, onOk: () -> Unit) {
    GlassDialog(show = show, title = title, summary = summary, onDismissRequest = onDismiss) {
        DialogButtons(onCancel = onDismiss, onOk = { onDismiss(); onOk() }, okText = okText)
    }
}

@Composable
fun Hint(text: String) {
    Text(
        text,
        fontSize = 13.sp,
        color = GlassText.secondary,
        modifier = Modifier.padding(horizontal = 28.dp, vertical = 4.dp),
    )
}

@Composable
fun BigStatus(title: String, subtitle: String, color: Color) {
    Column(Modifier.fillMaxWidth().padding(horizontal = 20.dp, vertical = 20.dp)) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Box(Modifier.size(14.dp).clip(CircleShape).background(color))
            Spacer(Modifier.padding(start = 12.dp))
            Text(title, fontSize = 24.sp, fontWeight = FontWeight.Bold, color = color)
        }
        Spacer(Modifier.height(6.dp))
        Text(subtitle, fontSize = 14.sp, color = GlassText.secondary)
    }
}

fun JSONObject.optStr(key: String): String = if (isNull(key)) "" else optString(key, "")

fun fmtUptime(sec: Int): String = when {
    sec <= 0 -> "—"
    sec < 60 -> "${sec} 秒"
    sec < 3600 -> "${sec / 60} 分 ${sec % 60} 秒"
    sec < 86400 -> "${sec / 3600} 小时 ${(sec % 3600) / 60} 分"
    else -> "${sec / 86400} 天 ${(sec % 86400) / 3600} 小时"
}

package com.qqbot.console.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
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
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.qqbot.console.Api
import java.net.URLEncoder
import top.yukonga.miuix.kmp.basic.ButtonDefaults
import top.yukonga.miuix.kmp.basic.InfiniteProgressIndicator
import top.yukonga.miuix.kmp.basic.Text
import top.yukonga.miuix.kmp.basic.TextButton
import top.yukonga.miuix.kmp.basic.TextField
import top.yukonga.miuix.kmp.overlay.OverlayDialog
import top.yukonga.miuix.kmp.preference.ArrowPreference
import top.yukonga.miuix.kmp.theme.MiuixTheme

/**
 * 模型选择弹窗：GET /api/models?profile=<id> 拉列表，点一个就保存并关闭；
 * 底部保留「手动输入」，应付接口列表拿不到或者要填自定义模型的情况。
 */
@Composable
fun ModelPickerDialog(
    profileId: String,
    profileName: String,
    current: String,
    onPick: (String) -> Unit,
    onDismiss: () -> Unit,
) {
    var loading by remember { mutableStateOf(true) }
    var error by remember { mutableStateOf("") }
    var models by remember { mutableStateOf<List<String>>(emptyList()) }
    var defaultModel by remember { mutableStateOf("") }
    var manual by remember { mutableStateOf(false) }
    var manualText by remember { mutableStateOf(current) }
    var reload by remember { mutableIntStateOf(0) }

    LaunchedEffect(profileId, reload) {
        loading = true
        error = ""
        val path = "/api/models?profile=" + URLEncoder.encode(profileId, "UTF-8")
        val r = Api.get(path)
        loading = false
        when {
            r == null -> error = "管理服务没有响应，检查机器人服务是否在运行"
            !r.optBoolean("ok") -> error = r.optStr("error").ifBlank { "获取模型列表失败" }
            else -> {
                val arr = r.optJSONArray("models")
                models = (0 until (arr?.length() ?: 0))
                    .mapNotNull { i -> arr?.optString(i)?.trim()?.takeIf(String::isNotEmpty) }
                defaultModel = r.optStr("default")
            }
        }
    }

    GlassDialog(
        show = true,
        title = "选择模型",
        onDismissRequest = onDismiss,
    ) {
        Column(Modifier.fillMaxWidth()) {
            Text(
                "接口：${profileName.ifBlank { profileId }}",
                fontSize = 13.sp,
                color = MiuixTheme.colorScheme.onSurfaceVariantSummary,
            )
            Spacer(Modifier.height(8.dp))

            when {
                loading -> {
                    InfiniteProgressIndicator()
                    Spacer(Modifier.height(10.dp))
                    Text(
                        "正在获取模型列表…",
                        fontSize = 13.sp,
                        color = MiuixTheme.colorScheme.onSurfaceVariantSummary,
                    )
                }

                error.isNotBlank() -> {
                    Text(error, fontSize = 13.sp, color = Bad)
                    Spacer(Modifier.height(12.dp))
                    TextButton(
                        text = "重试",
                        onClick = { reload++ },
                        modifier = Modifier.fillMaxWidth(),
                        colors = ButtonDefaults.textButtonColorsPrimary(),
                    )
                }

                else -> {
                    Column(
                        Modifier.fillMaxWidth().heightIn(max = 320.dp).verticalScroll(rememberScrollState()),
                    ) {
                        if (defaultModel.isNotBlank()) {
                            ArrowPreference(
                                title = "跟随账号默认",
                                summary = defaultModel,
                                endActions = {
                                    if (current.isBlank()) {
                                        Text("当前", color = Good, fontSize = 14.sp)
                                    }
                                },
                                onClick = {
                                    onPick("")
                                    onDismiss()
                                },
                            )
                        }
                        models.forEach { m ->
                            ArrowPreference(
                                title = m,
                                summary = if (m == defaultModel) "账号默认" else null,
                                endActions = {
                                    if (m == current) {
                                        Text("当前", color = Good, fontSize = 14.sp)
                                    }
                                },
                                onClick = {
                                    onPick(m)
                                    onDismiss()
                                },
                            )
                        }
                        if (models.isEmpty() && defaultModel.isBlank()) {
                            Text(
                                "没有获取到模型列表，可以手动输入。",
                                fontSize = 13.sp,
                                color = MiuixTheme.colorScheme.onSurfaceVariantSummary,
                                modifier = Modifier.padding(horizontal = 16.dp, vertical = 12.dp),
                            )
                        }
                    }
                }
            }

            Spacer(Modifier.height(12.dp))
            if (manual) {
                GlassTextField(
                    value = manualText,
                    onValueChange = { manualText = it },
                    label = "模型名称",
                    singleLine = true,
                    modifier = Modifier.fillMaxWidth().padding(bottom = 12.dp),
                )
                Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                    TextButton(
                        text = "取消",
                        onClick = { manual = false },
                        modifier = Modifier.weight(1f),
                    )
                    TextButton(
                        text = "保存",
                        onClick = {
                            val v = manualText.trim()
                            if (v.isNotBlank()) {
                                onPick(v)
                                onDismiss()
                            }
                        },
                        modifier = Modifier.weight(1f),
                        colors = ButtonDefaults.textButtonColorsPrimary(),
                    )
                }
            } else {
                Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                    TextButton(
                        text = "手动输入",
                        onClick = {
                            manual = true
                            manualText = current
                        },
                        modifier = Modifier.weight(1f),
                    )
                    TextButton(
                        text = "关闭",
                        onClick = onDismiss,
                        modifier = Modifier.weight(1f),
                    )
                }
            }
        }
    }
}

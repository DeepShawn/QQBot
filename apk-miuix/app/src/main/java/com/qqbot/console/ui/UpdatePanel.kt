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
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.qqbot.console.AppUpdates
import kotlinx.coroutines.launch
import top.yukonga.miuix.kmp.basic.ButtonDefaults
import top.yukonga.miuix.kmp.basic.LinearProgressIndicator
import top.yukonga.miuix.kmp.basic.Text
import top.yukonga.miuix.kmp.basic.TextButton
import top.yukonga.miuix.kmp.overlay.OverlayDialog
import top.yukonga.miuix.kmp.theme.MiuixTheme

/** App 新版本弹窗（启动自动检查 / 更多页手动检查都会用到）。 */
@Composable
fun UpdatePromptHost() {
    val ctx = LocalContext.current
    val scope = rememberCoroutineScope()
    val info = AppUpdates.available ?: return

    GlassDialog(
        show = AppUpdates.prompt,
        title = "发现新版本 ${info.version}" + (if (info.isLocal) "（本地）" else ""),
        onDismissRequest = { if (!AppUpdates.downloading) AppUpdates.prompt = false },
    ) {
        Column(Modifier.fillMaxWidth()) {
            Text(
                "当前版本 ${AppUpdates.currentVersion}",
                fontSize = 13.sp,
                color = MiuixTheme.colorScheme.onSurfaceVariantSummary,
            )
            Spacer(Modifier.height(10.dp))

            if (AppUpdates.downloading) {
                Text(AppUpdates.statusText, fontSize = 13.sp)
                Spacer(Modifier.height(10.dp))
                LinearProgressIndicator(progress = AppUpdates.progress, modifier = Modifier.fillMaxWidth())
            } else {
                if (info.isLocal) {
                    Text(
                        "本地更新包：不联网，直接从手机存储安装。",
                        fontSize = 13.sp,
                        color = Good,
                    )
                    Spacer(Modifier.height(8.dp))
                }
                Text("更新说明", fontSize = 14.sp, fontWeight = FontWeight.Medium)
                Spacer(Modifier.height(6.dp))
                Column(
                    Modifier.fillMaxWidth().heightIn(max = 220.dp).verticalScroll(rememberScrollState()),
                ) {
                    Text(
                        info.notes.ifBlank { "（这个版本没有更新说明）" },
                        fontSize = 13.sp,
                        fontFamily = FontFamily.Monospace,
                        color = MiuixTheme.colorScheme.onSurfaceVariantSummary,
                    )
                }
            }

            if (AppUpdates.error.isNotBlank()) {
                Spacer(Modifier.height(10.dp))
                Text(AppUpdates.error, fontSize = 13.sp, color = Bad)
            }

            Spacer(Modifier.height(16.dp))
            Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                TextButton(
                    text = "稍后",
                    onClick = { if (!AppUpdates.downloading) AppUpdates.prompt = false },
                    modifier = Modifier.weight(1f),
                )
                TextButton(
                    text = when {
                        AppUpdates.downloading -> "处理中…"
                        info.isLocal -> "立即安装"
                        else -> "立即更新"
                    },
                    onClick = { scope.launch { AppUpdates.downloadAndInstall(ctx) } },
                    modifier = Modifier.weight(1f),
                    colors = ButtonDefaults.textButtonColorsPrimary(),
                )
            }
            if (!AppUpdates.downloading) {
                Spacer(Modifier.height(4.dp))
                TextButton(
                    text = "跳过这个版本",
                    onClick = {
                        AppUpdates.skipVersion(ctx, info.version)
                        AppUpdates.prompt = false
                    },
                    modifier = Modifier.fillMaxWidth(),
                )
            }
            Spacer(Modifier.height(4.dp))
        }
    }
}

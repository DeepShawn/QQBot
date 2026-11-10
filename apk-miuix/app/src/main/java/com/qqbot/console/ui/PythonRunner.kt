package com.qqbot.console.ui

import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
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
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.qqbot.console.Api
import com.qqbot.console.Store
import kotlinx.coroutines.launch
import org.json.JSONObject
import top.yukonga.miuix.kmp.basic.InfiniteProgressIndicator
import top.yukonga.miuix.kmp.basic.Text
import top.yukonga.miuix.kmp.basic.TextField
import top.yukonga.miuix.kmp.overlay.OverlayDialog
import top.yukonga.miuix.kmp.theme.MiuixTheme

/**
 * Python 运行器：把一段代码 POST 到 /api/python/run，在机器人运行环境里执行，
 * 显示 stdout / stderr / 退出码 / 耗时，可复制输出或清空。
 */
@Composable
fun PythonRunnerDialog(onDismiss: () -> Unit) {
    val ctx = LocalContext.current
    val scope = rememberCoroutineScope()

    var code by remember { mutableStateOf("") }
    var running by remember { mutableStateOf(false) }
    var stdout by remember { mutableStateOf("") }
    var stderr by remember { mutableStateOf("") }
    var exitCode by remember { mutableStateOf(0) }
    var durationMs by remember { mutableStateOf(0L) }
    var timedOut by remember { mutableStateOf(false) }
    var error by remember { mutableStateOf("") }
    var hasResult by remember { mutableStateOf(false) }

    fun clearResult() {
        stdout = ""
        stderr = ""
        exitCode = 0
        durationMs = 0L
        timedOut = false
        error = ""
        hasResult = false
    }

    fun runCode() {
        if (running) return
        if (code.isBlank()) {
            Store.toast = "先写点 Python 代码"
            return
        }
        running = true
        error = ""
        hasResult = false
        scope.launch {
            val r = try {
                Api.post(
                    "/api/python/run",
                    JSONObject().put("code", code).put("timeout", 30),
                )
            } catch (e: Exception) {
                null
            }
            running = false
            if (r == null) {
                error = "管理服务没有响应，检查机器人服务是否在运行"
                return@launch
            }
            if (!r.optBoolean("ok")) {
                error = r.optStr("error").ifBlank { "运行失败，稍后再试" }
                return@launch
            }
            stdout = r.optStr("stdout")
            stderr = r.optStr("stderr")
            exitCode = r.optInt("exit_code", 0)
            durationMs = r.optLong("duration_ms", 0L)
            timedOut = r.optBoolean("timed_out")
            hasResult = true
        }
    }

    fun copyOutput() {
        val out = buildString {
            if (stdout.isNotBlank()) append(stdout)
            if (stderr.isNotBlank()) {
                if (isNotEmpty()) append("\n")
                append(stderr)
            }
        }
        copyToClipboard(ctx, out.ifBlank { "（没有输出）" })
        Store.toast = "已复制输出"
    }

    GlassDialog(
        show = true,
        title = "运行 Python 代码",
        onDismissRequest = { if (!running) onDismiss() },
    ) {
        Column(
            Modifier.fillMaxWidth().heightIn(max = 560.dp).verticalScroll(rememberScrollState()),
        ) {
            Text(
                "在机器人的运行环境里执行，最长 30 秒。",
                fontSize = 13.sp,
                color = MiuixTheme.colorScheme.onSurfaceVariantSummary,
            )
            Spacer(Modifier.height(10.dp))

            GlassTextField(
                value = code,
                onValueChange = { code = it },
                label = "Python 代码",
                modifier = Modifier.fillMaxWidth().heightIn(min = 190.dp).padding(bottom = 4.dp),
                singleLine = false,
                minLines = 10,
                maxLines = 20,
                mono = true,
            )

            if (running) {
                Spacer(Modifier.height(10.dp))
                InfiniteProgressIndicator()
                Spacer(Modifier.height(6.dp))
                Text(
                    "正在运行…",
                    fontSize = 13.sp,
                    color = MiuixTheme.colorScheme.onSurfaceVariantSummary,
                )
            }

            if (error.isNotBlank()) {
                Spacer(Modifier.height(10.dp))
                Text(error, fontSize = 13.sp, color = Bad)
            }

            if (hasResult) {
                Spacer(Modifier.height(12.dp))
                Text(
                    "退出码 $exitCode · 耗时 ${durationMs} ms" +
                        if (timedOut) " · 运行超时被终止" else "",
                    fontSize = 13.sp,
                    color = if (exitCode == 0 && !timedOut) Good else Warn,
                )
                if (stdout.isNotBlank()) {
                    Spacer(Modifier.height(10.dp))
                    Text("标准输出", fontSize = 13.sp, fontWeight = FontWeight.Medium)
                    Spacer(Modifier.height(4.dp))
                    InkBox(Modifier.fillMaxWidth()) {
                        Text(
                            stdout.takeLast(12000),
                            modifier = Modifier.padding(12.dp),
                            fontSize = 11.sp,
                            fontFamily = FontFamily.Monospace,
                            color = GlassText.mono,
                        )
                    }
                } else if (!timedOut) {
                    Spacer(Modifier.height(6.dp))
                    Text(
                        "（没有输出）",
                        fontSize = 13.sp,
                        color = MiuixTheme.colorScheme.onSurfaceVariantSummary,
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
                Spacer(Modifier.height(12.dp))
                Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                    GlassButton(text = "复制输出", onClick = { copyOutput() }, modifier = Modifier.weight(1f), primary = false)
                    GlassButton(text = "清空结果", onClick = { clearResult() }, modifier = Modifier.weight(1f), primary = false)
                }
            }

            Spacer(Modifier.height(12.dp))
            Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                GlassButton(text = if (running) "运行中…" else "运行", onClick = { runCode() }, modifier = Modifier.weight(1f), primary = true)
                GlassButton(text = "清空", onClick = {
                        if (!running) {
                            code = ""
                            clearResult()
                        }
                    }, modifier = Modifier.weight(1f), primary = false)
                GlassButton(text = "关闭", onClick = { if (!running) onDismiss() }, modifier = Modifier.weight(1f), primary = false)
            }
            Spacer(Modifier.height(4.dp))
        }
    }
}

private fun copyToClipboard(ctx: Context, text: String) {
    try {
        val cm = ctx.getSystemService(ClipboardManager::class.java) ?: return
        cm.setPrimaryClip(ClipData.newPlainText("Python 输出", text))
    } catch (_: Exception) {
    }
}

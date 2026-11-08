package com.qqbot.console.ui

import android.content.Intent
import android.net.Uri
import androidx.compose.foundation.layout.Column
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
    var confirmReinstall by remember { mutableStateOf(false) }

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
                summary = sessions?.let { "${it.size} 个会话" },
                onClick = {
                    scope.launch {
                        sessions = Api.get("/api/context")?.optJSONArray("sessions")?.objects().orEmpty()
                    }
                },
            )
            sessions?.take(8)?.forEach { s ->
                val msgs = s.optJSONArray("messages")?.objects().orEmpty()
                val last = msgs.lastOrNull()?.optStr("content").orEmpty().replace("\n", " ")
                val key = s.optStr("key")
                val title = when {
                    key.startsWith("group") -> "群 " + key.removePrefix("group").substringBefore(":") + " · " + key.substringAfter(":")
                    key.startsWith("private") -> "私聊 " + key.substringAfter(":")
                    else -> key
                }
                InfoRow(title, "${msgs.size} 条")
                if (last.isNotBlank()) Hint(last.take(60))
            }
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
}

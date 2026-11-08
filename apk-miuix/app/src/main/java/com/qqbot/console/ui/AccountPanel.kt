package com.qqbot.console.ui

import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.net.Uri
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
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.rememberUpdatedState
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.qqbot.console.Accounts
import com.qqbot.console.AccountsStore
import com.qqbot.console.AppScope
import com.qqbot.console.Attempt
import com.qqbot.console.Store
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import top.yukonga.miuix.kmp.basic.ButtonDefaults
import top.yukonga.miuix.kmp.basic.InfiniteProgressIndicator
import top.yukonga.miuix.kmp.basic.Text
import top.yukonga.miuix.kmp.basic.TextButton
import top.yukonga.miuix.kmp.basic.TextField
import top.yukonga.miuix.kmp.overlay.OverlayDialog
import top.yukonga.miuix.kmp.theme.MiuixTheme

/**
 * 账号面板：未登录时走 begin → 每 1.2 秒 poll 的登录流程；
 * 已登录显示账号、模型和退出登录。
 */
@Composable
fun AccountDialog(providerId: String, onDismiss: () -> Unit) {
    val ctx = LocalContext.current
    val scope = rememberCoroutineScope()
    val account = AccountsStore.find(providerId)

    var attempt by remember { mutableStateOf<Attempt?>(null) }
    var starting by remember { mutableStateOf(false) }
    var actionError by remember { mutableStateOf("") }
    var input by remember { mutableStateOf("") }
    var confirmSignout by remember { mutableStateOf(false) }
    var signingOut by remember { mutableStateOf(false) }

    fun startLogin() {
        if (starting) return
        starting = true
        actionError = ""
        input = ""
        attempt = null
        scope.launch {
            val r = Accounts.begin(providerId)
            starting = false
            if (r.attempt != null) {
                attempt = r.attempt
            } else {
                actionError = r.error ?: "开始登录失败"
            }
        }
    }

    fun cancelLogin(a: Attempt) {
        attempt = a.copy(phase = "cancelled", message = "已取消登录")
        scope.launch { Accounts.cancel(a.id) }
    }

    fun submitAnswer(a: Attempt) {
        val value = input.trim()
        if (value.isBlank()) {
            actionError = "先粘贴或输入内容再提交"
            return
        }
        actionError = ""
        scope.launch {
            val err = Accounts.answer(a.id, value)
            if (err != null) actionError = err else input = ""
        }
    }

    fun signOut() {
        if (signingOut) return
        signingOut = true
        actionError = ""
        scope.launch {
            val err = Accounts.signout(providerId)
            signingOut = false
            if (err == null) {
                AccountsStore.refresh()
                Store.toast = "${account.name} 已退出登录"
                onDismiss()
            } else {
                actionError = err
                confirmSignout = false
            }
        }
    }

    // 关闭面板时取消还在进行的登录，免得服务端一直挂着
    val latestAttempt by rememberUpdatedState(attempt)
    DisposableEffect(Unit) {
        onDispose {
            latestAttempt?.takeIf { it.active }?.let { a -> AppScope.launch { Accounts.cancel(a.id) } }
        }
    }

    // 轮询登录进度：1.2 秒一次，直到 done / failed / cancelled
    LaunchedEffect(attempt?.id) {
        val id = attempt?.id ?: return@LaunchedEffect
        var misses = 0
        while (true) {
            delay(1200)
            val v = Accounts.poll(id)
            if (v == null) {
                misses++
                if (misses >= 5) {
                    attempt = attempt?.copy(phase = "failed", error = "连接管理服务失败，请稍后再试")
                    break
                }
                continue
            }
            misses = 0
            attempt = v
            if (!v.active) {
                if (v.phase == "done") {
                    AccountsStore.refresh()
                    Store.toast = "已连接 ${v.account.ifBlank { account.name }}"
                }
                break
            }
        }
    }

    OverlayDialog(
        show = true,
        title = "${account.name} 账号",
        onDismissRequest = onDismiss,
    ) {
        Column(
            Modifier.fillMaxWidth().heightIn(max = 430.dp).verticalScroll(rememberScrollState()),
        ) {
            val a = attempt
            when {
                confirmSignout -> {
                    Text("退出后要重新用浏览器登录才能再用这个账号。", fontSize = 14.sp)
                    Spacer(Modifier.height(16.dp))
                    Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                        TextButton(
                            text = "不退出",
                            onClick = { confirmSignout = false },
                            modifier = Modifier.weight(1f),
                        )
                        TextButton(
                            text = if (signingOut) "正在退出…" else "确认退出",
                            onClick = { signOut() },
                            modifier = Modifier.weight(1f),
                            colors = ButtonDefaults.textButtonColorsPrimary(),
                        )
                    }
                }

                a != null -> {
                    AttemptBody(
                        ctx = ctx,
                        a = a,
                        input = input,
                        actionError = actionError,
                        onInput = { input = it },
                        onOpenUrl = { url -> openUrl(ctx, url) },
                        onCopy = { text ->
                            copyText(ctx, text)
                            Store.toast = "已复制"
                        },
                        onSubmit = { submitAnswer(a) },
                        onRetry = { startLogin() },
                        onCancel = { cancelLogin(a) },
                        onFinish = { scope.launch { AccountsStore.refresh() }; onDismiss() },
                        onClose = onDismiss,
                    )
                }

                account.signedIn -> {
                    Text(
                        "已连接${if (account.account.isBlank()) "" else "：" + account.account}",
                        fontSize = 15.sp,
                        fontWeight = FontWeight.Medium,
                    )
                    if (account.models.isNotEmpty()) {
                        Spacer(Modifier.height(8.dp))
                        Text(
                            "可用模型：" + account.models.joinToString("，"),
                            fontSize = 13.sp,
                            color = MiuixTheme.colorScheme.onSurfaceVariantSummary,
                        )
                    }
                    if (actionError.isNotBlank()) {
                        Spacer(Modifier.height(8.dp))
                        Text(actionError, fontSize = 13.sp, color = Bad)
                    }
                    Spacer(Modifier.height(16.dp))
                    TextButton(
                        text = if (signingOut) "正在退出…" else "退出登录",
                        onClick = { confirmSignout = true },
                        modifier = Modifier.fillMaxWidth(),
                    )
                }

                else -> {
                    Text(
                        "点「开始登录」，按提示在浏览器里完成授权。登录后聊天走账号额度，不用填 API Key。",
                        fontSize = 14.sp,
                        color = MiuixTheme.colorScheme.onSurfaceVariantSummary,
                    )
                    if (account.error == "unsupported") {
                        Spacer(Modifier.height(10.dp))
                        Text("这个版本还不支持这种账号，请先升级服务端代码。", fontSize = 13.sp, color = Warn)
                    }
                    if (actionError.isNotBlank()) {
                        Spacer(Modifier.height(10.dp))
                        Text(actionError, fontSize = 13.sp, color = Bad)
                    }
                    Spacer(Modifier.height(16.dp))
                    Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                        TextButton(text = "关闭", onClick = onDismiss, modifier = Modifier.weight(1f))
                        TextButton(
                            text = if (starting) "正在准备…" else "开始登录",
                            onClick = { startLogin() },
                            modifier = Modifier.weight(1f),
                            colors = ButtonDefaults.textButtonColorsPrimary(),
                        )
                    }
                }
            }
        }
    }
}

@Composable
private fun AttemptBody(
    ctx: Context,
    a: Attempt,
    input: String,
    actionError: String,
    onInput: (String) -> Unit,
    onOpenUrl: (String) -> Unit,
    onCopy: (String) -> Unit,
    onSubmit: () -> Unit,
    onRetry: () -> Unit,
    onCancel: () -> Unit,
    onFinish: () -> Unit,
    onClose: () -> Unit,
) {
    val message = a.message.ifBlank { phaseName(a.phase) }
    Text(message, fontSize = 15.sp, fontWeight = FontWeight.Medium)
    Spacer(Modifier.height(10.dp))

    when (a.phase) {
        "open" -> {
            Text(
                "在浏览器里完成登录后，这里会自动继续。",
                fontSize = 13.sp,
                color = MiuixTheme.colorScheme.onSurfaceVariantSummary,
            )
            if (a.url.isNotBlank()) {
                Spacer(Modifier.height(6.dp))
                Text(
                    a.url,
                    fontSize = 11.sp,
                    color = MiuixTheme.colorScheme.onSurfaceVariantSummary,
                    modifier = Modifier.fillMaxWidth(),
                )
            }
            Spacer(Modifier.height(14.dp))
            TextButton(
                text = "打开浏览器",
                onClick = { if (a.url.isNotBlank()) onOpenUrl(a.url) },
                modifier = Modifier.fillMaxWidth(),
                colors = ButtonDefaults.textButtonColorsPrimary(),
            )
            Spacer(Modifier.height(6.dp))
            TextButton(text = "取消登录", onClick = onCancel, modifier = Modifier.fillMaxWidth())
        }

        "device" -> {
            Text(
                "在打开的网页里输入下面的一次性代码：",
                fontSize = 13.sp,
                color = MiuixTheme.colorScheme.onSurfaceVariantSummary,
            )
            Spacer(Modifier.height(8.dp))
            Text(
                a.userCode.ifBlank { "—" },
                fontSize = 26.sp,
                fontWeight = FontWeight.Bold,
                fontFamily = FontFamily.Monospace,
            )
            Spacer(Modifier.height(12.dp))
            Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                TextButton(
                    text = "复制代码",
                    onClick = { if (a.userCode.isNotBlank()) onCopy(a.userCode) },
                    modifier = Modifier.weight(1f),
                )
                TextButton(
                    text = "打开浏览器",
                    onClick = { if (a.url.isNotBlank()) onOpenUrl(a.url) },
                    modifier = Modifier.weight(1f),
                    colors = ButtonDefaults.textButtonColorsPrimary(),
                )
            }
            if (a.url.isNotBlank()) {
                Spacer(Modifier.height(8.dp))
                Text(
                    a.url,
                    fontSize = 11.sp,
                    color = MiuixTheme.colorScheme.onSurfaceVariantSummary,
                )
            }
            Spacer(Modifier.height(10.dp))
            TextButton(text = "取消登录", onClick = onCancel, modifier = Modifier.fillMaxWidth())
        }

        "prompt" -> {
            Text(
                a.placeholder.ifBlank { "把浏览器里的内容粘贴到这里" },
                fontSize = 13.sp,
                color = MiuixTheme.colorScheme.onSurfaceVariantSummary,
            )
            if (a.url.isNotBlank()) {
                Spacer(Modifier.height(8.dp))
                TextButton(
                    text = "打开浏览器",
                    onClick = { onOpenUrl(a.url) },
                    modifier = Modifier.fillMaxWidth(),
                )
            }
            Spacer(Modifier.height(10.dp))
            TextField(
                value = input,
                onValueChange = onInput,
                label = "粘贴完整网址或授权码",
                singleLine = false,
                minLines = 2,
                maxLines = 4,
                modifier = Modifier.fillMaxWidth().padding(bottom = 12.dp),
            )
            if (actionError.isNotBlank()) {
                Text(actionError, fontSize = 13.sp, color = Bad)
                Spacer(Modifier.height(8.dp))
            }
            Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                TextButton(text = "取消登录", onClick = onCancel, modifier = Modifier.weight(1f))
                TextButton(
                    text = "提交",
                    onClick = onSubmit,
                    modifier = Modifier.weight(1f),
                    colors = ButtonDefaults.textButtonColorsPrimary(),
                )
            }
        }

        "done" -> {
            Text(
                "已连接" + if (a.account.isBlank()) "" else "：${a.account}",
                fontSize = 13.sp,
                color = Good,
            )
            Spacer(Modifier.height(14.dp))
            TextButton(
                text = "完成",
                onClick = onFinish,
                modifier = Modifier.fillMaxWidth(),
                colors = ButtonDefaults.textButtonColorsPrimary(),
            )
        }

        "failed" -> {
            Text(
                a.error.ifBlank { "登录没有完成，再试一次" },
                fontSize = 13.sp,
                color = Bad,
            )
            Spacer(Modifier.height(14.dp))
            Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                TextButton(text = "关闭", onClick = onClose, modifier = Modifier.weight(1f))
                TextButton(
                    text = "重试",
                    onClick = onRetry,
                    modifier = Modifier.weight(1f),
                    colors = ButtonDefaults.textButtonColorsPrimary(),
                )
            }
        }

        "cancelled" -> {
            Text("已取消这次登录。", fontSize = 13.sp, color = MiuixTheme.colorScheme.onSurfaceVariantSummary)
            Spacer(Modifier.height(14.dp))
            Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                TextButton(text = "关闭", onClick = onClose, modifier = Modifier.weight(1f))
                TextButton(
                    text = "重新开始",
                    onClick = onRetry,
                    modifier = Modifier.weight(1f),
                    colors = ButtonDefaults.textButtonColorsPrimary(),
                )
            }
        }

        else -> {
            // starting / 未知阶段
            InfiniteProgressIndicator()
            Spacer(Modifier.height(10.dp))
            Text(
                "正在准备登录，请稍候…",
                fontSize = 13.sp,
                color = MiuixTheme.colorScheme.onSurfaceVariantSummary,
            )
            Spacer(Modifier.height(10.dp))
            TextButton(text = "取消登录", onClick = onCancel, modifier = Modifier.fillMaxWidth())
        }
    }
}

private fun phaseName(phase: String): String = when (phase) {
    "starting" -> "正在准备登录…"
    "open" -> "在浏览器里完成登录"
    "device" -> "在网页里输入一次性代码"
    "prompt" -> "需要你粘贴授权信息"
    "done" -> "登录完成"
    "failed" -> "登录失败"
    "cancelled" -> "已取消"
    else -> "正在登录…"
}

private fun copyText(ctx: Context, text: String) {
    try {
        val cm = ctx.getSystemService(ClipboardManager::class.java) ?: return
        cm.setPrimaryClip(ClipData.newPlainText("登录代码", text))
    } catch (_: Exception) {
    }
}

private fun openUrl(ctx: Context, url: String) {
    try {
        ctx.startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(url)).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
    } catch (e: Exception) {
        Store.toast = "打不开浏览器：${e.message ?: "没有可用的浏览器"}"
    }
}

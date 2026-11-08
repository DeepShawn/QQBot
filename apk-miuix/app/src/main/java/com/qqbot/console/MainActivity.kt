package com.qqbot.console

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Bundle
import android.os.PowerManager
import android.provider.Settings
import androidx.activity.ComponentActivity
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.systemBarsPadding
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.qqbot.console.ui.AiPage
import com.qqbot.console.ui.GroupPage
import com.qqbot.console.ui.HomePage
import com.qqbot.console.ui.MorePage
import com.qqbot.console.ui.RecordsPage
import com.qqbot.console.ui.UpdatePromptHost
import kotlinx.coroutines.delay
import top.yukonga.miuix.kmp.basic.ButtonDefaults
import top.yukonga.miuix.kmp.basic.Card
import top.yukonga.miuix.kmp.basic.InfiniteProgressIndicator
import top.yukonga.miuix.kmp.basic.LinearProgressIndicator
import top.yukonga.miuix.kmp.basic.MiuixScrollBehavior
import top.yukonga.miuix.kmp.basic.NavigationBar
import top.yukonga.miuix.kmp.basic.NavigationBarItem
import top.yukonga.miuix.kmp.basic.Scaffold
import top.yukonga.miuix.kmp.basic.Text
import top.yukonga.miuix.kmp.basic.TextButton
import top.yukonga.miuix.kmp.basic.TopAppBar
import top.yukonga.miuix.kmp.icon.MiuixIcons
import top.yukonga.miuix.kmp.icon.extended.Community
import top.yukonga.miuix.kmp.icon.extended.File
import top.yukonga.miuix.kmp.icon.extended.Home
import top.yukonga.miuix.kmp.icon.extended.Messages
import top.yukonga.miuix.kmp.icon.extended.More
import top.yukonga.miuix.kmp.theme.ColorSchemeMode
import top.yukonga.miuix.kmp.theme.MiuixTheme
import top.yukonga.miuix.kmp.theme.ThemeController

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        installCrashLogger()
        enableEdgeToEdge()
        super.onCreate(savedInstanceState)
        setContent {
            val controller = remember { ThemeController(ColorSchemeMode.System) }
            // MIUIX 的弹窗 / 下拉用 navigationevent 处理返回手势，必须能拿到 dispatcher，否则一弹就闪退
            androidx.compose.runtime.CompositionLocalProvider(
                androidx.navigationevent.compose.LocalNavigationEventDispatcherOwner provides this@MainActivity,
            ) {
                MiuixTheme(controller = controller) { Root() }
            }
        }
    }
}

@Composable
private fun Root() {
    val ctx = LocalContext.current
    LaunchedEffect(Unit) { Installer.boot(ctx) }
    when (Installer.phase) {
        Phase.READY -> MainScreen()
        else -> SetupScreen()
    }
}

// ============================================================ 首次安装 / 启动 / 出错

@Composable
private fun SetupScreen() {
    val ctx = LocalContext.current
    val pkg = Env.DEFAULT_PACKAGE

    val askStorage = rememberLauncherForActivityResult(ActivityResultContracts.RequestMultiplePermissions()) { r ->
        if (r.values.any { it }) Installer.installFromFile(ctx, pkg)
        else Store.toast = "需要存储权限才能读取安装包"
    }
    val picker = rememberLauncherForActivityResult(ActivityResultContracts.OpenDocument()) { uri ->
        if (uri != null) Installer.installFromUri(ctx, uri)
    }

    Scaffold(topBar = { TopAppBar(title = "机器人控制台") }) { padding ->
        Column(
            Modifier.fillMaxSize().padding(padding).padding(horizontal = 16.dp),
            verticalArrangement = Arrangement.Center,
            horizontalAlignment = Alignment.CenterHorizontally,
        ) {
            Card(Modifier.fillMaxWidth()) {
                Column(Modifier.padding(24.dp), horizontalAlignment = Alignment.CenterHorizontally) {
                    when (Installer.phase) {
                        Phase.CHECKING -> {
                            InfiniteProgressIndicator()
                            Spacer(Modifier.height(16.dp))
                            Text("检查运行环境…")
                        }

                        Phase.NEED_INSTALL -> {
                            Text("安装运行环境", fontSize = 22.sp, fontWeight = FontWeight.Bold)
                            Spacer(Modifier.height(10.dp))
                            Text(
                                "QQ、NapCat 和机器人都会装进这个 App，之后不再依赖其它 App。只需安装一次。",
                                textAlign = TextAlign.Center,
                                color = MiuixTheme.colorScheme.onSurfaceVariantSummary,
                            )
                            Spacer(Modifier.height(20.dp))
                            if (pkg.isFile) {
                                Text(
                                    "已找到安装包（${pkg.length() shr 20} MB）",
                                    color = MiuixTheme.colorScheme.onSurfaceVariantSummary,
                                    fontSize = 13.sp,
                                )
                                Spacer(Modifier.height(12.dp))
                                TextButton(
                                    text = "开始安装",
                                    modifier = Modifier.fillMaxWidth(),
                                    colors = ButtonDefaults.textButtonColorsPrimary(),
                                    onClick = {
                                        val granted = ctx.checkSelfPermission(Manifest.permission.READ_EXTERNAL_STORAGE) ==
                                            PackageManager.PERMISSION_GRANTED
                                        if (granted) Installer.installFromFile(ctx, pkg)
                                        else askStorage.launch(arrayOf(
                                            Manifest.permission.READ_EXTERNAL_STORAGE,
                                            Manifest.permission.WRITE_EXTERNAL_STORAGE,
                                        ))
                                    },
                                )
                                Spacer(Modifier.height(8.dp))
                            } else {
                                Text(
                                    "没找到安装包：\n${pkg.absolutePath}",
                                    textAlign = TextAlign.Center,
                                    fontSize = 13.sp,
                                    color = MiuixTheme.colorScheme.onSurfaceVariantSummary,
                                )
                                Spacer(Modifier.height(12.dp))
                            }
                            TextButton(
                                text = "手动选择安装包",
                                modifier = Modifier.fillMaxWidth(),
                                onClick = { picker.launch(arrayOf("*/*")) },
                            )
                        }

                        Phase.INSTALLING, Phase.STARTING -> {
                            Text(
                                if (Installer.phase == Phase.INSTALLING) "正在安装" else "正在启动",
                                fontSize = 22.sp,
                                fontWeight = FontWeight.Bold,
                            )
                            Spacer(Modifier.height(20.dp))
                            LinearProgressIndicator(progress = Installer.progress, modifier = Modifier.fillMaxWidth())
                            Spacer(Modifier.height(12.dp))
                            Text(
                                Installer.detail,
                                textAlign = TextAlign.Center,
                                fontSize = 13.sp,
                                color = MiuixTheme.colorScheme.onSurfaceVariantSummary,
                            )
                            Spacer(Modifier.height(8.dp))
                            Text("可以切到后台，不要彻底划掉 App", fontSize = 12.sp, color = MiuixTheme.colorScheme.onSurfaceVariantSummary)
                        }

                        Phase.ERROR -> {
                            Text("出了点问题", fontSize = 22.sp, fontWeight = FontWeight.Bold)
                            Spacer(Modifier.height(12.dp))
                            Text(Installer.error, fontSize = 13.sp, color = MiuixTheme.colorScheme.onSurfaceVariantSummary)
                            Spacer(Modifier.height(20.dp))
                            TextButton(
                                text = "重试",
                                modifier = Modifier.fillMaxWidth(),
                                colors = ButtonDefaults.textButtonColorsPrimary(),
                                onClick = { Installer.boot(ctx) },
                            )
                            Spacer(Modifier.height(8.dp))
                            TextButton(
                                text = "重新安装运行环境",
                                modifier = Modifier.fillMaxWidth(),
                                onClick = { Installer.reinstall() },
                            )
                        }

                        Phase.READY -> Unit
                    }
                }
            }
        }
    }
}

// ============================================================ 主界面

private data class Tab(val label: String, val title: String, val icon: androidx.compose.ui.graphics.vector.ImageVector)

private val TABS = listOf(
    Tab("概览", "机器人控制台", MiuixIcons.Home),
    Tab("AI", "AI 与人格", MiuixIcons.Messages),
    Tab("群管", "群管理", MiuixIcons.Community),
    Tab("记录", "记录", MiuixIcons.File),
    Tab("更多", "更多", MiuixIcons.More),
)

@Composable
private fun MainScreen() {
    val ctx = LocalContext.current
    var tab by remember { mutableIntStateOf(0) }
    val scrollBehavior = MiuixScrollBehavior()

    // 每 3 秒刷新一次状态；连不上时提示
    LaunchedEffect(Unit) {
        while (true) {
            Store.refresh()
            delay(3000)
        }
    }
    // 第一次进入主界面时，请求忽略电池优化，防止后台被杀
    LaunchedEffect(Unit) { requestIgnoreBattery(ctx) }
    // 每天最多检查一次 App 更新（开关在更多页）
    LaunchedEffect(Unit) { AppUpdates.autoCheckIfDue(ctx) }

    Scaffold(
        topBar = { TopAppBar(title = TABS[tab].title, scrollBehavior = scrollBehavior) },
        bottomBar = {
            NavigationBar {
                TABS.forEachIndexed { i, t ->
                    NavigationBarItem(selected = tab == i, onClick = { tab = i }, icon = t.icon, label = t.label)
                }
            }
        },
    ) { padding ->
        Box(Modifier.fillMaxSize()) {
            if (!Store.online && Store.state == null) {
                Column(
                    Modifier.fillMaxSize().padding(padding),
                    verticalArrangement = Arrangement.Center,
                    horizontalAlignment = Alignment.CenterHorizontally,
                ) {
                    InfiniteProgressIndicator()
                    Spacer(Modifier.height(12.dp))
                    Text("连接管理服务…", color = MiuixTheme.colorScheme.onSurfaceVariantSummary)
                }
            } else {
                when (tab) {
                    0 -> HomePage(padding, scrollBehavior)
                    1 -> AiPage(padding, scrollBehavior)
                    2 -> GroupPage(padding, scrollBehavior)
                    3 -> RecordsPage(padding, scrollBehavior)
                    else -> MorePage(padding, scrollBehavior)
                }
            }
            UpdatePromptHost()
            Toast(Modifier.align(Alignment.BottomCenter).padding(padding).padding(bottom = 12.dp))
        }
    }
}

@Composable
private fun Toast(modifier: Modifier) {
    val msg = Store.toast ?: return
    LaunchedEffect(msg) {
        delay(2200)
        if (Store.toast == msg) Store.toast = null
    }
    Card(modifier = modifier.padding(horizontal = 24.dp)) {
        Text(msg, modifier = Modifier.padding(horizontal = 20.dp, vertical = 12.dp), fontSize = 14.sp)
    }
}

/** 闪退时把完整堆栈写到 Download/DSH-Go/qqbot-crash.log，方便排查。 */
private fun installCrashLogger() {
    val prev = Thread.getDefaultUncaughtExceptionHandler()
    Thread.setDefaultUncaughtExceptionHandler { t, e ->
        try {
            val sw = java.io.StringWriter()
            e.printStackTrace(java.io.PrintWriter(sw))
            java.io.File("/sdcard/Download/DSH-Go/qqbot-crash.log").appendText(
                "==== ${java.util.Date()} thread=${t.name}\n$sw\n"
            )
        } catch (_: Throwable) {}
        prev?.uncaughtException(t, e)
    }
}

private fun requestIgnoreBattery(ctx: android.content.Context) {
    try {
        val pm = ctx.getSystemService(PowerManager::class.java)
        if (!pm.isIgnoringBatteryOptimizations(ctx.packageName)) {
            ctx.startActivity(
                Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS, Uri.parse("package:${ctx.packageName}"))
            )
        }
    } catch (_: Exception) {}
}

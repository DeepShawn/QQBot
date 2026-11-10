package com.qqbot.console

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Bundle
import android.os.PowerManager
import android.provider.Settings
import androidx.activity.ComponentActivity
import androidx.activity.SystemBarStyle
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.animation.core.AnimationSpec
import androidx.compose.animation.core.spring
import androidx.compose.animation.core.tween
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.layout.calculateEndPadding
import androidx.compose.foundation.layout.calculateStartPadding
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.foundation.layout.systemBarsPadding
import androidx.compose.foundation.pager.HorizontalPager
import androidx.compose.foundation.pager.PagerDefaults
import androidx.compose.foundation.pager.PagerSnapDistance
import androidx.compose.foundation.pager.rememberPagerState
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.SideEffect
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.drawBehind
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalLayoutDirection
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.core.view.WindowCompat
import com.qqbot.console.ui.AiPage
import com.qqbot.console.ui.GlassBackdrop
import com.qqbot.console.ui.GlassButton
import com.qqbot.console.ui.GlassCard
import com.qqbot.console.ui.GlassTabBar
import com.qqbot.console.ui.GlassTabItem
import com.qqbot.console.ui.GlassText
import com.qqbot.console.ui.GlassTokens
import com.qqbot.console.ui.LocalAccent
import com.qqbot.console.ui.accentById
import com.qqbot.console.ui.GroupPage
import com.qqbot.console.ui.glass
import com.qqbot.console.ui.HomePage
import com.qqbot.console.ui.MorePage
import com.qqbot.console.ui.RecordsPage
import com.qqbot.console.ui.UpdatePromptHost
import kotlin.math.abs
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import top.yukonga.miuix.kmp.basic.Card
import top.yukonga.miuix.kmp.basic.InfiniteProgressIndicator
import top.yukonga.miuix.kmp.basic.LinearProgressIndicator
import top.yukonga.miuix.kmp.basic.MiuixScrollBehavior
import top.yukonga.miuix.kmp.basic.Scaffold
import top.yukonga.miuix.kmp.basic.Text
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
import top.yukonga.miuix.kmp.squircle.LocalSquircleEnabled

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        installCrashLogger()
        // 显式 edge-to-edge：状态栏 / 导航栏都透明，让 GlassBackdrop 铺到状态栏后面
        enableEdgeToEdge(
            statusBarStyle = SystemBarStyle.dark(android.graphics.Color.TRANSPARENT),
            navigationBarStyle = SystemBarStyle.dark(android.graphics.Color.TRANSPARENT),
        )
        super.onCreate(savedInstanceState)
        // 部分 ROM（MIUI 等）光靠 enableEdgeToEdge 仍会留一条黑底，这里再兜底一次
        try {
            window.statusBarColor = android.graphics.Color.TRANSPARENT
            window.navigationBarColor = android.graphics.Color.TRANSPARENT
            WindowCompat.setDecorFitsSystemWindows(window, false)
        } catch (_: Exception) {
        }
        setContent {
            // 强调色 + 外观模式（dark / light / system）都来自 config `ui.*`，切换即时生效
            val accent = accentById(Store.str("config", "ui", "accent"))
            val themeMode = Store.str("config", "ui", "theme").ifBlank { "dark" }
            val systemDark = isSystemInDarkTheme()
            val dark = when (themeMode) {
                "light" -> false
                "system" -> systemDark
                else -> true   // dark（默认）
            }
            // 低功耗模式（ui.low_power）：弱化动画/高光/阴影/按压反馈
            val lowPower = Store.bool("config", "ui", "low_power", def = false)
            // 玻璃 tokens 是全局单例（getter 读取 dark / lowPower），这里同步主题并让状态栏图标颜色跟随
            SideEffect {
                GlassTokens.dark = dark
                GlassTokens.lowPower = lowPower
            }
            SideEffect {
                try {
                    val insets = WindowCompat.getInsetsController(window, window.decorView)
                    insets.isAppearanceLightStatusBars = !dark
                    insets.isAppearanceLightNavigationBars = !dark
                } catch (_: Exception) {
                }
            }
            val controller = remember(accent.id, dark) {
                ThemeController(
                    if (dark) ColorSchemeMode.Dark else ColorSchemeMode.Light,
                    keyColor = accent.primary,
                )
            }
            // MIUIX 的弹窗 / 下拉用 navigationevent 处理返回手势，必须能拿到 dispatcher，否则一弹就闪退；
            // 关掉 squircle，避免 MIUIX 的圆角与玻璃渐变描边在角部错位
            androidx.compose.runtime.CompositionLocalProvider(
                androidx.navigationevent.compose.LocalNavigationEventDispatcherOwner provides this@MainActivity,
                LocalSquircleEnabled provides false,
                LocalAccent provides accent,
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
    Box(Modifier.fillMaxSize()) {
        GlassBackdrop()
        when (Installer.phase) {
            Phase.READY -> MainScreen()
            else -> SetupScreen()
        }
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

    Scaffold(
        containerColor = Color.Transparent,
        topBar = {
            TopAppBar(
                title = "机器人控制台",
                color = Color.Transparent,
                titleColor = GlassText.primary,
                largeTitleColor = GlassText.primary,
            )
        },
    ) { padding ->
        Column(
            Modifier.fillMaxSize().padding(padding).padding(horizontal = 16.dp),
            verticalArrangement = Arrangement.Center,
            horizontalAlignment = Alignment.CenterHorizontally,
        ) {
            GlassCard(Modifier.fillMaxWidth()) {
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
                                GlassButton(text = "开始安装", onClick = {
                                        val granted = ctx.checkSelfPermission(Manifest.permission.READ_EXTERNAL_STORAGE) ==
                                            PackageManager.PERMISSION_GRANTED
                                        if (granted) Installer.installFromFile(ctx, pkg)
                                        else askStorage.launch(arrayOf(
                                            Manifest.permission.READ_EXTERNAL_STORAGE,
                                            Manifest.permission.WRITE_EXTERNAL_STORAGE,
                                        ))
                                    }, modifier = Modifier.fillMaxWidth(), primary = true)
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
                            GlassButton(text = "手动选择安装包", onClick = { picker.launch(arrayOf("*/*")) }, modifier = Modifier.fillMaxWidth(), primary = false)
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
                            GlassButton(text = "重试", onClick = { Installer.boot(ctx) }, modifier = Modifier.fillMaxWidth(), primary = true)
                            Spacer(Modifier.height(8.dp))
                            GlassButton(text = "重新安装运行环境", onClick = { Installer.reinstall() }, modifier = Modifier.fillMaxWidth(), primary = false)
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
    val scope = rememberCoroutineScope()
    val scrollBehavior = MiuixScrollBehavior()
    val pagerState = rememberPagerState(initialPage = 0, pageCount = { TABS.size })
    val tab = pagerState.currentPage
    // 低功耗模式：关闭页间变换、缩短吸附动画、减少预组合页
    val lowPower = GlassTokens.lowPower
    val snapSpec: AnimationSpec<Float> =
        if (lowPower) tween(140) else spring(dampingRatio = 0.86f, stiffness = 420f)

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
        containerColor = Color.Transparent,
        // 顶栏已删除：状态栏留白由内容区 statusBarsPadding 处理，Scaffold 不再重复给 inset
        contentWindowInsets = WindowInsets(0, 0, 0, 0),
        bottomBar = {
            GlassTabBar(
                items = TABS.map { GlassTabItem(it.label, it.icon) },
                selectedIndex = tab,
                // 连续页偏移：currentPage + 当前页的 offsetFraction（滑动时逐帧变化）
                pageOffset = { pagerState.currentPage + pagerState.currentPageOffsetFraction },
                onSelect = { i ->
                    scope.launch {
                        // 点击 / 底栏滑动松手：弹簧吸附滚到目标页（TabBar 胶囊会连续跟随）
                        pagerState.animateScrollToPage(i, animationSpec = snapSpec)
                    }
                },
                // 底栏拖动：直接请求 pager 滚到「页 + 分数」，胶囊随 pageOffset 连续移动
                onDragToPage = { page, fraction -> pagerState.requestScrollToPage(page, fraction) },
                modifier = Modifier.navigationBarsPadding().padding(horizontal = 16.dp, vertical = 10.dp),
            )
        },
    ) { padding ->
        // 浮动 Tab 栏会盖住滚动内容：除 Scaffold 给的底栏高度外再多留 16dp，滚到底最后一张卡完整可见
        val pagePadding = PaddingValues(
            start = padding.calculateStartPadding(LocalLayoutDirection.current),
            top = padding.calculateTopPadding(),
            end = padding.calculateEndPadding(LocalLayoutDirection.current),
            bottom = padding.calculateBottomPadding() + 16.dp,
        )
        Box(Modifier.fillMaxSize()) {
            if (!Store.online && Store.state == null) {
                Column(
                    Modifier.fillMaxSize().statusBarsPadding().padding(pagePadding),
                    verticalArrangement = Arrangement.Center,
                    horizontalAlignment = Alignment.CenterHorizontally,
                ) {
                    InfiniteProgressIndicator()
                    Spacer(Modifier.height(12.dp))
                    Text("连接管理服务…", color = MiuixTheme.colorScheme.onSurfaceVariantSummary)
                }
            } else {
                HorizontalPager(
                    state = pagerState,
                    modifier = Modifier.fillMaxSize(),
                    // 预组合相邻页，滑动时不留白；低功耗模式不预组合，省内存
                    beyondViewportPageCount = if (lowPower) 0 else 1,
                    // 松手弹簧吸附；最多一次滑 3 页
                    flingBehavior = PagerDefaults.flingBehavior(
                        state = pagerState,
                        pagerSnapDistance = PagerSnapDistance.atMost(3),
                        snapAnimationSpec = snapSpec,
                    ),
                ) { page ->
                    val offsetFraction = pagerState.currentPageOffsetFraction
                    Column(
                        Modifier
                            .fillMaxSize()
                            .statusBarsPadding()
                            // 相邻页：视差 + 缩放 0.96 + 透明度 0.6；只在 graphicsLayer 里读偏移，不逐帧重组
                            .graphicsLayer {
                                if (GlassTokens.lowPower) {
                                    scaleX = 1f; scaleY = 1f; alpha = 1f; translationX = 0f
                                } else {
                                    val off = ((pagerState.currentPage - page) + offsetFraction).coerceIn(-1f, 1f)
                                    val a = abs(off)
                                    scaleX = 1f - 0.04f * a
                                    scaleY = 1f - 0.04f * a
                                    alpha = 1f - 0.40f * a
                                    translationX = off * 36.dp.toPx()
                                }
                            },
                    ) {
                        // 页面大标题：带一点反向视差，滑动时比内容慢
                        Text(
                            TABS[page].title,
                            fontSize = 30.sp,
                            fontWeight = FontWeight.Bold,
                            color = GlassText.primary,
                            modifier = Modifier
                                .padding(start = 28.dp, top = 8.dp, bottom = 6.dp)
                                .graphicsLayer {
                                    if (GlassTokens.lowPower) {
                                        translationX = 0f
                                    } else {
                                        val off = ((pagerState.currentPage - page) + offsetFraction).coerceIn(-1f, 1f)
                                        translationX = off * 18.dp.toPx()
                                    }
                                },
                        )
                        // 垂直列表在 pager 内层滚动：横向手势归 pager，纵向归 LazyColumn
                        Box(Modifier.fillMaxWidth().weight(1f)) {
                            when (page) {
                                0 -> HomePage(pagePadding, scrollBehavior)
                                1 -> AiPage(pagePadding, scrollBehavior)
                                2 -> GroupPage(pagePadding, scrollBehavior)
                                3 -> RecordsPage(pagePadding, scrollBehavior)
                                else -> MorePage(pagePadding, scrollBehavior)
                            }
                        }
                    }
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
    Box(
        modifier.padding(horizontal = 24.dp),
        contentAlignment = Alignment.Center,
    ) {
        Box(
            Modifier
                .glass(
                    level = com.qqbot.console.ui.GlassLevel.Thick,
                    shape = androidx.compose.foundation.shape.RoundedCornerShape(22.dp),
                ),
        ) {
            Text(
                msg,
                modifier = Modifier.padding(horizontal = 20.dp, vertical = 12.dp),
                fontSize = 14.sp,
                color = GlassText.primary,
            )
        }
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

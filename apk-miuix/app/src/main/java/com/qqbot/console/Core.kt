package com.qqbot.console

import android.content.Context
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.io.FileInputStream
import java.util.concurrent.TimeUnit

/** 进程级协程作用域：安装 / 启动不会因为界面销毁而被取消。 */
object AppScope : CoroutineScope by CoroutineScope(SupervisorJob() + Dispatchers.IO)

// ============================================================ 安装 / 启动状态机

enum class Phase { CHECKING, NEED_INSTALL, INSTALLING, STARTING, READY, ERROR }

object Installer {
    var phase by mutableStateOf(Phase.CHECKING); private set
    var progress by mutableStateOf(0f); private set
    var detail by mutableStateOf(""); private set
    var error by mutableStateOf(""); private set

    private var busy = false

    /** App 打开时调用：已在运行 → 直接进主界面；已安装 → 拉起服务；否则引导安装。 */
    fun boot(ctx: Context) {
        if (busy) return
        AppScope.launch {
            when {
                Env.serviceAlive() -> phase = Phase.READY
                Env.validate(ctx) -> startService(ctx)
                else -> phase = Phase.NEED_INSTALL
            }
        }
    }

    fun installFromFile(ctx: Context, file: File) = runInstall(ctx, file.length()) { FileInputStream(file) }

    fun installFromUri(ctx: Context, uri: android.net.Uri) {
        val size = try {
            ctx.contentResolver.openFileDescriptor(uri, "r")?.use { it.statSize } ?: -1L
        } catch (e: Exception) { -1L }
        runInstall(ctx, size) {
            ctx.contentResolver.openInputStream(uri) ?: throw IllegalStateException("无法读取所选文件")
        }
    }

    private fun runInstall(ctx: Context, total: Long, open: () -> java.io.InputStream) {
        if (busy) return
        busy = true
        phase = Phase.INSTALLING
        progress = 0f
        detail = "准备中…"
        AppScope.launch {
            try {
                Env.prepareProot(ctx)
                Env.install(ctx, open, if (total > 0) total else 900L shl 20) { read, all, files ->
                    progress = (read.toFloat() / all).coerceIn(0f, 1f)
                    detail = "已解压 $files 个文件 · ${read shr 20} / ${all shr 20} MB"
                }
                busy = false
                startService(ctx)
            } catch (e: Throwable) {
                busy = false
                fail("安装失败：${e.message ?: e.javaClass.simpleName}")
            }
        }
    }

    fun startService(ctx: Context) {
        if (busy) return
        busy = true
        phase = Phase.STARTING
        progress = 0f
        detail = "正在启动 QQ 和机器人…"
        AppScope.launch {
            try {
                // 别的 App（比如 DSH Go）占着端口时会互相冲突，直接说清楚
                if (!ProotService.isRunning() && Env.serviceAlive()) {
                    busy = false
                    phase = Phase.READY
                    return@launch
                }
                withContext(Dispatchers.Main) { ProotService.start(ctx) }
                val limit = 240
                for (i in 1..limit) {
                    delay(1000)
                    if (Env.serviceAlive()) {
                        busy = false
                        phase = Phase.READY
                        return@launch
                    }
                    progress = i.toFloat() / limit
                    detail = "正在启动 QQ 和机器人… ${i}s\n" + ProotService.lastLog.take(120)
                    if (i > 20 && !ProotService.isRunning()) break   // 进程已经退出，不必再等
                }
                busy = false
                fail("服务没有起来。\n最后的输出：${ProotService.lastLog}\n完整日志：${Env.PUBLIC_LOG.absolutePath}")
            } catch (e: Throwable) {
                busy = false
                fail("启动失败：${e.message}")
            }
        }
    }

    fun reinstall() {
        phase = Phase.NEED_INSTALL
    }

    private fun fail(msg: String) {
        error = msg
        phase = Phase.ERROR
        Log.write("[app] $msg")
    }
}

// ============================================================ 管理 API（127.0.0.1:8080）

object Api {
    private const val BASE = "http://127.0.0.1:8080"
    private val JSON = "application/json; charset=utf-8".toMediaType()
    private val client = OkHttpClient.Builder()
        .connectTimeout(3, TimeUnit.SECONDS)
        .readTimeout(10, TimeUnit.MINUTES)      // 「开启服务」可能要等协议层好几分钟
        .build()

    private fun call(req: Request): JSONObject? = try {
        client.newCall(req).execute().use { r ->
            val body = r.body?.string().orEmpty()
            if (body.isBlank()) null else JSONObject(body)
        }
    } catch (e: Exception) { null }

    suspend fun get(path: String): JSONObject? = withContext(Dispatchers.IO) {
        call(Request.Builder().url(BASE + path).get().build())
    }

    suspend fun post(path: String, body: JSONObject = JSONObject()): JSONObject? = withContext(Dispatchers.IO) {
        call(Request.Builder().url(BASE + path).post(body.toString().toRequestBody(JSON)).build())
    }
}

// ============================================================ 全局状态（/api/state）

object Store {
    var state by mutableStateOf<JSONObject?>(null); private set
    var online by mutableStateOf(true); private set
    var toast by mutableStateOf<String?>(null)

    suspend fun refresh() {
        val s = Api.get("/api/state")
        online = s != null
        if (s != null) state = s
    }

    /** 提交一个嵌套 patch，例如 patchOf("plugins","ai_chat","search","enabled", value = true) */
    suspend fun patch(patch: JSONObject, okText: String? = "已保存"): Boolean {
        val r = Api.post("/api/config", patch)
        val ok = r?.optBoolean("ok") == true
        if (ok) {
            r!!.optJSONObject("config")?.let { cfg ->
                val s = JSONObject(state?.toString() ?: "{}")
                s.put("config", cfg)
                state = s
            }
            if (okText != null) toast = okText
        } else {
            toast = "保存失败：" + (r?.optString("error") ?: "管理服务没有响应")
        }
        return ok
    }

    suspend fun action(name: String, okText: String): Boolean {
        toast = "执行中…"
        val r = Api.post("/api/bot/action", JSONObject().put("action", name))
        val ok = r?.optBoolean("ok") == true
        r?.optJSONObject("state")?.let { state = it }
        toast = if (ok) okText else "操作失败：" + (r?.optString("output")?.takeLast(80) ?: "没有响应")
        return ok
    }

    // ---------- 读取辅助 ----------
    fun obj(vararg path: String): JSONObject? {
        var cur: JSONObject? = state
        for (k in path) cur = cur?.optJSONObject(k) ?: return null
        return cur
    }

    fun cfg(vararg path: String): JSONObject? = obj("config", *path)

    fun bool(vararg path: String, def: Boolean = false): Boolean {
        val parent = obj(*path.dropLast(1).toTypedArray()) ?: return def
        return parent.optBoolean(path.last(), def)
    }

    fun str(vararg path: String, def: String = ""): String {
        val parent = obj(*path.dropLast(1).toTypedArray()) ?: return def
        return if (parent.isNull(path.last())) def else parent.optString(path.last(), def)
    }

    fun int(vararg path: String, def: Int = 0): Int {
        val parent = obj(*path.dropLast(1).toTypedArray()) ?: return def
        return parent.optInt(path.last(), def)
    }
}

/** patchOf("plugins","ai_chat","search","enabled", value = true) → {"plugins":{"ai_chat":{"search":{"enabled":true}}}} */
fun patchOf(vararg path: String, value: Any?): JSONObject {
    var node: Any? = value ?: JSONObject.NULL
    for (k in path.reversed()) node = JSONObject().put(k, node)
    return node as JSONObject
}

fun JSONArray.objects(): List<JSONObject> = (0 until length()).mapNotNull { optJSONObject(it) }

package com.qqbot.console

import android.content.Context
import android.content.Intent
import android.net.Uri
import android.provider.Settings
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.core.content.FileProvider
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.OkHttpClient
import okhttp3.Request
import org.json.JSONObject
import java.io.File
import java.security.MessageDigest
import java.util.concurrent.TimeUnit

// ============================================================ App 自动更新

/** GitHub Release 里的一个版本。 */
data class AppRelease(
    val version: String,        // 3.3（去掉 v）
    val tag: String,            // v3.3
    val notes: String,          // release body（更新说明，可能是 markdown 原文）
    val apkUrl: String,
    val apkName: String,
    val sumsUrl: String,        // SHA256SUMS-*.txt
    val size: Long,
)

/** 一次检查的结果：release 为新版本时非空；error 非空表示检查失败；都为空表示已是最新。 */
data class UpdateCheck(
    val release: AppRelease? = null,
    val error: String? = null,
    val latestVersion: String = "",
)

object AppUpdates {
    const val REPO = "DeepShawn/QQBot"
    private const val LATEST_URL = "https://api.github.com/repos/$REPO/releases/latest"
    private const val UA = "QQBot-Console-Android"

    private const val PREFS = "qqbot_update"
    private const val KEY_AUTO = "auto_check"
    private const val KEY_LAST = "last_check"
    private const val KEY_SKIP = "skipped_version"
    private const val DAY_MS = 24L * 60 * 60 * 1000

    private val client = OkHttpClient.Builder()
        .connectTimeout(15, TimeUnit.SECONDS)
        .readTimeout(120, TimeUnit.SECONDS)
        .build()

    // ---------- 界面状态（Compose 直接读） ----------
    var checking by mutableStateOf(false); private set
    var downloading by mutableStateOf(false); private set
    var progress by mutableStateOf(0f); private set
    var statusText by mutableStateOf(""); private set
    var available by mutableStateOf<AppRelease?>(null); private set
    var prompt by mutableStateOf(false)
    var error by mutableStateOf(""); private set

    val currentVersion: String get() = BuildConfig.VERSION_NAME

    // ---------- 设置（SharedPreferences） ----------

    private fun prefs(ctx: Context) = ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE)

    fun autoCheckEnabled(ctx: Context): Boolean = prefs(ctx).getBoolean(KEY_AUTO, true)

    fun setAutoCheck(ctx: Context, on: Boolean) {
        prefs(ctx).edit().putBoolean(KEY_AUTO, on).apply()
    }

    fun skippedVersion(ctx: Context): String = prefs(ctx).getString(KEY_SKIP, "").orEmpty()

    fun skipVersion(ctx: Context, version: String) {
        prefs(ctx).edit().putString(KEY_SKIP, version).apply()
    }

    /** App 启动时调用：开关开着且距上次检查超过一天才真的去检查。 */
    suspend fun autoCheckIfDue(ctx: Context) {
        if (!autoCheckEnabled(ctx) || checking || downloading) return
        val p = prefs(ctx)
        val now = System.currentTimeMillis()
        if (now - p.getLong(KEY_LAST, 0L) < DAY_MS) return
        p.edit().putLong(KEY_LAST, now).apply()
        checking = true
        val result = check()
        checking = false
        val release = result.release ?: return
        if (release.version == skippedVersion(ctx)) return
        available = release
        error = ""
        prompt = true
    }

    /** 更多页「检查更新」：有新版时弹窗，已是最新 / 失败用 toast 告诉用户。 */
    suspend fun checkAndPrompt() {
        if (checking || downloading) return
        checking = true
        val result = check()
        checking = false
        when {
            result.error != null -> Store.toast = result.error
            result.release != null -> {
                available = result.release
                error = ""
                prompt = true
            }
            else -> Store.toast = "已是最新版本 $currentVersion"
        }
    }

    // ---------- 检查 ----------

    suspend fun check(): UpdateCheck = withContext(Dispatchers.IO) {
        val fetched = fetchLatest()
        val release = fetched.release ?: return@withContext fetched
        if (compareVersions(release.version, currentVersion) > 0) fetched
        else UpdateCheck(latestVersion = release.version)
    }

    private fun fetchLatest(): UpdateCheck = try {
        val req = Request.Builder()
            .url(LATEST_URL)
            .header("Accept", "application/vnd.github+json")
            .header("User-Agent", UA)
            .get()
            .build()
        client.newCall(req).execute().use { r ->
            if (!r.isSuccessful) {
                UpdateCheck(error = "检查更新失败：GitHub 返回 ${r.code}，稍后再试")
            } else {
                val root = JSONObject(r.body?.string().orEmpty())
                val tag = root.optString("tag_name").ifBlank {
                    return UpdateCheck(error = "检查更新失败：发布信息里没有版本号")
                }
                val version = tag.trim().trimStart('v', 'V', ' ')
                val assets = root.optJSONArray("assets")
                var apkUrl = ""
                var apkName = ""
                var sumsUrl = ""
                var size = 0L
                for (i in 0 until (assets?.length() ?: 0)) {
                    val a = assets!!.optJSONObject(i) ?: continue
                    val name = a.optString("name")
                    if (apkUrl.isBlank() && name.startsWith("QQBot-Console-") && name.endsWith(".apk")) {
                        apkUrl = a.optString("browser_download_url")
                        apkName = name
                        size = a.optLong("size")
                    }
                    if (sumsUrl.isBlank() && name.startsWith("SHA256SUMS-") && name.endsWith(".txt")) {
                        sumsUrl = a.optString("browser_download_url")
                    }
                }
                if (apkUrl.isBlank()) {
                    UpdateCheck(error = "检查更新失败：这个版本没有安装包")
                } else if (sumsUrl.isBlank()) {
                    UpdateCheck(error = "检查更新失败：这个版本没有校验文件")
                } else {
                    UpdateCheck(
                        release = AppRelease(
                            version = version,
                            tag = tag,
                            notes = root.optString("body").orEmpty(),
                            apkUrl = apkUrl,
                            apkName = apkName,
                            sumsUrl = sumsUrl,
                            size = size,
                        ),
                    )
                }
            }
        }
    } catch (e: Exception) {
        UpdateCheck(error = "检查更新失败：${friendly(e)}")
    }

    // ---------- 下载 / 校验 / 安装 ----------

    suspend fun downloadAndInstall(ctx: Context) {
        val info = available ?: return
        if (downloading) return
        downloading = true
        progress = 0f
        statusText = "准备下载…"
        error = ""
        withContext(Dispatchers.IO) {
            val dir = ctx.cacheDir
            val target = File(dir, info.apkName.ifBlank { "qqbot-console.apk" })
            val tmp = File(dir, "qqbot-console.apk.part")
            try {
                // 清掉上次留下的安装包
                dir.listFiles()?.forEach { f ->
                    if (f.name.endsWith(".apk") || f.name.endsWith(".apk.part") || f.name.endsWith(".txt.sums")) {
                        f.delete()
                    }
                }
                statusText = "正在读取校验文件…"
                val sums = downloadText(info.sumsUrl)
                val expected = parseSums(sums, info.apkName)
                    ?: throw IllegalStateException("校验文件里没有这个安装包，已取消更新")

                statusText = "正在下载…"
                downloadFile(info.apkUrl, tmp) { done, total ->
                    progress = if (total > 0) (done.toFloat() / total).coerceIn(0f, 1f) else 0f
                    statusText = if (total > 0) {
                        "正在下载… ${done / 1048576} / ${total / 1048576} MB"
                    } else {
                        "正在下载… ${done / 1048576} MB"
                    }
                }

                statusText = "正在校验安装包…"
                val actual = sha256(tmp)
                if (!actual.equals(expected, ignoreCase = true)) {
                    tmp.delete()
                    throw IllegalStateException("安装包校验失败，请稍后再试")
                }
                if (target.exists()) target.delete()
                if (!tmp.renameTo(target)) {
                    tmp.copyTo(target, overwrite = true)
                    tmp.delete()
                }
                statusText = "正在打开安装程序…"
                install(ctx, target)
                downloading = false
                prompt = false
                Store.toast = "安装包已下载，请在系统安装界面完成更新"
            } catch (e: Exception) {
                downloading = false
                error = if (e is IllegalStateException) (e.message ?: "更新失败") else "更新失败：${friendly(e)}"
            }
        }
    }

    private fun downloadText(url: String): String {
        val req = Request.Builder().url(url).header("User-Agent", UA).get().build()
        return client.newCall(req).execute().use { r ->
            if (!r.isSuccessful) throw IllegalStateException("下载校验文件失败：HTTP ${r.code}")
            r.body?.string() ?: throw IllegalStateException("校验文件是空的")
        }
    }

    private fun downloadFile(url: String, dest: File, onProgress: (Long, Long) -> Unit) {
        val req = Request.Builder().url(url).header("User-Agent", UA).get().build()
        client.newCall(req).execute().use { r ->
            if (!r.isSuccessful) throw IllegalStateException("下载安装包失败：HTTP ${r.code}")
            val body = r.body ?: throw IllegalStateException("下载安装包失败：没有响应内容")
            val total = body.contentLength()
            body.byteStream().use { input ->
                dest.outputStream().use { out ->
                    val buf = ByteArray(64 * 1024)
                    var done = 0L
                    while (true) {
                        val n = input.read(buf)
                        if (n <= 0) break
                        out.write(buf, 0, n)
                        done += n
                        onProgress(done, total)
                    }
                    out.flush()
                }
            }
        }
    }

    private fun install(ctx: Context, file: File) {
        if (!ctx.packageManager.canRequestPackageInstalls()) {
            // 没有「安装未知应用」权限：跳系统设置，用户回来后可以再点一次「立即更新」
            try {
                ctx.startActivity(
                    Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES, Uri.parse("package:${ctx.packageName}"))
                        .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK),
                )
            } catch (_: Exception) {
            }
            throw IllegalStateException("请先允许「安装未知应用」，再点一次「立即更新」")
        }
        val uri = FileProvider.getUriForFile(ctx, "${ctx.packageName}.files", file)
        val intent = Intent(Intent.ACTION_VIEW)
            .setDataAndType(uri, "application/vnd.android.package-archive")
            .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_ACTIVITY_NEW_TASK)
        ctx.startActivity(intent)
    }

    // ---------- 小工具 ----------

    private fun friendly(e: Exception): String = when (e) {
        is java.net.UnknownHostException -> "网络连不上 GitHub，检查网络后再试"
        is java.net.SocketTimeoutException -> "连接超时，稍后再试"
        else -> e.message?.take(80) ?: e.javaClass.simpleName
    }

    private fun sha256(file: File): String {
        val md = MessageDigest.getInstance("SHA-256")
        file.inputStream().use { input ->
            val buf = ByteArray(64 * 1024)
            while (true) {
                val n = input.read(buf)
                if (n <= 0) break
                md.update(buf, 0, n)
            }
        }
        return md.digest().joinToString("") { "%02x".format(it) }
    }

    private fun parseSums(text: String, apkName: String): String? {
        val wanted = apkName.trim()
        for (line in text.lineSequence()) {
            val parts = line.trim().split(Regex("\\s+")).filter { it.isNotBlank() }
            if (parts.size >= 2) {
                val name = parts.last().trimStart('*').substringAfterLast('/')
                if (name == wanted) return parts.first()
            }
        }
        return null
    }

    /** 只比较数字段：v3.3 vs 3.3 → 0；3.10 vs 3.9 → 正数。 */
    fun compareVersions(a: String, b: String): Int {
        val x = versionParts(a)
        val y = versionParts(b)
        val n = maxOf(x.size, y.size)
        for (i in 0 until n) {
            val diff = (x.getOrElse(i) { 0 }) - (y.getOrElse(i) { 0 })
            if (diff != 0) return diff
        }
        return 0
    }

    private fun versionParts(v: String): List<Int> =
        v.trim()
            .removePrefix("v").removePrefix("V")
            .split('.', '-', '_', '+', ' ')
            .mapNotNull { seg -> seg.takeWhile(Char::isDigit).takeIf(String::isNotEmpty)?.toIntOrNull() }
}

// ============================================================ 机器人代码更新（管理台接口）

object BotUpdates {
    /** GET /api/update/bot → {current:{commit}, latest:{commit,title,url}, behind} */
    suspend fun check(): JSONObject? = Api.get("/api/update/bot")

    /** POST /api/update/bot → {ok, started}；返回错误信息（空串表示成功）。 */
    suspend fun start(): String {
        val r = Api.post("/api/update/bot") ?: return "管理服务没有响应"
        if (r.optBoolean("ok")) return ""
        return r.optString("error").ifBlank { "更新没能开始" }
    }

    /** GET /api/update/bot/status */
    suspend fun status(): JSONObject? = Api.get("/api/update/bot/status")
}

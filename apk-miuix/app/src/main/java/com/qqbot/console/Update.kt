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
    private const val RELEASES_LATEST = "https://github.com/$REPO/releases/latest"
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

    /** 校验文件专用：短超时（10s/20s），拿不到就跳过校验，不能拖死整个更新。 */
    private val sumsClient = OkHttpClient.Builder()
        .connectTimeout(10, TimeUnit.SECONDS)
        .readTimeout(20, TimeUnit.SECONDS)
        .retryOnConnectionFailure(true)
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

    /** GET GitHub API 拿最新 release；5xx/429/网络错误最多重试 3 次（间隔 1s、2s）。 */
    private fun fetchLatest(): UpdateCheck {
        var lastError = ""
        for (attempt in 1..3) {
            val r = fetchApiOnce()
            r.release?.let { return UpdateCheck(release = it) }
            lastError = r.error.orEmpty()
            if (!r.retryable || attempt == 3) break
            try {
                Thread.sleep(attempt * 1000L)   // 第 1 次失败等 1s，第 2 次等 2s
            } catch (_: InterruptedException) {
                break
            }
        }
        // API 不可用（常见 504 / 限流）时回退：让 OkHttp 跟 releases/latest 的 302，从最终 URL 里取 tag
        val fallback = fetchFallback()
        fallback.release?.let { return UpdateCheck(release = it) }
        return UpdateCheck(error = lastError.ifBlank { fallback.error ?: "检查更新失败：GitHub 暂时不可用，稍后再试" })
    }

    private data class ApiAttempt(val release: AppRelease?, val error: String?, val retryable: Boolean)

    /** 调一次 GitHub API。 */
    private fun fetchApiOnce(): ApiAttempt = try {
        val req = Request.Builder()
            .url(LATEST_URL)
            .header("Accept", "application/vnd.github+json")
            .header("User-Agent", UA)
            .get()
            .build()
        client.newCall(req).execute().use { r ->
            if (!r.isSuccessful) {
                ApiAttempt(
                    release = null,
                    error = "检查更新失败：GitHub 暂时不可用（HTTP ${r.code}），稍后再试",
                    retryable = r.code >= 500 || r.code == 429,
                )
            } else {
                parseRelease(JSONObject(r.body?.string().orEmpty()))
            }
        }
    } catch (e: java.io.IOException) {
        ApiAttempt(release = null, error = "检查更新失败：${friendly(e)}，稍后再试", retryable = true)
    } catch (e: Exception) {
        ApiAttempt(release = null, error = "检查更新失败：${friendly(e)}", retryable = false)
    }

    /** 解析 release JSON。 */
    private fun parseRelease(root: JSONObject): ApiAttempt {
        val tag = root.optString("tag_name").trim()
        if (tag.isBlank()) {
            return ApiAttempt(null, "检查更新失败：发布信息里没有版本号，稍后再试", false)
        }
        val version = tag.trimStart('v', 'V', ' ')
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
        if (apkUrl.isBlank()) return ApiAttempt(null, "检查更新失败：这个版本没有安装包", false)
        // 校验文件是可选的：没有也允许更新，下载时跳过校验即可
        return ApiAttempt(
            release = AppRelease(
                version = version,
                tag = tag,
                notes = root.optString("body").orEmpty(),
                apkUrl = apkUrl,
                apkName = apkName,
                sumsUrl = sumsUrl,
                size = size,
            ),
            error = null,
            retryable = false,
        )
    }

    /** API 全部失败时的兜底：解析 releases/latest 跳转后的 tag，按约定拼资产地址。 */
    private fun fetchFallback(): UpdateCheck = try {
        val req = Request.Builder()
            .url(RELEASES_LATEST)
            .header("User-Agent", UA)
            .get()
            .build()
        client.newCall(req).execute().use { r ->
            if (!r.isSuccessful) {
                UpdateCheck(error = "检查更新失败：GitHub 暂时不可用（HTTP ${r.code}），稍后再试")
            } else {
                val tag = parseTagFromUrl(r.request.url.toString())
                if (tag.isNullOrBlank()) {
                    UpdateCheck(error = "检查更新失败：GitHub 暂时不可用，没找到最新版本号，稍后再试")
                } else {
                    val version = tag.trimStart('v', 'V', ' ')
                    val base = "https://github.com/$REPO/releases/download/$tag"
                    UpdateCheck(
                        release = AppRelease(
                            version = version,
                            tag = tag,
                            notes = "",
                            apkUrl = "$base/QQBot-Console-$version.apk",
                            apkName = "QQBot-Console-$version.apk",
                            sumsUrl = "$base/SHA256SUMS-$version.txt",
                            size = 0L,
                        ),
                    )
                }
            }
        }
    } catch (e: Exception) {
        UpdateCheck(error = "检查更新失败：${friendly(e)}，稍后再试")
    }

    /** 从 https://github.com/DeepShawn/QQBot/releases/tag/v3.5 里取 v3.5。 */
    private fun parseTagFromUrl(url: String): String? {
        val marker = "/releases/tag/"
        val i = url.indexOf(marker)
        if (i < 0) return null
        return url.substring(i + marker.length)
            .substringBefore('?')
            .substringBefore('#')
            .trim('/')
            .takeIf { it.isNotBlank() }
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

                // 1) 校验文件可选：短超时；失败 / 为空 / 404 就跳过校验继续下载 APK
                statusText = "正在读取校验文件…"
                val expected: String? = if (info.sumsUrl.isBlank()) {
                    null
                } else {
                    val sums = fetchSumsOptional(info.sumsUrl)
                    sums?.let { parseSums(it, info.apkName) }
                }
                if (expected == null) {
                    statusText = "校验文件获取失败，跳过校验继续下载…"
                }

                // 2) 下载 APK：主地址失败时，用手动拼接的 release 资产地址再试一次
                statusText = if (expected == null) "跳过校验，正在下载安装包…" else "正在下载… 0%"
                val urls = LinkedHashSet<String>()
                if (info.apkUrl.isNotBlank()) urls += info.apkUrl
                val manualUrl = "https://github.com/$REPO/releases/download/${info.tag}/QQBot-Console-${info.version}.apk"
                if (manualUrl != info.apkUrl) urls += manualUrl

                var lastError: Exception? = null
                var downloaded = false
                for (url in urls) {
                    try {
                        tmp.delete()
                        downloadFile(url, tmp) { done, total ->
                            progress = if (total > 0) (done.toFloat() / total).coerceIn(0f, 1f) else 0f
                            val pct = if (total > 0) " ${(progress * 100).toInt()}%" else ""
                            statusText = if (total > 0) {
                                "正在下载…$pct ${done / 1048576} / ${total / 1048576} MB"
                            } else {
                                "正在下载… ${done / 1048576} MB"
                            }
                        }
                        downloaded = true
                        break
                    } catch (e: Exception) {
                        lastError = e
                    }
                }
                if (!downloaded) {
                    val err = lastError
                    val detail = when (err) {
                        is IllegalStateException -> err.message ?: "网络错误"
                        is Exception -> friendly(err)
                        else -> "网络错误"
                    }
                    throw IllegalStateException("下载失败：$detail")
                }

                // 3) 只有拿到校验值才校验；校验失败才中止
                if (expected != null) {
                    statusText = "正在校验安装包…"
                    val actual = sha256(tmp)
                    if (!actual.equals(expected, ignoreCase = true)) {
                        tmp.delete()
                        throw IllegalStateException("安装包校验失败，请稍后再试")
                    }
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
                error = if (e is IllegalStateException) {
                    e.message ?: "更新失败，请稍后再试"
                } else {
                    "下载失败：${friendly(e)}"
                }
                statusText = error
                Store.toast = error
            }
        }
    }

    /** 可选校验文件：短超时（10s/20s）最多试 2 次；失败 / 空 / 404 返回 null（跳过校验）。 */
    private fun fetchSumsOptional(url: String): String? {
        repeat(2) {
            try {
                val req = Request.Builder().url(url).header("User-Agent", UA).get().build()
                sumsClient.newCall(req).execute().use { r ->
                    if (r.isSuccessful) {
                        val text = r.body?.string().orEmpty()
                        if (text.isNotBlank()) return text
                    }
                }
            } catch (_: Exception) {
                // 网络慢/被墙/404：再试一次或直接跳过
            }
        }
        return null
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
        is java.net.UnknownHostException -> "网络连不上 GitHub"
        is java.net.SocketTimeoutException -> "连接超时"
        is java.net.ConnectException -> "网络连不上"
        is java.io.IOException -> "网络不稳定"
        else -> "出了点问题"
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

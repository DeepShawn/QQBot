package com.qqbot.console

import android.util.Base64
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.util.concurrent.TimeUnit

/** 机器人形象参考图接口：GET / POST / DELETE /api/avatar/reference（管理台 127.0.0.1:8080）。 */
data class AvatarRef(val bytes: ByteArray? = null, val error: String = "")

object AvatarApi {
    private const val BASE = "http://127.0.0.1:8080"
    private const val PATH = "/api/avatar/reference"
    private val JSON = "application/json; charset=utf-8".toMediaType()
    private val client = OkHttpClient.Builder()
        .connectTimeout(5, TimeUnit.SECONDS)
        .readTimeout(60, TimeUnit.SECONDS)
        .writeTimeout(120, TimeUnit.SECONDS)   // 上传 base64 可能较大
        .build()

    /** 拉取已有参考图；404 / 服务未就绪视为没有（error 为空），其它失败给中文 error。 */
    suspend fun fetch(): AvatarRef = withContext(Dispatchers.IO) {
        try {
            val req = Request.Builder().url(BASE + PATH).get().build()
            client.newCall(req).execute().use { r ->
                if (r.code == 404) {
                    AvatarRef()
                } else if (!r.isSuccessful) {
                    AvatarRef(error = "读取参考图失败：HTTP ${r.code}")
                } else {
                    val bytes = r.body?.bytes()
                    if (bytes == null || bytes.isEmpty()) AvatarRef() else AvatarRef(bytes = bytes)
                }
            }
        } catch (e: Exception) {
            AvatarRef(error = "读取参考图失败：${friendlyError(e)}")
        }
    }

    /** 上传参考图；成功返回 null，失败返回中文错误。 */
    suspend fun upload(bytes: ByteArray, filename: String): String? = withContext(Dispatchers.IO) {
        try {
            val body = JSONObject()
                .put("data_base64", Base64.encodeToString(bytes, Base64.NO_WRAP))
                .put("filename", filename.ifBlank { "reference.png" })
                .toString()
                .toRequestBody(JSON)
            val req = Request.Builder().url(BASE + PATH).post(body).build()
            client.newCall(req).execute().use { r ->
                val text = r.body?.string().orEmpty()
                val obj = try {
                    if (text.isBlank()) null else JSONObject(text)
                } catch (_: Exception) {
                    null
                }
                if (r.isSuccessful && obj?.optBoolean("ok") == true) {
                    null
                } else {
                    obj?.optString("error").orEmpty().ifBlank { "上传参考图失败：HTTP ${r.code}" }
                }
            }
        } catch (e: Exception) {
            "上传参考图失败：${friendlyError(e)}"
        }
    }

    /** 删除参考图；成功返回 null，失败返回中文错误。 */
    suspend fun delete(): String? = withContext(Dispatchers.IO) {
        try {
            val req = Request.Builder().url(BASE + PATH).delete().build()
            client.newCall(req).execute().use { r ->
                val text = r.body?.string().orEmpty()
                val obj = try {
                    if (text.isBlank()) null else JSONObject(text)
                } catch (_: Exception) {
                    null
                }
                if (r.isSuccessful && obj?.optBoolean("ok") == true) {
                    null
                } else {
                    obj?.optString("error").orEmpty().ifBlank { "移除参考图失败：HTTP ${r.code}" }
                }
            }
        } catch (e: Exception) {
            "移除参考图失败：${friendlyError(e)}"
        }
    }

    private fun friendlyError(e: Exception): String = when (e) {
        is java.net.ConnectException -> "管理服务没有响应"
        is java.net.SocketTimeoutException -> "连接超时"
        is java.io.IOException -> "网络不稳定"
        else -> "出了点问题"
    }
}

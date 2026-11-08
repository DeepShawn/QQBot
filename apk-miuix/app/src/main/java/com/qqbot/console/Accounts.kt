package com.qqbot.console

import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import org.json.JSONObject
import java.net.URLEncoder

/** AI 账号（浏览器 OAuth 登录）的接口与状态。 */

val PROVIDER_ORDER = listOf("chatgpt", "claude", "gemini", "deepseek")

val PROVIDER_NAMES = mapOf(
    "chatgpt" to "ChatGPT",
    "claude" to "Claude",
    "gemini" to "Gemini",
    "deepseek" to "DeepSeek",
)

private fun JSONObject.text(key: String): String = if (isNull(key)) "" else optString(key, "")

data class Account(
    val id: String,
    val name: String,
    val signedIn: Boolean = false,
    val account: String = "",
    val models: List<String> = emptyList(),
    val defaultModel: String = "",
    val error: String = "",
) {
    companion object {
        fun placeholder(id: String): Account = Account(id = id, name = PROVIDER_NAMES[id] ?: id)

        fun from(o: JSONObject): Account {
            val id = o.optString("id")
            val modelsArr = o.optJSONArray("models")
            val models = (0 until (modelsArr?.length() ?: 0)).mapNotNull { i -> modelsArr?.optString(i) }
            return Account(
                id = id,
                name = o.optString("name").ifBlank { PROVIDER_NAMES[id] ?: id },
                signedIn = o.optBoolean("signedIn"),
                account = o.text("account"),
                models = models,
                defaultModel = o.text("default_model"),
                error = o.text("error"),
            )
        }
    }
}

/** 登录尝试（/api/accounts/begin 和 poll 返回的 attempt 视图）。 */
data class Attempt(
    val id: String,
    val provider: String,
    val phase: String,
    val message: String = "",
    val url: String = "",
    val userCode: String = "",
    val placeholder: String = "",
    val account: String = "",
    val error: String = "",
) {
    val active: Boolean
        get() = phase == "starting" || phase == "open" || phase == "device" || phase == "prompt"

    companion object {
        fun from(o: JSONObject): Attempt = Attempt(
            id = o.optString("id"),
            provider = o.optString("provider"),
            phase = o.optString("phase"),
            message = o.text("message"),
            url = o.text("url"),
            userCode = o.text("userCode"),
            placeholder = o.text("placeholder"),
            account = o.text("account"),
            error = o.text("error"),
        )
    }
}

data class BeginResult(val attempt: Attempt? = null, val error: String? = null)

object Accounts {
    /** GET /api/accounts；失败返回 null。 */
    suspend fun list(): List<Account>? {
        val r = Api.get("/api/accounts") ?: return null
        if (!r.optBoolean("ok")) return null
        val arr = r.optJSONArray("accounts") ?: return emptyList()
        return (0 until arr.length()).mapNotNull { i -> arr.optJSONObject(i)?.let(Account::from) }
    }

    suspend fun begin(provider: String): BeginResult {
        val r = Api.post("/api/accounts/begin", JSONObject().put("provider", provider))
            ?: return BeginResult(error = "管理服务没有响应")
        if (!r.optBoolean("ok")) {
            return BeginResult(error = r.text("error").ifBlank { "开始登录失败" })
        }
        val a = r.optJSONObject("attempt")?.let(Attempt::from)
            ?: return BeginResult(error = "登录请求没有返回结果")
        return BeginResult(attempt = a)
    }

    /** GET /api/accounts/poll?attempt=…；失败返回 null（界面会重试几次）。 */
    suspend fun poll(attemptId: String): Attempt? {
        if (attemptId.isBlank()) return null
        val path = "/api/accounts/poll?attempt=" + URLEncoder.encode(attemptId, "UTF-8")
        val r = Api.get(path) ?: return null
        if (!r.optBoolean("ok")) return null
        return r.optJSONObject("attempt")?.let(Attempt::from)
    }

    /** 返回错误信息；null 表示提交成功。 */
    suspend fun answer(attemptId: String, value: String): String? {
        val r = Api.post(
            "/api/accounts/answer",
            JSONObject().put("attempt", attemptId).put("value", value),
        ) ?: return "管理服务没有响应"
        return if (r.optBoolean("ok")) null else r.text("error").ifBlank { "提交失败" }
    }

    suspend fun cancel(attemptId: String) {
        if (attemptId.isBlank()) return
        Api.post("/api/accounts/cancel", JSONObject().put("attempt", attemptId))
    }

    /** 返回错误信息；null 表示已退出登录。 */
    suspend fun signout(provider: String): String? {
        val r = Api.post("/api/accounts/signout", JSONObject().put("provider", provider))
            ?: return "管理服务没有响应"
        return if (r.optBoolean("ok")) null else r.text("error").ifBlank { "退出登录失败" }
    }
}

/** 账号列表的全局缓存（AI 页和接口设置都用）。 */
object AccountsStore {
    var accounts by mutableStateOf<List<Account>>(emptyList()); private set
    var loading by mutableStateOf(false); private set
    var error by mutableStateOf(""); private set

    suspend fun refresh() {
        if (loading) return
        loading = true
        val list = Accounts.list()
        loading = false
        if (list == null) {
            error = "管理服务没有响应"
            return
        }
        error = ""
        accounts = list
    }

    fun find(id: String): Account = accounts.firstOrNull { it.id == id } ?: Account.placeholder(id)
}

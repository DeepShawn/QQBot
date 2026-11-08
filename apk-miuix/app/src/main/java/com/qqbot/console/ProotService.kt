package com.qqbot.console

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.os.IBinder
import android.os.PowerManager
import android.net.wifi.WifiManager
import java.io.File

/** 前台服务：在 proot 里常驻运行整套 QQ 机器人（QQ + NapCat + 机器人 + 管理台）。 */
class ProotService : Service() {

    companion object {
        private const val CHANNEL_ID = "qqbot-service"
        private const val NOTIFY_ID = 1001
        private const val PREFS = "service"

        @Volatile var lastLog = ""
            private set
        @Volatile private var process: Process? = null

        fun isRunning(): Boolean = process?.isAlive == true

        fun start(ctx: Context) {
            setWanted(ctx, true)
            ctx.startForegroundService(Intent(ctx, ProotService::class.java))
        }

        fun stop(ctx: Context) {
            setWanted(ctx, false)
            ctx.stopService(Intent(ctx, ProotService::class.java))
        }

        /** 记住「用户希望服务运行」，被系统杀掉后 START_STICKY 拉起时据此恢复。 */
        private fun setWanted(ctx: Context, v: Boolean) =
            ctx.getSharedPreferences(PREFS, MODE_PRIVATE).edit().putBoolean("wanted", v).apply()

        fun isWanted(ctx: Context): Boolean =
            ctx.getSharedPreferences(PREFS, MODE_PRIVATE).getBoolean("wanted", false)
    }

    private var wakeLock: PowerManager.WakeLock? = null
    private var wifiLock: WifiManager.WifiLock? = null

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        val ch = NotificationChannel(CHANNEL_ID, "机器人服务", NotificationManager.IMPORTANCE_LOW)
        ch.description = "QQ 机器人后台运行状态"
        getSystemService(NotificationManager::class.java).createNotificationChannel(ch)
        startForeground(NOTIFY_ID, buildNotification("正在启动…"))
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        // intent == null 表示被系统杀后重建：只有用户希望运行时才恢复
        if (intent == null && !isWanted(this)) {
            stopSelf()
            return START_NOT_STICKY
        }
        if (process?.isAlive == true) return START_STICKY
        if (!Env.validate(this)) {
            lastLog = "运行环境未安装"
            stopSelf()
            return START_NOT_STICKY
        }
        acquireLocks()
        Thread({ runProot() }, "proot-runner").start()
        return START_STICKY
    }

    private fun runProot() {
        try {
            Log.reset()
            Env.prepareProot(this)
            Env.ensureDirs(this)
            Env.cleanStaleLocks(this)
            Env.writeResolvConf(this)

            val tmp = File(cacheDir, "proot-tmp").apply { mkdirs() }.absolutePath
            val pb = ProcessBuilder(Env.prootArgs(this))
                .directory(Env.rootfsDir(this))
                .redirectErrorStream(true)
            pb.environment().apply {
                clear()
                put("PATH", "/system/bin:/system/xbin")
                put("HOME", filesDir.absolutePath)
                put("TMPDIR", tmp)
                put("PROOT_TMP_DIR", tmp)
                put("PROOT_LOADER", File(Env.binDir(this@ProotService), "loader").absolutePath)
                put("PROOT_L2S_DIR", File(Env.rootfsDir(this@ProotService), "root/.l2s").apply { mkdirs() }.absolutePath)
                put("LD_LIBRARY_PATH", Env.libDir(this@ProotService).absolutePath)
            }
            Log.write("[app] 启动 proot：${pb.command().first()}")
            val p = pb.start()
            process = p
            notify("QQ 机器人运行中")
            // 必须持续读输出，否则 64KB 管道写满后子进程会卡住
            p.inputStream.bufferedReader().useLines { lines ->
                lines.forEach { line ->
                    if (line.isNotBlank()) {
                        lastLog = line.take(300)
                        Log.write(line)
                    }
                }
            }
            val code = p.waitFor()
            Log.write("[app] proot 退出，code=$code")
            lastLog = "服务进程已退出（code=$code）"
        } catch (e: Throwable) {
            lastLog = "启动失败：${e.javaClass.simpleName} ${e.message}"
            Log.write("[app] $lastLog")
        } finally {
            process = null
            notify("服务已停止")
        }
    }

    override fun onDestroy() {
        try {
            process?.destroy()
            if (process?.waitFor(5, java.util.concurrent.TimeUnit.SECONDS) == false) {
                process?.destroyForcibly()
            }
        } catch (_: Exception) {}
        process = null
        releaseLocks()
        super.onDestroy()
    }

    /** 熄屏后 CPU 休眠会让 QQ 心跳断掉，所以要持有唤醒锁和 Wi-Fi 锁。 */
    private fun acquireLocks() {
        try {
            val pm = getSystemService(POWER_SERVICE) as PowerManager
            wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "qqbot:service").apply {
                setReferenceCounted(false); acquire()
            }
        } catch (_: Exception) {}
        try {
            val wm = applicationContext.getSystemService(WIFI_SERVICE) as WifiManager
            @Suppress("DEPRECATION")
            wifiLock = wm.createWifiLock(WifiManager.WIFI_MODE_FULL_HIGH_PERF, "qqbot:wifi").apply {
                setReferenceCounted(false); acquire()
            }
        } catch (_: Exception) {}
    }

    private fun releaseLocks() {
        try { wakeLock?.takeIf { it.isHeld }?.release() } catch (_: Exception) {}
        try { wifiLock?.takeIf { it.isHeld }?.release() } catch (_: Exception) {}
        wakeLock = null; wifiLock = null
    }

    private fun buildNotification(text: String): Notification {
        val intent = PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        return Notification.Builder(this, CHANNEL_ID)
            .setContentTitle("机器人控制台")
            .setContentText(text)
            .setSmallIcon(android.R.drawable.stat_notify_chat)
            .setOngoing(true)
            .setContentIntent(intent)
            .build()
    }

    private fun notify(text: String) {
        try {
            getSystemService(NotificationManager::class.java).notify(NOTIFY_ID, buildNotification(text))
        } catch (_: Exception) {}
    }
}

package com.qqbot.console

import android.content.Context
import android.os.StatFs
import java.io.File
import java.io.FilterInputStream
import java.io.IOException
import java.io.InputStream

/** 本地运行环境：安装 rootfs、准备 proot、生成启动参数。 */
object Env {
    const val MARKER = ".installed"
    const val SERVICE_PORT = 8080

    /** 默认安装包位置（由 DSH 端打包生成）。 */
    val DEFAULT_PACKAGE = File("/sdcard/Download/DSH-Go/rootfs-pkg/rootfs.tar.gz")

    /** 运行日志镜像到这里，方便在手机文件管理里直接查看。 */
    val PUBLIC_LOG = File("/sdcard/Download/DSH-Go/qqbot-app.log")

    fun rootfsDir(ctx: Context): File = File(ctx.filesDir, "rootfs")
    fun binDir(ctx: Context): File = File(ctx.filesDir, "bin")
    fun libDir(ctx: Context): File = File(ctx.filesDir, "lib")

    private val MUST_HAVE = listOf(
        "usr/bin/bash",
        "opt/QQ/qq",
        "opt/python/bin/python3.12",
        "root/workspace/qqbot/start-all.sh",
    )

    fun isInstalled(ctx: Context): Boolean = File(rootfsDir(ctx), MARKER).isFile

    /** rootfs 是否完整可用（避免解压中断后残留半成品）。 */
    fun validate(ctx: Context): Boolean {
        if (!isInstalled(ctx)) return false
        val root = rootfsDir(ctx)
        return MUST_HAVE.all { File(root, it).exists() }
    }

    fun freeBytes(ctx: Context): Long = try {
        StatFs(ctx.filesDir.absolutePath).availableBytes
    } catch (e: Exception) { Long.MAX_VALUE }

    // ---------------------------------------------------------------- proot 二进制

    /**
     * 释放 assets 里的 proot / loader / .so 并加可执行位。
     * 注意：不能遍历 assets.list("")——Android 会把系统框架的资源目录
     * （images、webkit、geoid_height_map…）也列进来，open() 目录会抛 FileNotFoundException。
     */
    private val BIN_ASSETS = listOf("proot", "loader")
    private val LIB_ASSETS = listOf("libtalloc.so.2", "libandroid-shmem.so")

    fun prepareProot(ctx: Context) {
        val bin = binDir(ctx).apply { mkdirs() }
        val lib = libDir(ctx).apply { mkdirs() }
        fun release(name: String, dir: File) {
            // 一共才 ~300KB，每次都重新释放，保证升级后不会用到旧二进制
            val dst = File(dir, name)
            val tmp = File(dir, "$name.tmp")
            ctx.assets.open(name).use { input ->
                tmp.outputStream().use { out -> input.copyTo(out, 1 shl 16) }
            }
            if (!tmp.renameTo(dst)) throw IOException("无法写入 $dst")
            dst.setReadable(true, false)
            dst.setExecutable(true, false)
        }
        BIN_ASSETS.forEach { release(it, bin) }
        LIB_ASSETS.forEach { release(it, lib) }
    }

    // ---------------------------------------------------------------- 安装 rootfs

    /** 统计已读取的压缩字节数，用来算真实进度。 */
    private class CountingStream(input: InputStream, val onRead: (Long) -> Unit) : FilterInputStream(input) {
        var count = 0L
        override fun read(): Int = super.read().also { if (it >= 0) { count++; onRead(count) } }
        override fun read(b: ByteArray, off: Int, len: Int): Int =
            super.read(b, off, len).also { if (it > 0) { count += it; onRead(count) } }
    }

    /**
     * 解压到 rootfs.staging，校验通过后再原子替换成 rootfs。
     * onProgress(已读压缩字节, 总字节, 已解出文件数)
     */
    fun install(
        ctx: Context,
        open: () -> InputStream,
        totalBytes: Long,
        onProgress: (read: Long, total: Long, files: Long) -> Unit,
    ) {
        // 解压后约是压缩包的 2.6 倍，再留 1GB 余量
        val need = totalBytes * 3 + (1L shl 30)
        if (freeBytes(ctx) < need) {
            throw IOException("存储空间不足：需要约 ${need shr 30} GB，可用 ${freeBytes(ctx) shr 30} GB")
        }
        val target = rootfsDir(ctx)
        val staging = File(ctx.filesDir, "rootfs.staging")
        staging.deleteRecursively()
        staging.mkdirs()

        var files = 0L
        var lastTick = 0L
        open().use { raw ->
            val counting = CountingStream(raw) { read ->
                if (read - lastTick > (4L shl 20)) {          // 每 4MB 回调一次
                    lastTick = read
                    onProgress(read, totalBytes, files)
                }
            }
            val extractor = TarGzExtractor(counting, staging) { f, _ -> files = f }
            extractor.extract()
            if (extractor.failed > 0) {
                Log.write("解压完成，${extractor.failed} 个条目失败，首个：${extractor.lastError}")
            }
        }
        onProgress(totalBytes, totalBytes, files)

        val missing = MUST_HAVE.filter { !File(staging, it).exists() }
        if (missing.isNotEmpty()) {
            staging.deleteRecursively()
            throw IOException("安装包不完整，缺少：" + missing.joinToString())
        }
        target.deleteRecursively()
        if (!staging.renameTo(target)) throw IOException("无法启用运行环境（rename 失败）")
        File(target, MARKER).writeText(System.currentTimeMillis().toString())
    }

    // ---------------------------------------------------------------- 启动前准备

    /** 每次启动重写 DNS，避免沿用打包时手机网络 / VPN 的 DNS。 */
    fun writeResolvConf(ctx: Context) {
        try {
            val f = File(rootfsDir(ctx), "etc/resolv.conf")
            f.delete()   // 可能是指向别处的软链，先删再写
            f.writeText("nameserver 223.5.5.5\nnameserver 119.29.29.29\noptions single-request-reopen\n")
        } catch (e: Exception) {
            Log.write("写 resolv.conf 失败：${e.message}")
        }
    }

    /** 安装包里不带这些目录（打包时排除了），每次启动前补齐。缺 /tmp 会让 xvfb-run 直接失败。 */
    fun ensureDirs(ctx: Context) {
        val root = rootfsDir(ctx)
        val worldWritable = listOf("tmp", "var/tmp", "run", "run/lock", "dev/shm")
        val plain = listOf(
            "var/log", "var/cache", "var/lib/qqbot-empty", "var/lib/qqbot-shm",
            "proc", "sys", "dev", "mnt", "media", "sdcard", "storage", "system", "apex",
            "root/workspace/qqbot/logs", "root/workspace/qqbot/run",
            "root/workspace/qqbot/protocol/logs",
        )
        plain.forEach { File(root, it).mkdirs() }
        worldWritable.forEach { name ->
            val d = File(root, name)
            d.mkdirs()
            try { android.system.Os.chmod(d.absolutePath, "1777".toInt(8)) } catch (_: Exception) {
                d.setReadable(true, false); d.setWritable(true, false); d.setExecutable(true, false)
            }
        }
    }

    /** 冷启动清理残留锁：X 显示锁、QQ 单例锁、旧 pid 文件。 */
    fun cleanStaleLocks(ctx: Context) {
        val root = rootfsDir(ctx)
        try {
            File(root, "tmp").listFiles()?.forEach { it.deleteRecursively() }
            File(root, "root/.config/QQ").listFiles()
                ?.filter { it.name.startsWith("Singleton") }
                ?.forEach { it.delete() }
            File(root, "root/workspace/qqbot/run").listFiles()
                ?.filter { it.name.endsWith(".pid") }
                ?.forEach { it.delete() }
        } catch (_: Exception) {}
    }

    fun serviceAlive(): Boolean = portOpen(SERVICE_PORT)

    fun portOpen(port: Int): Boolean = try {
        java.net.Socket().use { s -> s.connect(java.net.InetSocketAddress("127.0.0.1", port), 600) }
        true
    } catch (e: Exception) { false }

    /** proot 参数：与 DSH 在这台手机上验证过的一套保持一致。 */
    fun prootArgs(ctx: Context): List<String> {
        val root = rootfsDir(ctx).absolutePath
        val proot = File(binDir(ctx), "proot").absolutePath
        val emptyDir = File(root, "var/lib/qqbot-empty").apply { mkdirs() }.absolutePath
        val shmDir = File(root, "var/lib/qqbot-shm").apply { mkdirs() }.absolutePath
        return listOf(
            proot,
            "--kill-on-exit", "--link2symlink", "--sysvipc", "-L", "-0",
            "--rootfs=$root", "--cwd=/root",
            "-b", "/dev",
            "-b", "/dev/urandom:/dev/random",
            "-b", "/proc",
            "-b", "/proc/self/fd:/dev/fd",
            "-b", "/proc/self/fd/0:/dev/stdin",
            "-b", "/proc/self/fd/1:/dev/stdout",
            "-b", "/proc/self/fd/2:/dev/stderr",
            "-b", "/sys",
            "-b", "$emptyDir:/sys/fs/selinux",
            "-b", "$shmDir:/dev/shm",
            "-b", "/system",
            "-b", "/apex",
            "-b", "/storage/emulated/0:/sdcard",
            "-b", "/storage",
            "/usr/bin/env", "-i",
            "HOME=/root", "USER=root",
            "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "TERM=xterm", "LANG=C.UTF-8", "LC_ALL=C.UTF-8", "TZ=Asia/Shanghai",
            "TMPDIR=/tmp", "UV_USE_IO_URING=0",
            "/bin/bash", "-lc",
            // 先起管理台（几秒就好），App 就能马上进主界面；QQ 登录慢慢来
            "cd /root/workspace/qqbot && " +
                // 可选：把手机存储里的代码同步进运行环境（存在才同步）
                "if [ -d /sdcard/Download/DSH-Go/qqbot-src ]; then " +
                "echo '[sync] 从 sdcard/Download/DSH-Go/qqbot-src 同步机器人代码'; " +
                "cp -a /sdcard/Download/DSH-Go/qqbot-src/bot/. /root/workspace/qqbot/bot/ 2>/dev/null; " +
                "[ -d /sdcard/Download/DSH-Go/qqbot-src/scripts ] && cp -a /sdcard/Download/DSH-Go/qqbot-src/scripts/. /root/workspace/qqbot/scripts/ 2>/dev/null; " +
                "for f in /sdcard/Download/DSH-Go/qqbot-src/*.sh; do [ -e \"\$f\" ] && cp -a \"\$f\" /root/workspace/qqbot/; done; " +
                "fi; " +
                "bash scripts/admin-start.sh; ./start-all.sh; " +
                "echo '===== qq-stderr.log ====='; tail -n 60 protocol/logs/qq-stderr.log 2>&1; " +
                "echo '===== napcat.log ====='; tail -n 30 protocol/logs/napcat.log 2>&1; " +
                "echo '===== env ====='; uname -a; id; ls -la /dev/shm /tmp 2>&1 | head; " +
                "exec sleep infinity",
        )
    }
}

/** 简单的文件日志：写到公共下载目录，超过 2MB 自动截断。 */
object Log {
    private val lock = Any()

    fun reset() = synchronized(lock) {
        try { Env.PUBLIC_LOG.parentFile?.mkdirs(); Env.PUBLIC_LOG.writeText("") } catch (_: Exception) {}
    }

    fun write(line: String) = synchronized(lock) {
        try {
            val f = Env.PUBLIC_LOG
            if (f.length() > (2L shl 20)) f.writeText("")
            f.appendText(line + "\n")
        } catch (_: Exception) {}
    }
}

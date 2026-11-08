package com.qqbot.console

import org.apache.commons.compress.archivers.tar.TarArchiveInputStream
import org.apache.commons.compress.compressors.gzip.GzipCompressorInputStream
import java.io.BufferedInputStream
import java.io.File
import java.io.InputStream
import java.nio.file.Files
import java.nio.file.LinkOption
import java.nio.file.Path
import java.nio.file.StandardCopyOption

/**
 * 用 commons-compress 解压 tar.gz：正确处理 PAX / GNU 长文件名、符号链接、硬链接、权限位。
 * 单条目失败不会中断整体（记录失败数与最后错误）。
 */
class TarGzExtractor(
    private val input: InputStream,
    private val rootDir: File,
    private val onProgress: (files: Long, bytes: Long) -> Unit,
) {
    private val rootPath: Path = rootDir.toPath()
    private var fileCount = 0L
    private var byteCount = 0L
    var failed = 0
        private set
    var lastError = ""
        private set

    fun extract() {
        rootDir.mkdirs()
        GzipCompressorInputStream(BufferedInputStream(input, 1 shl 16)).use { gz ->
            TarArchiveInputStream(gz).use { tar ->
                var entry = tar.nextEntry
                while (entry != null) {
                    val name = entry.name.trimStart('/')
                    if (name.isNotEmpty() && !name.contains("..")) {
                        val target = rootPath.resolve(name)
                        if (target.startsWith(rootPath)) {
                            try {
                                when {
                                    entry.isDirectory -> target.toFile().mkdirs()
                                    entry.isSymbolicLink -> {
                                        target.toFile().parentFile?.mkdirs()
                                        try {
                                            Files.deleteIfExists(target)
                                            Files.createSymbolicLink(target, Path.of(entry.linkName))
                                        } catch (e: Exception) {
                                            try { target.toFile().createNewFile() } catch (_: Exception) {}
                                        }
                                    }
                                    entry.isLink -> {
                                        target.toFile().parentFile?.mkdirs()
                                        val src = rootPath.resolve(entry.linkName.trimStart('/'))
                                        if (Files.exists(src, LinkOption.NOFOLLOW_LINKS)) {
                                            Files.copy(src, target, StandardCopyOption.REPLACE_EXISTING)
                                        }
                                    }
                                    else -> {
                                        target.toFile().parentFile?.mkdirs()
                                        Files.newOutputStream(target).use { out -> tar.copyTo(out, 1 shl 16) }
                                        applyMode(target, entry.mode)
                                        fileCount++
                                        byteCount += entry.size
                                        if (fileCount % 200 == 0L) onProgress(fileCount, byteCount)
                                    }
                                }
                            } catch (e: Exception) {
                                failed++
                                if (failed <= 3) lastError = "$name: ${e.javaClass.simpleName} ${e.message}"
                            }
                        }
                    }
                    entry = tar.nextEntry
                }
            }
        }
        onProgress(fileCount, byteCount)
    }

    private fun applyMode(path: Path, mode: Int) {
        try {
            val f = path.toFile()
            f.setReadable(true, false)
            f.setWritable(true, true)
            if (mode and 0b001_000_000 != 0) f.setExecutable(true, false)
        } catch (_: Exception) {}
    }
}

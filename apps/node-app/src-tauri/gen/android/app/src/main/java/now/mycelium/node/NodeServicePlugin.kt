// SPDX-License-Identifier: AGPL-3.0-only
package now.mycelium.node

import android.Manifest
import android.app.Activity
import android.content.ContentValues
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.PackageManager
import android.os.BatteryManager
import android.os.Build
import android.os.PowerManager
import android.os.SystemClock
import android.provider.MediaStore
import androidx.core.content.FileProvider
import app.tauri.annotation.Command
import app.tauri.annotation.InvokeArg
import app.tauri.annotation.TauriPlugin
import app.tauri.plugin.Invoke
import app.tauri.plugin.JSObject
import app.tauri.plugin.Plugin

@InvokeArg
class StartArgs {
  var detail: String? = null
}

@InvokeArg
class ShareArgs {
  var path: String? = null
}

/** Tauri bridge: start/stop/status of the serving foreground service, and evidence share. */
@TauriPlugin
class NodeServicePlugin(private val activity: Activity) : Plugin(activity) {
  @Command
  fun start(invoke: Invoke) {
    val args = invoke.parseArgs(StartArgs::class.java)
    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU &&
      activity.checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED
    ) {
      activity.requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), 4102)
    }
    val intent = Intent(activity, NodeForegroundService::class.java)
    args.detail?.let { intent.putExtra(NodeForegroundService.EXTRA_DETAIL, it) }
    activity.startForegroundService(intent)
    invoke.resolve(status())
  }

  @Command
  fun stop(invoke: Invoke) {
    val intent = Intent(activity, NodeForegroundService::class.java)
      .setAction(NodeForegroundService.ACTION_STOP)
    activity.startService(intent)
    invoke.resolve(JSObject().put("running", false))
  }

  /**
   * Offer one exported evidence bundle to the share sheet and, on Android 10+, save the same
   * bytes to the public Downloads collection (MediaStore, no storage permission). Only files
   * under cache/exports named mycelium-evidence-<ms>.json are accepted.
   */
  @Command
  fun share(invoke: Invoke) {
    val args = invoke.parseArgs(ShareArgs::class.java)
    val exports = java.io.File(activity.cacheDir, "exports").canonicalFile
    val file = args.path?.let { java.io.File(it).canonicalFile }
    if (file == null || file.parentFile != exports || !file.isFile || !EVIDENCE_NAME.matches(file.name)) {
      invoke.reject("mobile_evidence_export_path_invalid")
      return
    }
    val uri = FileProvider.getUriForFile(activity, "${activity.packageName}.fileprovider", file)
    val send = Intent(Intent.ACTION_SEND)
      .setType("application/json")
      .putExtra(Intent.EXTRA_STREAM, uri)
      .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
    val downloads = saveToDownloads(file)
    activity.startActivity(Intent.createChooser(send, "Export evidence"))
    invoke.resolve(JSObject().put("shared", true).put("downloads", downloads))
  }

  /** Copy the bundle into Downloads; returns the saved file name, or null below Android 10 or on failure. */
  private fun saveToDownloads(file: java.io.File): String? {
    if (Build.VERSION.SDK_INT < Build.VERSION_CODES.Q) return null
    val resolver = activity.contentResolver
    val values = ContentValues().apply {
      put(MediaStore.MediaColumns.DISPLAY_NAME, file.name)
      put(MediaStore.MediaColumns.MIME_TYPE, "application/json")
      put(MediaStore.MediaColumns.IS_PENDING, 1)
    }
    val uri = resolver.insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, values) ?: return null
    return try {
      resolver.openOutputStream(uri)?.use { out -> file.inputStream().use { it.copyTo(out) } }
        ?: throw java.io.IOException("no output stream")
      resolver.update(uri, ContentValues().apply { put(MediaStore.MediaColumns.IS_PENDING, 0) }, null, null)
      file.name
    } catch (e: Exception) {
      resolver.delete(uri, null, null)
      null
    }
  }

  private companion object {
    val EVIDENCE_NAME = Regex("^mycelium-evidence-[0-9]+\\.json$")
  }

  @Command
  fun status(invoke: Invoke) {
    invoke.resolve(status())
  }

  /**
   * Power and thermal readings an unprivileged app can take, for the node core's
   * sealed runtime observations. Battery level, charging and battery temperature
   * come from the sticky ACTION_BATTERY_CHANGED broadcast (no receiver is
   * registered); thermal status (API 29+) and headroom (API 30+) from PowerManager.
   * A reading the OS does not provide is null, never a guess.
   */
  @Command
  fun platformMetrics(invoke: Invoke) {
    val out = JSObject()
    val battery = activity.registerReceiver(null, IntentFilter(Intent.ACTION_BATTERY_CHANGED))
    val level = battery?.getIntExtra(BatteryManager.EXTRA_LEVEL, -1) ?: -1
    val scale = battery?.getIntExtra(BatteryManager.EXTRA_SCALE, -1) ?: -1
    out.put("battery_level_percent", if (level >= 0 && scale > 0) level * 100.0 / scale else null)
    val status = battery?.getIntExtra(BatteryManager.EXTRA_STATUS, -1) ?: -1
    out.put(
      "charging",
      when (status) {
        BatteryManager.BATTERY_STATUS_CHARGING, BatteryManager.BATTERY_STATUS_FULL -> 1.0
        BatteryManager.BATTERY_STATUS_DISCHARGING, BatteryManager.BATTERY_STATUS_NOT_CHARGING -> 0.0
        else -> null
      },
    )
    val tenths = battery?.getIntExtra(BatteryManager.EXTRA_TEMPERATURE, Int.MIN_VALUE) ?: Int.MIN_VALUE
    out.put("battery_temperature_celsius", if (tenths != Int.MIN_VALUE) tenths / 10.0 else null)
    val power = activity.getSystemService(Context.POWER_SERVICE) as PowerManager
    out.put(
      "thermal_status",
      if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) power.currentThermalStatus.toDouble() else null,
    )
    val headroom = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) power.getThermalHeadroom(0) else Float.NaN
    out.put("thermal_headroom", if (headroom.isNaN()) null else headroom.toDouble())
    invoke.resolve(out)
  }

  private fun status(): JSObject {
    val running = NodeForegroundService.running
    val uptime = if (running) SystemClock.elapsedRealtime() - NodeForegroundService.startedElapsedMs else 0L
    return JSObject().put("running", running).put("uptime_ms", uptime)
  }
}

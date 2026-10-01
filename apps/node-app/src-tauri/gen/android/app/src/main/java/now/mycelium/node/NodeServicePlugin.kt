package now.mycelium.node

import android.Manifest
import android.app.Activity
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.os.SystemClock
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

  /** Offer one exported evidence bundle to the share sheet; only files under cache/exports. */
  @Command
  fun share(invoke: Invoke) {
    val args = invoke.parseArgs(ShareArgs::class.java)
    val exports = java.io.File(activity.cacheDir, "exports").canonicalFile
    val file = args.path?.let { java.io.File(it).canonicalFile }
    if (file == null || file.parentFile != exports || !file.isFile) {
      invoke.reject("mobile_evidence_export_path_invalid")
      return
    }
    val uri = FileProvider.getUriForFile(activity, "${activity.packageName}.fileprovider", file)
    val send = Intent(Intent.ACTION_SEND)
      .setType("application/json")
      .putExtra(Intent.EXTRA_STREAM, uri)
      .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
    activity.startActivity(Intent.createChooser(send, "Export evidence"))
    invoke.resolve(JSObject().put("shared", true))
  }

  @Command
  fun status(invoke: Invoke) {
    invoke.resolve(status())
  }

  private fun status(): JSObject {
    val running = NodeForegroundService.running
    val uptime = if (running) SystemClock.elapsedRealtime() - NodeForegroundService.startedElapsedMs else 0L
    return JSObject().put("running", running).put("uptime_ms", uptime)
  }
}

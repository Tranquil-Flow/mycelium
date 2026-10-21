// SPDX-License-Identifier: AGPL-3.0-only
package now.mycelium.node

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import android.os.PowerManager
import android.os.SystemClock

/**
 * Keeps the app process (and therefore the Rust node core that lives in it) alive
 * while the user has chosen to serve. The service owns no protocol logic: the Rust
 * core does the work; this class only holds the OS-visible foreground contract,
 * a user-visible ongoing notification, and a partial wake lock.
 */
class NodeForegroundService : Service() {
  private var wakeLock: PowerManager.WakeLock? = null

  override fun onBind(intent: Intent?): IBinder? = null

  override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
    if (intent?.action == ACTION_STOP) {
      stopServing()
      return START_NOT_STICKY
    }
    val detail = intent?.getStringExtra(EXTRA_DETAIL) ?: "Serving as a Mycelium node"
    val notification = buildNotification(detail)
    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE) {
      startForeground(NOTIFICATION_ID, notification, ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE)
    } else {
      startForeground(NOTIFICATION_ID, notification)
    }
    if (wakeLock == null) {
      val pm = getSystemService(Context.POWER_SERVICE) as PowerManager
      wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "mycelium:node").apply {
        setReferenceCounted(false)
        acquire(WAKE_LOCK_TIMEOUT_MS)
      }
    }
    running = true
    startedElapsedMs = SystemClock.elapsedRealtime()
    // Not sticky: if the OS kills the process the member goes stale and later
    // reconnects with a fresh incarnation; the service is never silently resurrected.
    return START_NOT_STICKY
  }

  override fun onDestroy() {
    releaseWakeLock()
    running = false
    super.onDestroy()
  }

  private fun stopServing() {
    releaseWakeLock()
    running = false
    stopForeground(STOP_FOREGROUND_REMOVE)
    stopSelf()
  }

  private fun releaseWakeLock() {
    wakeLock?.let { if (it.isHeld) it.release() }
    wakeLock = null
  }

  private fun buildNotification(detail: String): Notification {
    val manager = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
    if (manager.getNotificationChannel(CHANNEL_ID) == null) {
      manager.createNotificationChannel(
        NotificationChannel(CHANNEL_ID, "Node serving", NotificationManager.IMPORTANCE_LOW),
      )
    }
    val open = PendingIntent.getActivity(
      this, 0,
      Intent(this, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP),
      PendingIntent.FLAG_IMMUTABLE,
    )
    val stop = PendingIntent.getService(
      this, 1,
      Intent(this, NodeForegroundService::class.java).setAction(ACTION_STOP),
      PendingIntent.FLAG_IMMUTABLE,
    )
    return Notification.Builder(this, CHANNEL_ID)
      .setContentTitle("Mycelium node")
      .setContentText(detail)
      .setSmallIcon(R.mipmap.ic_launcher)
      .setOngoing(true)
      .setContentIntent(open)
      .addAction(Notification.Action.Builder(null, "Stop", stop).build())
      .build()
  }

  companion object {
    const val CHANNEL_ID = "mycelium_node_serving"
    const val NOTIFICATION_ID = 4101
    const val ACTION_STOP = "now.mycelium.node.STOP_SERVING"
    const val EXTRA_DETAIL = "detail"
    // Bounded: the wake lock is re-acquired on each start and never held indefinitely.
    const val WAKE_LOCK_TIMEOUT_MS = 6L * 60L * 60L * 1000L

    @Volatile var running: Boolean = false
    @Volatile var startedElapsedMs: Long = 0L
  }
}

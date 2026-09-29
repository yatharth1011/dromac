package com.dromac.app

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.app.RemoteInput
import android.app.admin.DevicePolicyManager
import android.app.usage.UsageStatsManager
import android.bluetooth.BluetoothAdapter
import android.bluetooth.BluetoothManager
import android.bluetooth.le.AdvertiseCallback
import android.bluetooth.le.AdvertiseData
import android.bluetooth.le.AdvertiseSettings
import android.bluetooth.le.BluetoothLeAdvertiser
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.PackageManager
import android.database.Cursor
import android.graphics.Bitmap
import android.hardware.camera2.CameraCaptureSession
import android.hardware.camera2.CameraDevice
import android.hardware.camera2.CaptureRequest
import android.hardware.display.DisplayManager
import android.hardware.display.VirtualDisplay
import android.location.LocationManager
import android.media.Image
import android.media.ImageReader
import android.media.projection.MediaProjection
import android.media.projection.MediaProjectionManager
import android.media.AudioAttributes
import android.media.AudioManager
import android.media.MediaMetadata
import android.media.Rating
import android.media.RingtoneManager
import android.media.session.MediaController
import android.media.session.MediaSessionManager
import android.media.session.PlaybackState
import android.net.ConnectivityManager
import android.net.Network
import android.net.NetworkCapabilities
import android.net.Uri
import android.net.nsd.NsdManager
import android.net.nsd.NsdServiceInfo
import android.os.BatteryManager
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.HandlerThread
import android.os.IBinder
import android.os.Looper
import android.os.PowerManager
import android.os.VibrationEffect
import android.os.Vibrator
import android.os.VibratorManager
import android.provider.CalendarContract
import android.service.notification.StatusBarNotification
import android.telecom.TelecomManager
import android.telephony.PhoneStateListener
import android.telephony.TelephonyManager
import android.view.KeyEvent
import androidx.core.app.NotificationCompat
import org.json.JSONArray
import org.json.JSONObject
import java.io.BufferedReader
import java.io.ByteArrayOutputStream
import java.io.InputStreamReader
import java.io.OutputStream
import java.net.Inet4Address
import java.net.NetworkInterface
import java.net.ServerSocket
import java.net.Socket

class StationServerService : Service() {

    companion object {
        // 0xFFFF is the Bluetooth SIG "reserved for testing" company id — appropriate
        // for a private, unregistered local-use beacon like this one.
        const val BLE_MANUFACTURER_ID = 0xFFFF
        // Updated on every HTTP request handled. The Mac dashboard polls every ~2s
        // while open, so "recent" (a few seconds) is a good proxy for "connected" --
        // exposed so MainActivity can show real connection status, not just whether
        // the various permissions are granted.
        @Volatile var lastRequestAt: Long = 0
        // Set by SmsOtpReceiver when a verification code is spotted in an incoming SMS.
        @Volatile var latestOtp: String? = null
        @Volatile var latestOtpAt: Long = 0
        // Lets MainActivity (the only thing allowed to show the system screen-capture
        // consent dialog) hand the grant to this plain, unbound Service.
        @Volatile var instance: StationServerService? = null
    }

    private var serverSocket: ServerSocket? = null
    private var serverThread: Thread? = null
    private val port = 8822
    @Volatile private var running = false

    private var nsdManager: NsdManager? = null
    private var nsdRegistered = false
    private val nsdListener = object : NsdManager.RegistrationListener {
        override fun onServiceRegistered(info: NsdServiceInfo) { nsdRegistered = true }
        override fun onRegistrationFailed(info: NsdServiceInfo, errorCode: Int) { nsdRegistered = false }
        override fun onServiceUnregistered(info: NsdServiceInfo) { nsdRegistered = false }
        override fun onUnregistrationFailed(info: NsdServiceInfo, errorCode: Int) {}
    }

    private var bleAdvertiser: BluetoothLeAdvertiser? = null
    @Volatile private var bleAdvertising = false
    private var lastAdvertisedIp: String? = null
    private val bleAdvertiseCallback = object : AdvertiseCallback() {
        override fun onStartSuccess(settingsInEffect: AdvertiseSettings?) { bleAdvertising = true }
        override fun onStartFailure(errorCode: Int) { bleAdvertising = false }
    }
    private var connectivityManager: ConnectivityManager? = null
    private val networkCallback = object : ConnectivityManager.NetworkCallback() {
        override fun onCapabilitiesChanged(network: Network, capabilities: NetworkCapabilities) { maintainBleAdvertising() }
        override fun onAvailable(network: Network) { maintainBleAdvertising() }
    }
    private val bluetoothStateReceiver = object : android.content.BroadcastReceiver() {
        override fun onReceive(context: Context, intent: Intent) {
            when (intent.getIntExtra(BluetoothAdapter.EXTRA_STATE, -1)) {
                BluetoothAdapter.STATE_ON -> maintainBleAdvertising()
                BluetoothAdapter.STATE_OFF, BluetoothAdapter.STATE_TURNING_OFF -> {
                    // The adapter itself killed our advertise set; drop our stale
                    // handle so the next tick does a clean re-acquire instead of
                    // calling stopAdvertising on a dead reference.
                    bleAdvertiser = null
                    bleAdvertising = false
                }
            }
        }
    }
    private val bleHandler = Handler(Looper.getMainLooper())
    private val bleRetryRunnable = object : Runnable {
        override fun run() {
            maintainBleAdvertising()
            bleHandler.postDelayed(this, 5000)
        }
    }

    // Once the Mac is actively polling us, the beacon has done its job -- broadcasting
    // is otherwise pure battery drain with nobody listening for it. Stop it while
    // connected, and let the periodic tick above resume it the moment the Mac goes
    // quiet (dashboard closed, Mac asleep, link dropped), so reconnection is still
    // near-instant, just not at the cost of advertising 24/7 for no reason.
    private val CONNECTED_IDLE_MS = 6000L // Mac polls every ~2s while its dashboard is open

    private fun isMacConnected(): Boolean =
        lastRequestAt > 0 && System.currentTimeMillis() - lastRequestAt < CONNECTED_IDLE_MS

    private fun maintainBleAdvertising() {
        if (isMacConnected()) {
            if (bleAdvertising) stopBleAdvertising()
        } else {
            startBleAdvertising(force = true)
        }
    }

    // ---------- incoming call tracking ----------
    @Volatile private var incomingCallNumber: String? = null
    @Volatile private var callRinging = false
    private var telephonyManager: TelephonyManager? = null
    @Suppress("DEPRECATION")
    private val phoneStateListener = object : PhoneStateListener() {
        @Suppress("DEPRECATION")
        override fun onCallStateChanged(state: Int, phoneNumber: String?) {
            when (state) {
                TelephonyManager.CALL_STATE_RINGING -> {
                    callRinging = true
                    incomingCallNumber = phoneNumber
                }
                else -> {
                    callRinging = false
                    incomingCallNumber = null
                }
            }
        }
    }

    // ---------- keep-awake ----------
    private var keepAwakeWakeLock: PowerManager.WakeLock? = null

    override fun onCreate() {
        super.onCreate()
        instance = this
        startForegroundNotification()
        startServer()
        registerNsd()
        connectivityManager = getSystemService(Context.CONNECTIVITY_SERVICE) as? ConnectivityManager
        try { connectivityManager?.registerDefaultNetworkCallback(networkCallback) } catch (_: Exception) {}
        try {
            registerReceiver(bluetoothStateReceiver, IntentFilter(BluetoothAdapter.ACTION_STATE_CHANGED))
        } catch (_: Exception) {}
        try {
            telephonyManager = getSystemService(Context.TELEPHONY_SERVICE) as? TelephonyManager
            @Suppress("DEPRECATION")
            telephonyManager?.listen(phoneStateListener, PhoneStateListener.LISTEN_CALL_STATE)
        } catch (_: Exception) {}
        // Retried on a timer (not just once) because BLUETOOTH_ADVERTISE permission may be
        // granted moments after this service starts, and because the adapter can be off at boot.
        bleHandler.post(bleRetryRunnable)
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        return START_STICKY
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onDestroy() {
        running = false
        try { serverSocket?.close() } catch (_: Exception) {}
        unregisterNsd()
        bleHandler.removeCallbacks(bleRetryRunnable)
        stopBleAdvertising()
        try { connectivityManager?.unregisterNetworkCallback(networkCallback) } catch (_: Exception) {}
        try { unregisterReceiver(bluetoothStateReceiver) } catch (_: Exception) {}
        @Suppress("DEPRECATION")
        try { telephonyManager?.listen(phoneStateListener, PhoneStateListener.LISTEN_NONE) } catch (_: Exception) {}
        try { keepAwakeWakeLock?.release() } catch (_: Exception) {}
        stopMirroring()
        stopCameraStream()
        try { blackoutView?.let { (getSystemService(Context.WINDOW_SERVICE) as android.view.WindowManager).removeView(it) } } catch (_: Exception) {}
        instance = null
        super.onDestroy()
    }

    // Continuously broadcasts a tiny BLE beacon ("here's my current IP:port") so a
    // nearby Mac can find this phone by physical proximity alone, with zero IP-range
    // guessing — works even across routed subnets/VLANs where mDNS can't reach.
    // `force` re-asserts advertising even if we think it's already running, since the
    // system can drop it silently (see bleRetryRunnable).
    private fun startBleAdvertising(force: Boolean = false) {
        try {
            val ip = getLocalIpv4() ?: return
            if (!force && ip == lastAdvertisedIp && bleAdvertising) return
            stopBleAdvertising()
            val payload = buildBlePayload(ip, port) ?: return
            val btManager = getSystemService(Context.BLUETOOTH_SERVICE) as? BluetoothManager ?: return
            val adapter: BluetoothAdapter = btManager.adapter ?: return
            if (!adapter.isEnabled) return
            val advertiser = adapter.bluetoothLeAdvertiser ?: return
            val settings = AdvertiseSettings.Builder()
                .setAdvertiseMode(AdvertiseSettings.ADVERTISE_MODE_LOW_LATENCY)
                .setTxPowerLevel(AdvertiseSettings.ADVERTISE_TX_POWER_HIGH)
                .setConnectable(false)
                .build()
            val data = AdvertiseData.Builder()
                .setIncludeDeviceName(false)
                .setIncludeTxPowerLevel(false)
                .addManufacturerData(BLE_MANUFACTURER_ID, payload)
                .build()
            bleAdvertiser = advertiser
            advertiser.startAdvertising(settings, data, bleAdvertiseCallback)
            lastAdvertisedIp = ip
        } catch (_: SecurityException) {
            // BLUETOOTH_ADVERTISE not granted yet; the retry timer will try again,
            // and mDNS/IP-scan discovery still work in the meantime.
        } catch (_: Exception) {}
    }

    private fun stopBleAdvertising() {
        try { bleAdvertiser?.stopAdvertising(bleAdvertiseCallback) } catch (_: Exception) {}
        bleAdvertising = false
    }

    private fun getLocalIpv4(): String? {
        return try {
            val interfaces = java.util.Collections.list(NetworkInterface.getNetworkInterfaces())
            for (iface in interfaces) {
                val addresses = java.util.Collections.list(iface.inetAddresses)
                for (addr in addresses) {
                    if (addr is Inet4Address && !addr.isLoopbackAddress) return addr.hostAddress
                }
            }
            null
        } catch (_: Exception) {
            null
        }
    }

    private fun buildBlePayload(ip: String, port: Int): ByteArray? {
        val parts = ip.split(".").map { it.toIntOrNull() ?: return null }
        if (parts.size != 4) return null
        return byteArrayOf(
            parts[0].toByte(), parts[1].toByte(), parts[2].toByte(), parts[3].toByte(),
            ((port shr 8) and 0xFF).toByte(), (port and 0xFF).toByte()
        )
    }

    // Advertises this device on mDNS/Bonjour as "_dromac._tcp" so the Mac
    // dashboard can find it by name instead of having to scan IP ranges —
    // works the same way on any network, home or campus, no assumptions.
    private fun registerNsd() {
        try {
            val info = NsdServiceInfo().apply {
                serviceName = "Dromac"
                serviceType = "_dromac._tcp"
                port = this@StationServerService.port
            }
            nsdManager = getSystemService(Context.NSD_SERVICE) as NsdManager
            nsdManager?.registerService(info, NsdManager.PROTOCOL_DNS_SD, nsdListener)
        } catch (_: Exception) {}
    }

    private fun unregisterNsd() {
        try {
            if (nsdRegistered) nsdManager?.unregisterService(nsdListener)
        } catch (_: Exception) {}
    }

    private fun buildForegroundNotification(): Notification {
        val channelId = "dromac_service"
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val channel = NotificationChannel(channelId, "Dromac Service", NotificationManager.IMPORTANCE_LOW)
            val nm = getSystemService(NotificationManager::class.java)
            nm.createNotificationChannel(channel)
        }
        return NotificationCompat.Builder(this, channelId)
            .setContentTitle("Dromac")
            .setContentText("Control server running on port $port")
            .setSmallIcon(R.drawable.ic_notification)
            .setOngoing(true)
            .build()
    }

    // Android 14+ requires startForeground() to explicitly say which of the manifest's
    // declared types are active RIGHT NOW when more than one is declared -- the plain
    // two-arg overload (which implicitly activates every declared type) apparently
    // doesn't tolerate "mediaProjection" being declared-but-unused at startup, and
    // throws immediately in onCreate(). This is what made the whole app crash on
    // every launch. So only "specialUse" is activated at startup; "mediaProjection"
    // gets added on top of it, dynamically, only once mirroring actually begins.
    private fun startForegroundNotification() {
        if (Build.VERSION.SDK_INT >= 34) {
            startForeground(1, buildForegroundNotification(), android.content.pm.ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE)
        } else {
            startForeground(1, buildForegroundNotification())
        }
    }

    private fun promoteToMediaProjectionType() = refreshForegroundServiceType()

    @Volatile private var cameraActive = false
    // Deliberately separate from `mediaProjection != null`: the platform's own
    // exception text gave it away -- "Media projections require a foreground
    // service of type ...MEDIA_PROJECTION" is thrown by
    // MediaProjectionManager.getMediaProjection() itself, BEFORE we ever get a
    // MediaProjection object back to check for null. Gating the type mask on
    // `mediaProjection != null` therefore always promoted one call too late,
    // no matter which line the promotion call was moved above. This flag is
    // set the instant we're ABOUT to ask for the projection, so the type is
    // already active for that very call.
    @Volatile private var mirrorActive = false

    // Recomputes which foreground-service types should be active right now (base
    // specialUse plus mediaProjection and/or camera, whichever are actually in
    // use) and re-asserts startForeground() with that combined mask. Android 14
    // requires every currently-active declared type to be passed on EVERY
    // startForeground() call, not just the one that just changed -- naively
    // demoting straight back to specialUse-only when one feature finishes would
    // silently drop another feature's still-active type (e.g. a camera capture
    // finishing while mirroring is still running).
    private fun refreshForegroundServiceType() {
        if (Build.VERSION.SDK_INT >= 34) {
            try {
                var types = android.content.pm.ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE
                if (mirrorActive) types = types or android.content.pm.ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PROJECTION
                if (cameraActive) types = types or android.content.pm.ServiceInfo.FOREGROUND_SERVICE_TYPE_CAMERA
                startForeground(1, buildForegroundNotification(), types)
            } catch (_: Exception) {}
        }
    }

    private fun startServer() {
        running = true
        serverThread = Thread {
            try {
                serverSocket = ServerSocket(port)
                while (running) {
                    val socket = serverSocket!!.accept()
                    Thread { handleClient(socket) }.start()
                }
            } catch (_: Exception) {
                // socket closed on stop, or transient error
            }
        }
        serverThread?.start()
    }

    private fun handleClient(socket: Socket) {
        try {
            socket.soTimeout = 5000
            val input = BufferedReader(InputStreamReader(socket.getInputStream()))
            val requestLine = input.readLine() ?: return
            val parts = requestLine.split(" ")
            if (parts.size < 2) return
            val method = parts[0]
            val path = parts[1]

            var contentLength = 0
            while (true) {
                val line = input.readLine() ?: break
                if (line.isEmpty()) break
                if (line.startsWith("Content-Length:", ignoreCase = true)) {
                    contentLength = line.substringAfter(":").trim().toIntOrNull() ?: 0
                }
            }

            var body = ""
            if (contentLength > 0) {
                val buf = CharArray(contentLength)
                var readTotal = 0
                while (readTotal < contentLength) {
                    val r = input.read(buf, readTotal, contentLength - readTotal)
                    if (r == -1) break
                    readTotal += r
                }
                body = String(buf, 0, readTotal)
            }

            route(method, path, body, socket.getOutputStream(), socket)
        } catch (_: Exception) {
            // malformed / dropped request, ignore
        } finally {
            try { socket.close() } catch (_: Exception) {}
        }
    }

    private fun route(method: String, path: String, body: String, out: OutputStream, socket: Socket) {
        lastRequestAt = System.currentTimeMillis()
        if (bleAdvertising) stopBleAdvertising() // Mac is actively talking to us now; no need to keep beaconing
        when {
            method == "GET" && path == "/api/state" -> writeJson(out, 200, buildStateJson().toString())
            method == "GET" && path == "/api/artwork" -> serveArtwork(out)
            method == "GET" && path == "/api/notifications" -> writeJson(out, 200, buildNotificationsJson().toString())
            method == "POST" && path == "/api/media/play_pause" -> { sendMediaKey(KeyEvent.KEYCODE_MEDIA_PLAY_PAUSE); writeJson(out, 200, "{\"ok\":true}") }
            method == "POST" && path == "/api/media/next" -> { sendMediaKey(KeyEvent.KEYCODE_MEDIA_NEXT); writeJson(out, 200, "{\"ok\":true}") }
            method == "POST" && path == "/api/media/prev" -> { sendMediaKey(KeyEvent.KEYCODE_MEDIA_PREVIOUS); writeJson(out, 200, "{\"ok\":true}") }
            method == "POST" && path == "/api/media/play_search" -> {
                val query = try { JSONObject(body).optString("query", "") } catch (_: Exception) { "" }
                if (query.isEmpty()) writeJson(out, 400, "{\"error\":\"query required\"}")
                else writeJson(out, 200, "{\"ok\":${playFromSearch(query)}}")
            }
            method == "POST" && path == "/api/media/seek" -> {
                val pos = try { JSONObject(body).optLong("positionMs", -1L) } catch (_: Exception) { -1L }
                writeJson(out, 200, "{\"ok\":${pos >= 0 && seekTo(pos)}}")
            }
            method == "POST" && path == "/api/volume/set" -> { setVolumeFromBody(body); writeJson(out, 200, "{\"ok\":true}") }
            method == "POST" && path == "/api/volume/up" -> { adjustVolume(AudioManager.ADJUST_RAISE); writeJson(out, 200, "{\"ok\":true}") }
            method == "POST" && path == "/api/volume/down" -> { adjustVolume(AudioManager.ADJUST_LOWER); writeJson(out, 200, "{\"ok\":true}") }
            method == "POST" && path == "/api/ring" -> { ringPhone(); writeJson(out, 200, "{\"ok\":true}") }
            method == "POST" && path == "/api/clipboard" -> { setClipboardFromBody(body); writeJson(out, 200, "{\"ok\":true}") }
            method == "POST" && path == "/api/flashlight/toggle" -> {
                val on = toggleFlashlight()
                writeJson(out, 200, "{\"ok\":true,\"on\":$on}")
            }
            method == "POST" && path == "/api/dnd/toggle" -> {
                val enabled = try { JSONObject(body).optBoolean("enabled", false) } catch (_: Exception) { false }
                val ok = setDnd(enabled)
                writeJson(out, 200, "{\"ok\":$ok}")
            }
            method == "POST" && path == "/api/notifications/clear_all" -> {
                writeJson(out, 200, "{\"ok\":${clearAllNotifications()}}")
            }
            method == "POST" && path == "/api/notifications/dismiss" -> {
                val key = try { JSONObject(body).optString("key", "") } catch (_: Exception) { "" }
                val ok = key.isNotEmpty() && dismissNotification(key)
                writeJson(out, 200, "{\"ok\":$ok}")
            }
            method == "POST" && path == "/api/notifications/action" -> {
                val json = try { JSONObject(body) } catch (_: Exception) { JSONObject() }
                val key = json.optString("key", "")
                val idx = json.optInt("actionIndex", -1)
                val text = if (json.has("text")) json.optString("text", "") else null
                val ok = key.isNotEmpty() && idx >= 0 && fireNotificationAction(key, idx, text)
                writeJson(out, 200, "{\"ok\":$ok}")
            }
            method == "POST" && path == "/api/call/answer" -> writeJson(out, 200, "{\"ok\":${answerCall()}}")
            method == "GET" && path == "/api/apps" -> writeJson(out, 200, listLaunchableApps().toString())
            method == "POST" && path == "/api/apps/launch" -> {
                val pkg = try { JSONObject(body).optString("package", "") } catch (_: Exception) { "" }
                writeJson(out, 200, "{\"ok\":${pkg.isNotEmpty() && launchApp(pkg)}}")
            }
            method == "POST" && path == "/api/screen/wake" -> { wakeScreen(); writeJson(out, 200, "{\"ok\":true}") }
            method == "POST" && path == "/api/screen/blackout/toggle" -> {
                val on = blackoutView == null
                val ok = setBlackout(on)
                val errJson = if (!ok && lastBlackoutError != null) JSONObject.quote(lastBlackoutError) else "null"
                writeJson(out, 200, "{\"ok\":$ok,\"on\":${ok && on},\"error\":$errJson}")
            }
            method == "POST" && path == "/api/screen/keep_awake/toggle" -> {
                writeJson(out, 200, "{\"ok\":true,\"on\":${toggleKeepAwake()}}")
            }
            method == "POST" && path == "/api/screen/lock" -> writeJson(out, 200, "{\"ok\":${lockScreen()}}")
            method == "GET" && path == "/api/location" -> writeJson(out, 200, fetchLocation().toString())
            method == "POST" && path == "/api/screen/mirror/start" -> {
                val ok = startMirroring()
                val errJson = if (!ok && lastMirrorError != null) JSONObject.quote(lastMirrorError) else "null"
                writeJson(out, 200, "{\"ok\":$ok,\"needsConsent\":${!ok && projectionResultData == null},\"error\":$errJson}")
            }
            method == "POST" && path == "/api/screen/mirror/stop" -> { stopMirroring(); writeJson(out, 200, "{\"ok\":true}") }
            method == "GET" && path == "/api/screen/stream" -> streamScreen(socket, out)
            method == "GET" && path == "/api/camera/pending" -> handleCameraPending(out)
            method == "POST" && path == "/api/camera/start" -> {
                val json = try { JSONObject(body) } catch (_: Exception) { JSONObject() }
                val ok = startCameraStream(json.optString("facing", "back"), json.optBoolean("flash", false))
                val errJson = if (!ok && lastCameraError != null) JSONObject.quote(lastCameraError) else "null"
                writeJson(out, 200, "{\"ok\":$ok,\"error\":$errJson}")
            }
            method == "GET" && path == "/api/camera/stream" -> streamCamera(socket, out)
            method == "POST" && path == "/api/camera/photo" -> {
                val bytes = captureCameraPhoto()
                if (bytes != null) writeBinary(out, 200, "image/jpeg", bytes)
                else writeJson(out, 200, "{\"ok\":false,\"error\":${JSONObject.quote(lastCameraError ?: "capture failed")}}")
            }
            method == "POST" && path == "/api/camera/flash" -> {
                val json = try { JSONObject(body) } catch (_: Exception) { JSONObject() }
                setCameraFlash(json.optBoolean("on", false))
                writeJson(out, 200, "{\"ok\":true}")
            }
            method == "POST" && path == "/api/camera/switch" -> {
                val json = try { JSONObject(body) } catch (_: Exception) { JSONObject() }
                val ok = switchCameraStream(json.optString("facing", "back"))
                val errJson = if (!ok && lastCameraError != null) JSONObject.quote(lastCameraError) else "null"
                writeJson(out, 200, "{\"ok\":$ok,\"error\":$errJson}")
            }
            method == "POST" && path == "/api/camera/stop" -> { stopCameraStream(); writeJson(out, 200, "{\"ok\":true}") }
            method == "POST" && path == "/api/screen/tap" -> {
                val json = try { JSONObject(body) } catch (_: Exception) { JSONObject() }
                val ok = dispatchTap(json.optDouble("x", -1.0), json.optDouble("y", -1.0))
                writeJson(out, 200, "{\"ok\":$ok}")
            }
            method == "POST" && path == "/api/screen/swipe" -> {
                val json = try { JSONObject(body) } catch (_: Exception) { JSONObject() }
                val ok = dispatchSwipe(
                    json.optDouble("x1", -1.0), json.optDouble("y1", -1.0),
                    json.optDouble("x2", -1.0), json.optDouble("y2", -1.0),
                    json.optLong("durationMs", 200L)
                )
                writeJson(out, 200, "{\"ok\":$ok}")
            }
            method == "POST" && path == "/api/screen/nav" -> {
                val json = try { JSONObject(body) } catch (_: Exception) { JSONObject() }
                val svc = DromacAccessibilityService.instance
                val reason = if (svc == null) "accessibility service not connected" else null
                val ok = if (svc == null) false else when (json.optString("action", "")) {
                    "back" -> svc.back()
                    "home" -> svc.home()
                    "recents" -> svc.recents()
                    else -> false
                }
                val errJson = if (!ok) JSONObject.quote(reason ?: "performGlobalAction returned false") else "null"
                writeJson(out, 200, "{\"ok\":$ok,\"error\":$errJson}")
            }
            method == "POST" && path == "/api/keyboard/text" -> {
                val json = try { JSONObject(body) } catch (_: Exception) { JSONObject() }
                val ime = DromacInputMethodService.instance
                val text = json.optString("text", "")
                val ok = ime != null && text.isNotEmpty() && ime.commitText(text)
                val errJson = if (!ok && ime == null) JSONObject.quote("Dromac isn't the active keyboard") else "null"
                writeJson(out, 200, "{\"ok\":$ok,\"error\":$errJson}")
            }
            method == "POST" && path == "/api/keyboard/key" -> {
                val json = try { JSONObject(body) } catch (_: Exception) { JSONObject() }
                val ime = DromacInputMethodService.instance
                val ok = if (ime == null) false else when (json.optString("key", "")) {
                    "backspace" -> ime.backspace()
                    "enter" -> ime.enter()
                    else -> false
                }
                val errJson = if (!ok && ime == null) JSONObject.quote("Dromac isn't the active keyboard") else "null"
                writeJson(out, 200, "{\"ok\":$ok,\"error\":$errJson}")
            }
            else -> writeJson(out, 404, "{\"error\":\"not found\"}")
        }
    }

    // ---------- media ----------

    private fun activeController(): MediaController? {
        return try {
            val msm = getSystemService(Context.MEDIA_SESSION_SERVICE) as MediaSessionManager
            val comp = ComponentName(this, NotificationAccessService::class.java)
            val sessions = msm.getActiveSessions(comp)
            if (sessions.isEmpty()) null
            else sessions.firstOrNull { it.playbackState?.state == PlaybackState.STATE_PLAYING } ?: sessions.first()
        } catch (_: SecurityException) {
            null
        } catch (_: Exception) {
            null
        }
    }

    private fun sendMediaKey(keyCode: Int) {
        val controller = activeController() ?: return
        try {
            controller.dispatchMediaButtonEvent(KeyEvent(KeyEvent.ACTION_DOWN, keyCode))
            controller.dispatchMediaButtonEvent(KeyEvent(KeyEvent.ACTION_UP, keyCode))
        } catch (_: Exception) {}
    }

    private fun seekTo(positionMs: Long): Boolean {
        val controller = activeController() ?: return false
        return try {
            controller.transportControls.seekTo(positionMs)
            true
        } catch (_: Exception) {
            false
        }
    }

    private fun playFromSearch(query: String): Boolean {
        val controller = activeController()
        val activePackage = controller?.packageName
        if (controller != null) {
            try {
                controller.transportControls.playFromSearch(query, null)
            } catch (_: Exception) {}
        }
        // Most apps (YouTube Music, Spotify, patched builds alike) don't actually
        // wire onPlayFromSearch up to real playback even though the call above
        // succeeds silently. Fall back to deep-linking into whichever app is
        // actually active so it's one tap away instead of nothing happening.
        return openSearchDeepLink(query, activePackage)
    }

    private fun tryIntent(intent: Intent): Boolean {
        return try {
            if (intent.resolveActivity(packageManager) != null) {
                startActivityFromBackground(intent)
                true
            } else {
                false
            }
        } catch (_: Exception) {
            false
        }
    }

    // A background service's startActivity() is only reliably honored by Android
    // if the app currently has something visible on screen -- true regardless of
    // whether the phone is locked or not (a full-screen-intent notification, tried
    // first, turned out to only auto-launch while the device is LOCKED; on an
    // unlocked phone it just sits as a tappable banner, which looked like "nothing
    // happened"). Briefly showing an invisible 1x1 overlay window satisfies that
    // "has a visible window" exemption in both cases, then the real launch
    // actually goes through. Needs "Display over other apps" granted once.
    private fun startActivityFromBackground(intent: Intent) {
        val canOverlay = try { android.provider.Settings.canDrawOverlays(this) } catch (_: Exception) { false }
        if (!canOverlay) {
            try { startActivity(intent) } catch (_: Exception) {}
            return
        }
        var overlay: android.view.View? = null
        var wm: android.view.WindowManager? = null
        try {
            wm = getSystemService(Context.WINDOW_SERVICE) as android.view.WindowManager
            overlay = android.view.View(this)
            val type = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O)
                android.view.WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY
            else
                @Suppress("DEPRECATION") android.view.WindowManager.LayoutParams.TYPE_PHONE
            val params = android.view.WindowManager.LayoutParams(
                1, 1, type,
                android.view.WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE or
                    android.view.WindowManager.LayoutParams.FLAG_NOT_TOUCHABLE or
                    android.view.WindowManager.LayoutParams.FLAG_LAYOUT_NO_LIMITS,
                android.graphics.PixelFormat.TRANSLUCENT
            )
            wm.addView(overlay, params)
        } catch (_: Exception) {
            overlay = null
        }
        try {
            startActivity(intent)
        } catch (_: Exception) {}
        if (overlay != null) {
            val finalOverlay = overlay
            val finalWm = wm
            Handler(Looper.getMainLooper()).postDelayed({
                try { finalWm?.removeView(finalOverlay) } catch (_: Exception) {}
            }, 800)
        }
    }

    private fun openSearchDeepLink(query: String, activePackage: String?): Boolean {
        val encoded = Uri.encode(query)

        fun spotify(pkg: String?) = Intent(Intent.ACTION_VIEW, Uri.parse("spotify:search:$encoded")).apply {
            pkg?.let { setPackage(it) }
            addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        }
        fun ytMusic(pkg: String?) = Intent(Intent.ACTION_VIEW, Uri.parse("https://music.youtube.com/search?q=$encoded")).apply {
            pkg?.let { setPackage(it) }
            addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        }

        val candidates = mutableListOf<Intent>()
        when {
            activePackage == null -> {}
            activePackage.contains("spotify", ignoreCase = true) -> candidates.add(spotify(activePackage))
            activePackage.contains("youtube.music", ignoreCase = true) -> candidates.add(ytMusic(activePackage))
        }
        // Generic fallbacks in case nothing was active or the package didn't match a known pattern.
        candidates.add(spotify(null))
        candidates.add(ytMusic("app.rvx.android.apps.youtube.music"))
        candidates.add(ytMusic(null))

        for (intent in candidates) {
            if (tryIntent(intent)) return true
        }
        return false
    }

    // Returns true/false if the app reports a like/heart/thumbs rating for the
    // current track via the standard MediaSession Rating API, or null if the
    // app doesn't populate it (in which case we genuinely don't know, and
    // should say so rather than guess).
    private fun readLikedState(metadata: MediaMetadata?): Boolean? {
        val rating = try {
            metadata?.getRating(MediaMetadata.METADATA_KEY_USER_RATING)
        } catch (_: Exception) {
            null
        } ?: return null
        if (!rating.isRated) return null
        return when (rating.ratingStyle) {
            Rating.RATING_THUMB_UP_DOWN -> rating.isThumbUp
            Rating.RATING_HEART -> rating.hasHeart()
            else -> null
        }
    }

    private fun findMediaNotification(packageName: String?): StatusBarNotification? {
        if (packageName == null) return null
        val svc = NotificationAccessService.instance ?: return null
        return try {
            svc.activeNotifications.firstOrNull {
                it.packageName == packageName && !it.notification.actions.isNullOrEmpty()
            }
        } catch (_: Exception) {
            null
        }
    }

    // Finds Like/Dislike-style actions on the current app's media notification by
    // title keyword, so the dashboard can offer dedicated buttons that fire the
    // exact same PendingIntents as the notification's own action buttons.
    private fun extractRatingActions(sbn: StatusBarNotification?): Pair<JSONObject?, JSONObject?> {
        val actions = sbn?.notification?.actions ?: return Pair(null, null)
        var like: JSONObject? = null
        var dislike: JSONObject? = null
        actions.forEachIndexed { idx, action ->
            val lower = (action.title?.toString() ?: "").lowercase()
            when {
                lower.contains("dislike") || lower.contains("thumbs down") -> {
                    dislike = JSONObject().apply { put("key", sbn.key); put("index", idx) }
                }
                lower.contains("like") || lower.contains("thumbs up") || lower.contains("heart") -> {
                    like = JSONObject().apply { put("key", sbn.key); put("index", idx) }
                }
            }
        }
        return Pair(like, dislike)
    }

    private fun buildStateJson(): JSONObject {
        val root = JSONObject()
        val controller = activeController()
        if (controller != null) {
            val metadata = controller.metadata
            val playbackState = controller.playbackState
            val np = JSONObject()
            np.put("title", metadata?.getString(MediaMetadata.METADATA_KEY_TITLE) ?: "")
            np.put("artist", metadata?.getString(MediaMetadata.METADATA_KEY_ARTIST) ?: "")
            np.put("playing", playbackState?.state == PlaybackState.STATE_PLAYING)
            // PlaybackState.position is a snapshot as of lastPositionUpdateTime, not
            // "right now" -- some apps only refresh their MediaSession state
            // occasionally, so using it raw goes progressively stale between those
            // updates (exactly the "lags depending on the song" symptom). Extrapolating
            // by elapsed time * playback speed, per Android's own documented recipe for
            // reading PlaybackState, keeps it accurate regardless of how often the
            // source app itself updates.
            val rawPosition = playbackState?.position ?: 0L
            val extrapolatedPosition = if (playbackState?.state == PlaybackState.STATE_PLAYING) {
                val lastUpdate = playbackState.lastPositionUpdateTime
                val elapsed = if (lastUpdate > 0) android.os.SystemClock.elapsedRealtime() - lastUpdate else 0L
                rawPosition + (elapsed * playbackState.playbackSpeed).toLong()
            } else rawPosition
            np.put("position", extrapolatedPosition.coerceAtLeast(0L))
            val duration = metadata?.getLong(MediaMetadata.METADATA_KEY_DURATION) ?: -1L
            np.put("duration", duration)
            val hasArt = metadata?.getBitmap(MediaMetadata.METADATA_KEY_ALBUM_ART) != null ||
                metadata?.getBitmap(MediaMetadata.METADATA_KEY_ART) != null
            np.put("hasArt", hasArt)
            when (val liked = readLikedState(metadata)) {
                null -> np.put("liked", JSONObject.NULL)
                else -> np.put("liked", liked)
            }
            val sbn = findMediaNotification(controller.packageName)
            val (likeAction, dislikeAction) = extractRatingActions(sbn)
            np.put("likeAction", likeAction ?: JSONObject.NULL)
            np.put("dislikeAction", dislikeAction ?: JSONObject.NULL)
            root.put("now_playing", np)
        } else {
            root.put("now_playing", JSONObject.NULL)
        }

        try {
            val audioManager = getSystemService(Context.AUDIO_SERVICE) as AudioManager
            val index = audioManager.getStreamVolume(AudioManager.STREAM_MUSIC)
            if (lastKnownVolumeIndex != null && lastKnownVolumeIndex != index) {
                // Index moved without going through setFineVolume (hardware buttons, another
                // app, a wired headset remote) -- our interpolation boost no longer applies
                // to this new step, so drop it instead of silently over-amplifying.
                setBoost(0)
            }
            lastKnownVolumeIndex = index
            val vol = JSONObject()
            vol.put("level", index + (if (currentStepRangeMb > 0) currentBoostMb.toDouble() / currentStepRangeMb else 0.0))
            vol.put("max", audioManager.getStreamMaxVolume(AudioManager.STREAM_MUSIC))
            root.put("volume", vol)
        } catch (_: Exception) {}

        try {
            val bm = getSystemService(Context.BATTERY_SERVICE) as BatteryManager
            root.put("battery", bm.getIntProperty(BatteryManager.BATTERY_PROPERTY_CAPACITY))
        } catch (_: Exception) {}

        try {
            val nm = getSystemService(NotificationManager::class.java)
            root.put("dnd", nm.currentInterruptionFilter != NotificationManager.INTERRUPTION_FILTER_ALL)
            root.put("dndAvailable", nm.isNotificationPolicyAccessGranted)
        } catch (_: Exception) {
            root.put("dnd", false)
            root.put("dndAvailable", false)
        }
        root.put("flashlight", torchOn)
        root.put("keepAwake", keepAwakeWakeLock?.isHeld == true)
        try {
            val km = getSystemService(Context.KEYGUARD_SERVICE) as android.app.KeyguardManager
            root.put("locked", km.isKeyguardLocked)
        } catch (_: Exception) {
            root.put("locked", false)
        }
        root.put("blackout", blackoutView != null)
        root.put("overlayGranted", try { android.provider.Settings.canDrawOverlays(this) } catch (_: Exception) { false })
        root.put("keyboardActive", DromacInputMethodService.instance != null)
        root.put("mirrorGranted", projectionResultData != null)
        root.put("mirrorActive", mediaProjection != null)
        root.put("accessibilityAvailable", DromacAccessibilityService.instance != null)
        root.put("cameraGranted", androidx.core.content.ContextCompat.checkSelfPermission(this, android.Manifest.permission.CAMERA) == android.content.pm.PackageManager.PERMISSION_GRANTED)

        val dpm = getSystemService(Context.DEVICE_POLICY_SERVICE) as? DevicePolicyManager
        val adminComponent = ComponentName(this, DromacDeviceAdminReceiver::class.java)
        root.put("lockAvailable", dpm?.isAdminActive(adminComponent) == true)

        if (callRinging && incomingCallNumber != null) {
            val call = JSONObject()
            call.put("number", incomingCallNumber)
            root.put("incomingCall", call)
        } else {
            root.put("incomingCall", JSONObject.NULL)
        }

        // OTPs expire fast in practice; stop surfacing one after 2 minutes so a stale
        // code doesn't linger on the dashboard.
        if (latestOtp != null && System.currentTimeMillis() - latestOtpAt < 120_000) {
            root.put("otp", latestOtp)
        } else {
            root.put("otp", JSONObject.NULL)
        }

        root.put("nextEvent", getNextCalendarEvent() ?: JSONObject.NULL)
        root.put("screenTimeTodayMin", getUsageTodayMinutes())

        root.put("device_model", "${Build.MANUFACTURER} ${Build.MODEL}")
        root.put("connected", true)
        return root
    }

    private fun serveArtwork(out: OutputStream) {
        val metadata = activeController()?.metadata
        val bitmap: Bitmap? = metadata?.getBitmap(MediaMetadata.METADATA_KEY_ALBUM_ART)
            ?: metadata?.getBitmap(MediaMetadata.METADATA_KEY_ART)
        if (bitmap == null) {
            writeJson(out, 404, "{\"error\":\"no artwork\"}")
            return
        }
        val stream = ByteArrayOutputStream()
        bitmap.compress(Bitmap.CompressFormat.JPEG, 85, stream)
        writeBinary(out, 200, "image/jpeg", stream.toByteArray())
    }

    // ---------- camera ----------
    // Photos taken with the phone's own dedicated camera screen (CameraActivity)
    // land here in a small FIFO mailbox instead of being written to disk on the
    // phone -- the Mac drains it (GET /api/camera/pending, repeatedly, until
    // empty) and saves each one straight into Documents/Dromac. Bounded so a
    // burst of photos taken while the Mac happens to be unreachable doesn't
    // grow unbounded, but a temporary disconnect still doesn't silently lose a
    // shot the way a single "latest photo only" slot would.
    private val pendingCaptureLock = Object()
    private val pendingCaptures = ArrayDeque<ByteArray>()
    private val PENDING_CAPTURE_MAX = 8

    fun enqueuePendingCapture(bytes: ByteArray) {
        synchronized(pendingCaptureLock) {
            pendingCaptures.addLast(bytes)
            while (pendingCaptures.size > PENDING_CAPTURE_MAX) pendingCaptures.removeFirst()
        }
    }

    private fun dequeuePendingCapture(): ByteArray? = synchronized(pendingCaptureLock) {
        if (pendingCaptures.isEmpty()) null else pendingCaptures.removeFirst()
    }

    private fun handleCameraPending(out: OutputStream) {
        val bytes = dequeuePendingCapture()
        if (bytes != null) writeBinary(out, 200, "image/jpeg", bytes)
        else writeJson(out, 404, "{\"error\":\"no pending capture\"}")
    }

    // ---------- Mac-triggered live camera stream ----------
    // A real live preview for the Mac's own camera button, not a blind shutter --
    // opens the camera once and keeps it open (repeating preview request into a
    // small YUV reader, JPEG-encoded per frame and streamed the same MJPEG way
    // as screen mirroring above) so "start" is a live feed, and "photo" is a
    // still capture layered on TOP of that already-running, already-focused
    // session -- much faster than the old open-warmup-capture-close cycle,
    // since 3A has already converged from the live preview by the time the
    // user taps capture.
    private var camDevice: CameraDevice? = null
    private var camSession: CameraCaptureSession? = null
    private var camPreviewReader: ImageReader? = null
    private var camStillReader: ImageReader? = null
    private var camBgThread: HandlerThread? = null
    private var camBgHandler: Handler? = null
    private var camCharacteristics: android.hardware.camera2.CameraCharacteristics? = null
    private var camSensorOrientation = 90
    @Volatile private var camFacing = android.hardware.camera2.CameraCharacteristics.LENS_FACING_BACK
    @Volatile private var camFlashOn = false
    @Volatile var lastCameraError: String? = null
    @Volatile private var camLatestFrameJpeg: ByteArray? = null
    private val camFrameLock = Object()
    private var camStillLatch: java.util.concurrent.CountDownLatch? = null
    @Volatile private var camStillBytes: ByteArray? = null

    fun isCameraStreamActive(): Boolean = camDevice != null

    private fun startCameraStream(facing: String, flash: Boolean): Boolean {
        if (camDevice != null) {
            camFlashOn = flash
            updateCameraRepeatingRequest()
            return true
        }
        lastCameraError = null
        camFacing = if (facing == "front") android.hardware.camera2.CameraCharacteristics.LENS_FACING_FRONT else android.hardware.camera2.CameraCharacteristics.LENS_FACING_BACK
        camFlashOn = flash
        return try {
            val manager = getSystemService(Context.CAMERA_SERVICE) as android.hardware.camera2.CameraManager
            val id = manager.cameraIdList.firstOrNull { cid ->
                manager.getCameraCharacteristics(cid).get(android.hardware.camera2.CameraCharacteristics.LENS_FACING) == camFacing
            } ?: manager.cameraIdList.firstOrNull()
            if (id == null) { lastCameraError = "no camera found"; return false }
            val characteristics = manager.getCameraCharacteristics(id)
            camCharacteristics = characteristics
            camSensorOrientation = characteristics.get(android.hardware.camera2.CameraCharacteristics.SENSOR_ORIENTATION) ?: 90
            val map = characteristics.get(android.hardware.camera2.CameraCharacteristics.SCALER_STREAM_CONFIGURATION_MAP)

            val previewSizes = map?.getOutputSizes(android.graphics.ImageFormat.YUV_420_888)?.toList() ?: emptyList()
            val previewSize = previewSizes.filter { it.width in 1..959 }.maxByOrNull { it.width.toLong() * it.height }
                ?: previewSizes.minByOrNull { it.width.toLong() * it.height } ?: android.util.Size(640, 480)
            val stillSize = map?.getOutputSizes(android.graphics.ImageFormat.JPEG)?.toList()?.maxByOrNull { it.width.toLong() * it.height }
                ?: android.util.Size(1920, 1080)

            val bgThread = HandlerThread("DromacCameraStream").also { it.start() }
            camBgThread = bgThread
            val bgHandler = Handler(bgThread.looper)
            camBgHandler = bgHandler

            val previewReader = android.media.ImageReader.newInstance(previewSize.width, previewSize.height, android.graphics.ImageFormat.YUV_420_888, 2)
            previewReader.setOnImageAvailableListener({ r ->
                var image: Image? = null
                try {
                    image = r.acquireLatestImage()
                    val img = image
                    if (img != null) {
                        val jpeg = yuv420ToJpeg(img, 60)
                        if (jpeg != null) synchronized(camFrameLock) { camLatestFrameJpeg = jpeg }
                    }
                } catch (_: Exception) {
                } finally {
                    try { image?.close() } catch (_: Exception) {}
                }
            }, bgHandler)

            val stillReader = android.media.ImageReader.newInstance(stillSize.width, stillSize.height, android.graphics.ImageFormat.JPEG, 1)
            stillReader.setOnImageAvailableListener({ r ->
                var image: Image? = null
                try {
                    image = r.acquireLatestImage()
                    val img = image
                    if (img != null) {
                        val buffer = img.planes[0].buffer
                        val bytes = ByteArray(buffer.remaining())
                        buffer.get(bytes)
                        camStillBytes = bytes
                    }
                } catch (_: Exception) {
                } finally {
                    try { image?.close() } catch (_: Exception) {}
                    camStillLatch?.countDown()
                }
            }, bgHandler)

            cameraActive = true
            refreshForegroundServiceType()

            val openLatch = java.util.concurrent.CountDownLatch(1)
            var openError: String? = null
            manager.openCamera(id, object : CameraDevice.StateCallback() {
                override fun onOpened(device: CameraDevice) { camDevice = device; openLatch.countDown() }
                override fun onDisconnected(device: CameraDevice) {
                    try { device.close() } catch (_: Exception) {}
                    if (camDevice === device) camDevice = null
                    openError = "camera disconnected"; openLatch.countDown()
                }
                override fun onError(device: CameraDevice, error: Int) {
                    try { device.close() } catch (_: Exception) {}
                    if (camDevice === device) camDevice = null
                    openError = "camera error $error"; openLatch.countDown()
                }
            }, bgHandler)

            if (!openLatch.await(6, java.util.concurrent.TimeUnit.SECONDS) || camDevice == null) {
                lastCameraError = openError ?: "camera open timed out"
                stopCameraStream()
                return false
            }
            val device = camDevice!!

            val sessionLatch = java.util.concurrent.CountDownLatch(1)
            var sessionFailed = false
            device.createCaptureSession(
                listOf(previewReader.surface, stillReader.surface),
                object : CameraCaptureSession.StateCallback() {
                    override fun onConfigured(session: CameraCaptureSession) { camSession = session; sessionLatch.countDown() }
                    override fun onConfigureFailed(session: CameraCaptureSession) { sessionFailed = true; sessionLatch.countDown() }
                }, bgHandler
            )
            if (!sessionLatch.await(6, java.util.concurrent.TimeUnit.SECONDS) || sessionFailed || camSession == null) {
                lastCameraError = "camera session configuration failed"
                stopCameraStream()
                return false
            }

            camPreviewReader = previewReader
            camStillReader = stillReader
            updateCameraRepeatingRequest()
            true
        } catch (e: SecurityException) {
            lastCameraError = "camera permission not granted"
            stopCameraStream()
            false
        } catch (e: Exception) {
            lastCameraError = "${e.javaClass.simpleName}: ${e.message}"
            stopCameraStream()
            false
        }
    }

    private fun updateCameraRepeatingRequest() {
        val device = camDevice ?: return
        val session = camSession ?: return
        val reader = camPreviewReader ?: return
        try {
            val hasFlash = camCharacteristics?.get(android.hardware.camera2.CameraCharacteristics.FLASH_INFO_AVAILABLE) == true
            val request = device.createCaptureRequest(CameraDevice.TEMPLATE_PREVIEW).apply {
                addTarget(reader.surface)
                set(CaptureRequest.CONTROL_MODE, CaptureRequest.CONTROL_MODE_AUTO)
                set(CaptureRequest.CONTROL_AF_MODE, CaptureRequest.CONTROL_AF_MODE_CONTINUOUS_PICTURE)
                set(CaptureRequest.CONTROL_AE_MODE, if (camFlashOn && hasFlash) CaptureRequest.CONTROL_AE_MODE_ON_ALWAYS_FLASH else CaptureRequest.CONTROL_AE_MODE_ON)
            }.build()
            session.setRepeatingRequest(request, null, camBgHandler)
        } catch (_: Exception) {}
    }

    fun setCameraFlash(on: Boolean) {
        camFlashOn = on
        updateCameraRepeatingRequest()
    }

    fun switchCameraStream(facing: String): Boolean {
        val flash = camFlashOn
        stopCameraStream()
        return startCameraStream(facing, flash)
    }

    fun captureCameraPhoto(): ByteArray? {
        val device = camDevice ?: return null
        val session = camSession ?: return null
        val reader = camStillReader ?: return null
        val latch = java.util.concurrent.CountDownLatch(1)
        camStillBytes = null
        camStillLatch = latch
        try {
            val hasFlash = camCharacteristics?.get(android.hardware.camera2.CameraCharacteristics.FLASH_INFO_AVAILABLE) == true
            val request = device.createCaptureRequest(CameraDevice.TEMPLATE_STILL_CAPTURE).apply {
                addTarget(reader.surface)
                set(CaptureRequest.CONTROL_MODE, CaptureRequest.CONTROL_MODE_AUTO)
                set(CaptureRequest.CONTROL_AF_MODE, CaptureRequest.CONTROL_AF_MODE_CONTINUOUS_PICTURE)
                set(CaptureRequest.CONTROL_AE_MODE, if (camFlashOn && hasFlash) CaptureRequest.CONTROL_AE_MODE_ON_ALWAYS_FLASH else CaptureRequest.CONTROL_AE_MODE_ON)
                set(CaptureRequest.JPEG_ORIENTATION, camSensorOrientation)
            }.build()
            session.capture(request, null, camBgHandler)
        } catch (e: Exception) {
            lastCameraError = e.message ?: "still capture failed"
            camStillLatch = null
            return null
        }
        try { latch.await(8, java.util.concurrent.TimeUnit.SECONDS) } catch (_: Exception) {}
        camStillLatch = null
        if (camStillBytes == null) lastCameraError = "capture timed out"
        return camStillBytes
    }

    private fun stopCameraStream() {
        try { camSession?.close() } catch (_: Exception) {}
        try { camDevice?.close() } catch (_: Exception) {}
        try { camPreviewReader?.close() } catch (_: Exception) {}
        try { camStillReader?.close() } catch (_: Exception) {}
        try { camBgThread?.quitSafely() } catch (_: Exception) {}
        camSession = null
        camDevice = null
        camPreviewReader = null
        camStillReader = null
        camBgThread = null
        camBgHandler = null
        camCharacteristics = null
        synchronized(camFrameLock) { camLatestFrameJpeg = null }
        cameraActive = false
        refreshForegroundServiceType()
    }

    // Same NV21-repacking approach as every other minimal Camera2 sample: the
    // three YUV_420_888 planes aren't guaranteed to already be packed the way
    // NV21 needs (rowStride/pixelStride vary by device), so this copies row by
    // row instead of assuming the buffers are contiguous.
    private fun yuv420ToJpeg(image: Image, quality: Int): ByteArray? {
        return try {
            val width = image.width
            val height = image.height
            val yPlane = image.planes[0]
            val uPlane = image.planes[1]
            val vPlane = image.planes[2]
            val nv21 = ByteArray(width * height + width * height / 2)

            var pos = 0
            val yBuffer = yPlane.buffer
            val yRowStride = yPlane.rowStride
            for (row in 0 until height) {
                yBuffer.position(row * yRowStride)
                yBuffer.get(nv21, pos, width)
                pos += width
            }

            val uBuffer = uPlane.buffer
            val vBuffer = vPlane.buffer
            val uRowStride = uPlane.rowStride
            val uPixelStride = uPlane.pixelStride
            val vRowStride = vPlane.rowStride
            val vPixelStride = vPlane.pixelStride
            val chromaHeight = height / 2
            val chromaWidth = width / 2
            for (row in 0 until chromaHeight) {
                val vRowStart = row * vRowStride
                val uRowStart = row * uRowStride
                for (col in 0 until chromaWidth) {
                    nv21[pos++] = vBuffer.get(vRowStart + col * vPixelStride)
                    nv21[pos++] = uBuffer.get(uRowStart + col * uPixelStride)
                }
            }

            val yuvImage = android.graphics.YuvImage(nv21, android.graphics.ImageFormat.NV21, width, height, null)
            val stream = ByteArrayOutputStream()
            yuvImage.compressToJpeg(android.graphics.Rect(0, 0, width, height), quality, stream)
            val rawJpeg = stream.toByteArray()
            // YuvImage's JPEG carries no orientation of its own -- the sensor's
            // native (landscape) buffer needs the same rotation the still
            // capture applies via JPEG_ORIENTATION, or the live preview shows
            // sideways/upside-down relative to how the phone is actually held.
            if (camSensorOrientation % 360 == 0) return rawJpeg
            val bitmap = android.graphics.BitmapFactory.decodeByteArray(rawJpeg, 0, rawJpeg.size) ?: return rawJpeg
            val matrix = android.graphics.Matrix().apply { postRotate(camSensorOrientation.toFloat()) }
            val rotated = Bitmap.createBitmap(bitmap, 0, 0, bitmap.width, bitmap.height, matrix, true)
            val rotatedStream = ByteArrayOutputStream()
            rotated.compress(Bitmap.CompressFormat.JPEG, quality, rotatedStream)
            if (rotated !== bitmap) bitmap.recycle()
            rotated.recycle()
            rotatedStream.toByteArray()
        } catch (_: Exception) {
            null
        }
    }

    // Streams MJPEG the same way streamScreen() does above -- one open HTTP
    // connection, one boundary-delimited JPEG per frame, for as long as the
    // client stays connected and the camera stream is still active.
    private fun streamCamera(socket: Socket, out: OutputStream) {
        val boundary = "dromaccameraframe"
        try {
            val header = "HTTP/1.1 200 OK\r\nContent-Type: multipart/x-mixed-replace; boundary=$boundary\r\nCache-Control: no-store\r\nConnection: close\r\n\r\n"
            out.write(header.toByteArray(Charsets.UTF_8))
            out.flush()
            var lastSent: ByteArray? = null
            while (running && camDevice != null && !socket.isClosed) {
                val frame = synchronized(camFrameLock) { camLatestFrameJpeg }
                if (frame != null && frame !== lastSent) {
                    lastSent = frame
                    val part = "--$boundary\r\nContent-Type: image/jpeg\r\nContent-Length: ${frame.size}\r\n\r\n"
                    out.write(part.toByteArray(Charsets.UTF_8))
                    out.write(frame)
                    out.write("\r\n".toByteArray(Charsets.UTF_8))
                    out.flush()
                }
                Thread.sleep(120)
            }
        } catch (_: Exception) {}
    }

    // ---------- volume ----------
    //
    // Android's real STREAM_MUSIC range is often tiny (as few as ~15-16 steps on
    // many phones), so dragging the dashboard slider would otherwise make the
    // actual audible volume jump in big, coarse chunks no matter how smoothly the
    // UI itself moves. To get genuinely finer control on top of that, a
    // LoudnessEnhancer is attached to the GLOBAL output mix (audio session 0 --
    // the same technique standalone "volume booster" apps use, and it applies
    // regardless of which app is currently playing). Volume is set to the floor
    // of the two nearest native steps, and the fractional remainder is filled in
    // with a small boost, so the slider's fine positions are actually audible
    // instead of just visually smooth.

    private var loudnessEnhancer: android.media.audiofx.LoudnessEnhancer? = null
    @Volatile private var currentBoostMb = 0
    @Volatile private var lastKnownVolumeIndex: Int? = null
    // The boost needed to bridge one native step is NOT a fixed dB value -- Android's
    // volume curve is non-linear, so a guessed constant mismatches the real per-step
    // gain and produces an audible jump/dip at every single step boundary. This tracks
    // the ACTUAL measured gap (see stepGainMb) for whichever step we're currently
    // interpolating from, so both the applied boost and the reported fine level (in
    // buildStateJson) stay consistent with reality instead of a guess.
    @Volatile private var currentStepRangeMb = 500
    private val FALLBACK_STEP_RANGE_MB = 500 // only used pre-API 28, where the real value can't be queried

    private fun ensureLoudnessEnhancer(): android.media.audiofx.LoudnessEnhancer? {
        loudnessEnhancer?.let { return it }
        return try {
            val le = android.media.audiofx.LoudnessEnhancer(0)
            loudnessEnhancer = le
            le
        } catch (_: Exception) {
            null
        }
    }

    private fun setBoost(gainMb: Int) {
        currentBoostMb = gainMb
        try {
            val le = ensureLoudnessEnhancer() ?: return
            le.setTargetGain(gainMb)
            le.enabled = gainMb > 0
        } catch (_: Exception) {}
    }

    // Real dB gap between two adjacent native volume steps, via the public
    // getStreamVolumeDb API (Android 9+). Falls back to a guessed constant on
    // older devices where that API doesn't exist.
    private fun stepGainMb(audioManager: AudioManager, floorIndex: Int, max: Int): Int {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.P || floorIndex >= max) return FALLBACK_STEP_RANGE_MB
        return try {
            val deviceType = android.media.AudioDeviceInfo.TYPE_BUILTIN_SPEAKER
            val dbFloor = audioManager.getStreamVolumeDb(AudioManager.STREAM_MUSIC, floorIndex, deviceType)
            val dbCeil = audioManager.getStreamVolumeDb(AudioManager.STREAM_MUSIC, floorIndex + 1, deviceType)
            ((dbCeil - dbFloor) * 100).toInt().coerceIn(0, 2000) // dB -> millibels
        } catch (_: Exception) {
            FALLBACK_STEP_RANGE_MB
        }
    }

    private fun adjustVolume(direction: Int) {
        try {
            val audioManager = getSystemService(Context.AUDIO_SERVICE) as AudioManager
            audioManager.adjustStreamVolume(AudioManager.STREAM_MUSIC, direction, 0)
            setBoost(0)
            lastKnownVolumeIndex = audioManager.getStreamVolume(AudioManager.STREAM_MUSIC)
        } catch (_: Exception) {}
    }

    // Accepts a fractional device-index level (e.g. 4.35 = 35% of the way from
    // native step 4 to step 5) and realizes it as [floor step] + [interpolating
    // boost sized to the REAL measured gap], instead of only the integer level
    // the old API took.
    private fun setFineVolume(fine: Double) {
        try {
            val audioManager = getSystemService(Context.AUDIO_SERVICE) as AudioManager
            val max = audioManager.getStreamMaxVolume(AudioManager.STREAM_MUSIC)
            val clamped = fine.coerceIn(0.0, max.toDouble())
            val floorIndex = clamped.toInt()
            val frac = clamped - floorIndex
            audioManager.setStreamVolume(AudioManager.STREAM_MUSIC, floorIndex, 0)
            lastKnownVolumeIndex = floorIndex
            currentStepRangeMb = stepGainMb(audioManager, floorIndex, max)
            setBoost((frac * currentStepRangeMb).toInt())
        } catch (_: Exception) {}
    }

    private fun setVolumeFromBody(body: String) {
        try {
            val json = JSONObject(body)
            val fine = json.optDouble("level", Double.NaN)
            if (!fine.isNaN()) {
                setFineVolume(fine)
                return
            }
            val level = json.optInt("level", -1)
            if (level >= 0) {
                val audioManager = getSystemService(Context.AUDIO_SERVICE) as AudioManager
                audioManager.setStreamVolume(AudioManager.STREAM_MUSIC, level, 0)
                setBoost(0)
                lastKnownVolumeIndex = level
            }
        } catch (_: Exception) {}
    }

    // ---------- ring / clipboard ----------

    private fun ringPhone() {
        try {
            val vibrator: Vibrator = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
                (getSystemService(Context.VIBRATOR_MANAGER_SERVICE) as VibratorManager).defaultVibrator
            } else {
                @Suppress("DEPRECATION")
                getSystemService(Context.VIBRATOR_SERVICE) as Vibrator
            }
            val pattern = longArrayOf(0, 500, 200, 500, 200, 500)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                vibrator.vibrate(VibrationEffect.createWaveform(pattern, -1))
            } else {
                @Suppress("DEPRECATION")
                vibrator.vibrate(pattern, -1)
            }

            val audioManager = getSystemService(Context.AUDIO_SERVICE) as AudioManager
            val maxVol = audioManager.getStreamMaxVolume(AudioManager.STREAM_ALARM)
            audioManager.setStreamVolume(AudioManager.STREAM_ALARM, maxVol, 0)

            val uri: Uri = RingtoneManager.getDefaultUri(RingtoneManager.TYPE_ALARM)
            val ringtone = RingtoneManager.getRingtone(applicationContext, uri)
            ringtone.audioAttributes = AudioAttributes.Builder()
                .setUsage(AudioAttributes.USAGE_ALARM)
                .build()
            ringtone.play()
            Handler(Looper.getMainLooper()).postDelayed({ try { ringtone.stop() } catch (_: Exception) {} }, 8000)
        } catch (_: Exception) {}
    }

    // ---------- flashlight ----------

    @Volatile private var torchOn = false

    private fun toggleFlashlight(): Boolean {
        return try {
            val cameraManager = getSystemService(Context.CAMERA_SERVICE) as android.hardware.camera2.CameraManager
            val cameraId = cameraManager.cameraIdList.firstOrNull { id ->
                cameraManager.getCameraCharacteristics(id)
                    .get(android.hardware.camera2.CameraCharacteristics.FLASH_INFO_AVAILABLE) == true
            } ?: return torchOn
            val next = !torchOn
            cameraManager.setTorchMode(cameraId, next)
            torchOn = next
            torchOn
        } catch (_: Exception) {
            torchOn
        }
    }

    // ---------- do not disturb ----------
    // Requires the special "Notification Policy Access" grant (same tier as
    // notification-listener access) -- MainActivity has a button that opens the
    // settings screen for it. Silently no-ops until granted; dndAvailable in
    // /api/state tells the dashboard whether to prompt for it.

    private fun setDnd(enabled: Boolean): Boolean {
        return try {
            val nm = getSystemService(NotificationManager::class.java)
            if (!nm.isNotificationPolicyAccessGranted) return false
            nm.setInterruptionFilter(
                if (enabled) NotificationManager.INTERRUPTION_FILTER_PRIORITY
                else NotificationManager.INTERRUPTION_FILTER_ALL
            )
            true
        } catch (_: Exception) {
            false
        }
    }

    private fun clearAllNotifications(): Boolean {
        val svc = NotificationAccessService.instance ?: return false
        return try {
            svc.cancelAllNotifications()
            true
        } catch (_: Exception) {
            false
        }
    }

    private fun setClipboardFromBody(body: String) {
        try {
            val json = JSONObject(body)
            val text = json.optString("text", "")
            Handler(Looper.getMainLooper()).post {
                try {
                    val clipboard = getSystemService(Context.CLIPBOARD_SERVICE) as android.content.ClipboardManager
                    clipboard.setPrimaryClip(android.content.ClipData.newPlainText("Dromac", text))
                } catch (_: Exception) {}
            }
        } catch (_: Exception) {}
    }

    // ---------- notifications ----------

    private fun buildNotificationsJson(): JSONObject {
        val root = JSONObject()
        val arr = JSONArray()
        val svc = NotificationAccessService.instance
        if (svc != null) {
            try {
                val active = svc.activeNotifications
                    .sortedByDescending { it.postTime }
                    .take(20)
                for (sbn in active) {
                    val extras = sbn.notification.extras
                    val title = extras.getCharSequence(Notification.EXTRA_TITLE)?.toString() ?: ""
                    val text = extras.getCharSequence(Notification.EXTRA_TEXT)?.toString() ?: ""
                    if (title.isEmpty() && text.isEmpty()) continue
                    val item = JSONObject()
                    item.put("key", sbn.key)
                    item.put("app", sbn.packageName)
                    item.put("title", title)
                    item.put("text", text)
                    item.put("when", sbn.postTime)
                    val actionsArr = JSONArray()
                    sbn.notification.actions?.forEachIndexed { idx, action ->
                        val a = JSONObject()
                        a.put("index", idx)
                        a.put("title", action.title?.toString() ?: "")
                        a.put("hasReply", !action.remoteInputs.isNullOrEmpty())
                        actionsArr.put(a)
                    }
                    item.put("actions", actionsArr)
                    arr.put(item)
                }
            } catch (_: Exception) {}
        }
        root.put("notifications", arr)
        return root
    }

    private fun findNotification(key: String): StatusBarNotification? {
        val svc = NotificationAccessService.instance ?: return null
        return try {
            svc.activeNotifications.firstOrNull { it.key == key }
        } catch (_: Exception) {
            null
        }
    }

    private fun dismissNotification(key: String): Boolean {
        val svc = NotificationAccessService.instance ?: return false
        return try {
            svc.cancelNotification(key)
            true
        } catch (_: Exception) {
            false
        }
    }

    private fun fireNotificationAction(key: String, actionIndex: Int, text: String?): Boolean {
        val sbn = findNotification(key) ?: return false
        val actions = sbn.notification.actions ?: return false
        if (actionIndex !in actions.indices) return false
        val action = actions[actionIndex]
        return try {
            val remoteInputs = action.remoteInputs
            if (!remoteInputs.isNullOrEmpty() && !text.isNullOrEmpty()) {
                val intent = Intent()
                val bundle = Bundle()
                for (ri in remoteInputs) {
                    bundle.putCharSequence(ri.resultKey, text)
                }
                RemoteInput.addResultsToIntent(remoteInputs, intent, bundle)
                action.actionIntent.send(applicationContext, 0, intent)
            } else {
                action.actionIntent.send()
            }
            true
        } catch (_: Exception) {
            false
        }
    }

    // ---------- calls ----------
    // Answering is possible with the normal, runtime-grantable ANSWER_PHONE_CALLS
    // permission. Declining/ending a call programmatically is NOT included here --
    // on modern Android that needs MODIFY_PHONE_STATE, a system|signature
    // permission no ordinary app (this one included) can be granted, so it's a
    // real platform wall rather than something left unfinished.
    private fun answerCall(): Boolean {
        return try {
            val telecomManager = getSystemService(Context.TELECOM_SERVICE) as TelecomManager
            telecomManager.acceptRingingCall()
            true
        } catch (_: Exception) {
            false
        }
    }

    // ---------- app launcher ----------

    private fun listLaunchableApps(): JSONArray {
        val arr = JSONArray()
        return try {
            val intent = Intent(Intent.ACTION_MAIN, null).addCategory(Intent.CATEGORY_LAUNCHER)
            val resolved = packageManager.queryIntentActivities(intent, 0)
            resolved
                .map { it.activityInfo.packageName to it.loadLabel(packageManager).toString() }
                .distinctBy { it.first }
                .sortedBy { it.second.lowercase() }
                .forEach { (pkg, label) ->
                    arr.put(JSONObject().apply { put("package", pkg); put("label", label) })
                }
            arr
        } catch (_: Exception) {
            arr
        }
    }

    // A plain startActivity() from here silently does nothing on Android 10+ --
    // background services aren't allowed to bring another app to the foreground
    // (a real OS restriction, not a bug on our end). The one documented exemption
    // that fits a headless service is a full-screen-intent notification, the same
    // mechanism used for incoming-call/alarm UI, so that's what actually launches
    // the app here instead of a direct startActivity call.
    private fun launchApp(packageName: String): Boolean {
        return try {
            val intent = packageManager.getLaunchIntentForPackage(packageName) ?: return false
            intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            startActivityFromBackground(intent)
            true
        } catch (_: Exception) {
            false
        }
    }

    // ---------- screen ----------

    private fun wakeScreen() {
        try {
            val pm = getSystemService(Context.POWER_SERVICE) as PowerManager
            @Suppress("DEPRECATION")
            val wl = pm.newWakeLock(
                PowerManager.SCREEN_BRIGHT_WAKE_LOCK or PowerManager.ACQUIRE_CAUSES_WAKEUP,
                "dromac:wake"
            )
            wl.acquire(10_000)
        } catch (_: Exception) {}
    }

    private fun toggleKeepAwake(): Boolean {
        return try {
            val held = keepAwakeWakeLock?.isHeld == true
            if (held) {
                keepAwakeWakeLock?.release()
                false
            } else {
                val pm = getSystemService(Context.POWER_SERVICE) as PowerManager
                val wl = pm.newWakeLock(PowerManager.SCREEN_DIM_WAKE_LOCK, "dromac:keep_awake")
                wl.acquire()
                keepAwakeWakeLock = wl
                true
            }
        } catch (_: Exception) {
            false
        }
    }

    // A real "turn the display off" API doesn't exist for a non-system app (see the
    // long comment this replaced), but an OLED panel means a true-black overlay costs
    // almost no power on those pixels anyway -- so instead of fighting the OS's
    // screen-off/lock coupling, just paint over the real content with an opaque
    // black overlay (the same SYSTEM_ALERT_WINDOW mechanism as startActivityFromBackground).
    // FLAG_NOT_TOUCHABLE lets it sit purely visually on top: real touches (including
    // our own AccessibilityService-injected ones for remote control) still reach the
    // actual UI underneath, so mirroring + control keep working while the physical
    // screen just looks off. FLAG_KEEP_SCREEN_ON keeps the real display awake and
    // interactive the whole time, never touching the lock timeout at all.
    private var blackoutView: android.view.View? = null

    @Volatile var lastBlackoutError: String? = null

    private fun setBlackout(enabled: Boolean): Boolean {
        lastBlackoutError = null
        val canOverlay = try { android.provider.Settings.canDrawOverlays(this) } catch (_: Exception) { false }
        if (!canOverlay) {
            lastBlackoutError = "Display Over Apps not granted"
            return false
        }
        return try {
            val wm = getSystemService(Context.WINDOW_SERVICE) as android.view.WindowManager
            if (enabled) {
                if (blackoutView == null) {
                    val view = android.view.View(this)
                    view.setBackgroundColor(android.graphics.Color.BLACK)
                    val type = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O)
                        android.view.WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY
                    else
                        @Suppress("DEPRECATION") android.view.WindowManager.LayoutParams.TYPE_PHONE
                    val params = android.view.WindowManager.LayoutParams(
                        android.view.WindowManager.LayoutParams.MATCH_PARENT,
                        android.view.WindowManager.LayoutParams.MATCH_PARENT,
                        type,
                        android.view.WindowManager.LayoutParams.FLAG_NOT_TOUCHABLE or
                            android.view.WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE or
                            android.view.WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON or
                            android.view.WindowManager.LayoutParams.FLAG_LAYOUT_IN_SCREEN or
                            android.view.WindowManager.LayoutParams.FLAG_LAYOUT_NO_LIMITS,
                        android.graphics.PixelFormat.OPAQUE
                    )
                    // Extend under the status bar / camera cutout / gesture nav area too --
                    // without this a border around the "true black" area can stay lit.
                    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
                        params.layoutInDisplayCutoutMode =
                            android.view.WindowManager.LayoutParams.LAYOUT_IN_DISPLAY_CUTOUT_MODE_SHORT_EDGES
                    }
                    wm.addView(view, params)
                    blackoutView = view
                }
            } else {
                blackoutView?.let { try { wm.removeView(it) } catch (_: Exception) {} }
                blackoutView = null
            }
            true
        } catch (e: Exception) {
            lastBlackoutError = "${e.javaClass.simpleName}: ${e.message}"
            false
        }
    }

    // ---------- screen mirroring + remote control ----------
    // Screen capture uses MediaProjection, the only non-root way to read the display's
    // pixels -- Android requires its consent dialog to be shown from a real, visible
    // Activity (a background service can never trigger it), so MainActivity shows it
    // once and hands the resulting grant here via setProjectionConsent(). That grant
    // can then be reused to start/stop capture freely without re-prompting, for as
    // long as this service keeps running. Frames are downscaled + JPEG-encoded and
    // served as an MJPEG stream (one HTTP connection kept open, one boundary-delimited
    // JPEG per frame) -- simple, and an <img> tag on the Mac dashboard can play it
    // directly with zero client-side video decoding.
    //
    // Touch is injected via DromacAccessibilityService.dispatchGesture(), the
    // documented non-root mechanism for synthesizing taps/swipes system-wide.
    // Deliberately cannot see or touch the lock screen -- MediaProjection renders
    // black there by OS design (see the conversation this came out of), so this is
    // "use the phone from the Mac once it's already unlocked," same as real remote
    // desktop tools on Android.

    private var projectionResultCode: Int = 0
    private var projectionResultData: Intent? = null
    private var mediaProjection: MediaProjection? = null
    private var virtualDisplay: VirtualDisplay? = null
    private var imageReader: ImageReader? = null
    @Volatile private var latestFrameJpeg: ByteArray? = null
    private val frameLock = Object()
    private var mirrorWidth = 0
    private var mirrorHeight = 0

    fun setProjectionConsent(resultCode: Int, data: Intent) {
        // NOT .clone()'d: the consent Intent carries a special token the system uses to
        // look up the grant later, and cloning it appears to break that reference --
        // getMediaProjection() would then fail with a "not found" error. Store the
        // real Intent object as-is.
        projectionResultCode = resultCode
        projectionResultData = data
    }

    fun hasProjectionConsent(): Boolean = projectionResultData != null

    private val projectionCallback = object : MediaProjection.Callback() {
        override fun onStop() {
            stopMirroring()
        }
    }

    @Volatile var lastMirrorError: String? = null

    private fun startMirroring(): Boolean {
        if (mediaProjection != null) return true
        val data = projectionResultData ?: return false
        lastMirrorError = null
        // The exact platform exception ("Media projections require a foreground
        // service of type ...MEDIA_PROJECTION") is thrown by getMediaProjection()
        // ITSELF -- before it ever returns an object -- so the type must be
        // active before this call, not merely before createVirtualDisplay().
        mirrorActive = true
        promoteToMediaProjectionType()
        return try {
            val mpm = getSystemService(Context.MEDIA_PROJECTION_SERVICE) as MediaProjectionManager
            val projection = mpm.getMediaProjection(projectionResultCode, data)
            if (projection == null) {
                lastMirrorError = "getMediaProjection returned null"
                stopMirroring()
                return false
            }
            mediaProjection = projection
            projection.registerCallback(projectionCallback, Handler(Looper.getMainLooper()))

            val (realW, realH) = realDisplaySize()
            val scale = 480f / maxOf(realW, 1)
            mirrorWidth = (realW * scale).toInt().coerceAtLeast(2)
            mirrorHeight = (realH * scale).toInt().coerceAtLeast(2)

            val reader = ImageReader.newInstance(mirrorWidth, mirrorHeight, android.graphics.PixelFormat.RGBA_8888, 2)
            reader.setOnImageAvailableListener({ r ->
                var image: Image? = null
                try {
                    image = r.acquireLatestImage()
                    if (image != null) {
                        val jpeg = imageToJpeg(image)
                        if (jpeg != null) synchronized(frameLock) { latestFrameJpeg = jpeg }
                    }
                } catch (_: Exception) {
                } finally {
                    image?.close()
                }
            }, Handler(Looper.getMainLooper()))

            val vd = projection.createVirtualDisplay(
                "dromac_mirror", mirrorWidth, mirrorHeight, resources.displayMetrics.densityDpi,
                DisplayManager.VIRTUAL_DISPLAY_FLAG_AUTO_MIRROR,
                reader.surface, null, null
            )

            imageReader = reader
            virtualDisplay = vd
            true
        } catch (e: Exception) {
            lastMirrorError = "${e.javaClass.simpleName}: ${e.message}"
            stopMirroring()
            false
        }
    }

    fun isMirrorActive(): Boolean = mediaProjection != null

    fun stopMirroringFromUi() = stopMirroring()

    private fun stopMirroring() {
        try { virtualDisplay?.release() } catch (_: Exception) {}
        try { imageReader?.close() } catch (_: Exception) {}
        try { mediaProjection?.unregisterCallback(projectionCallback) } catch (_: Exception) {}
        try { mediaProjection?.stop() } catch (_: Exception) {}
        virtualDisplay = null
        imageReader = null
        mediaProjection = null
        mirrorActive = false
        // The consent grant is spent the moment createVirtualDisplay() has been called once --
        // it can never be reused for a second capture, even on a fresh MediaProjection instance
        // from the same resultData. Clearing it here makes MainActivity's "Allow Screen
        // Mirroring" button reappear automatically so the user has an obvious way back in,
        // instead of it staying hidden while silently no longer working.
        projectionResultData = null
        synchronized(frameLock) { latestFrameJpeg = null }
        refreshForegroundServiceType()
    }

    private fun imageToJpeg(image: Image): ByteArray? {
        return try {
            val plane = image.planes[0]
            val buffer = plane.buffer
            val pixelStride = plane.pixelStride
            val rowStride = plane.rowStride
            val rowPadding = rowStride - pixelStride * image.width
            val bitmap = Bitmap.createBitmap(
                image.width + rowPadding / pixelStride, image.height, Bitmap.Config.ARGB_8888
            )
            bitmap.copyPixelsFromBuffer(buffer)
            val cropped = if (rowPadding == 0) bitmap else Bitmap.createBitmap(bitmap, 0, 0, image.width, image.height)
            val stream = ByteArrayOutputStream()
            cropped.compress(Bitmap.CompressFormat.JPEG, 55, stream)
            if (cropped !== bitmap) bitmap.recycle()
            cropped.recycle()
            stream.toByteArray()
        } catch (_: Exception) {
            null
        }
    }

    // Streams MJPEG (multipart/x-mixed-replace) over the already-open socket -- unlike
    // every other route this keeps writing for as long as the client stays connected,
    // instead of one response and done.
    private fun streamScreen(socket: Socket, out: OutputStream) {
        val boundary = "dromacframe"
        try {
            val header = "HTTP/1.1 200 OK\r\nContent-Type: multipart/x-mixed-replace; boundary=$boundary\r\nCache-Control: no-store\r\nConnection: close\r\n\r\n"
            out.write(header.toByteArray(Charsets.UTF_8))
            out.flush()
            var lastSent: ByteArray? = null
            while (running && mediaProjection != null && !socket.isClosed) {
                val frame = synchronized(frameLock) { latestFrameJpeg }
                if (frame != null && frame !== lastSent) {
                    lastSent = frame
                    val part = "--$boundary\r\nContent-Type: image/jpeg\r\nContent-Length: ${frame.size}\r\n\r\n"
                    out.write(part.toByteArray(Charsets.UTF_8))
                    out.write(frame)
                    out.write("\r\n".toByteArray(Charsets.UTF_8))
                    out.flush()
                }
                Thread.sleep(120)
            }
        } catch (_: Exception) {
            // client disconnected, or mirroring was stopped -- either way just end the stream
        }
    }

    // Touch coordinates are normalized (0..1) against the REAL screen, not the
    // downscaled mirror resolution, since gestures are dispatched at full display scale.
    // resources.displayMetrics can report a SMALLER height than the display
    // actually is on some devices/API levels (it's the app-window size, which
    // may exclude the nav bar) -- but MediaProjection's AUTO_MIRROR capture
    // always mirrors the true, full physical display regardless. Sizing the
    // mirror capture with one height and mapping taps back with a shorter one
    // is exactly a proportional (0 at the top, growing toward the bottom)
    // error: a tap at 95% down the image lands at 95% of the SHORTER height,
    // which is above where 95% of the true (taller) screen actually is. Using
    // WindowManager's real size for both eliminates the mismatch outright.
    private fun realDisplaySize(): Pair<Int, Int> {
        return try {
            val wm = getSystemService(Context.WINDOW_SERVICE) as android.view.WindowManager
            val point = android.graphics.Point()
            @Suppress("DEPRECATION")
            wm.defaultDisplay.getRealSize(point)
            Pair(point.x, point.y)
        } catch (_: Exception) {
            Pair(resources.displayMetrics.widthPixels, resources.displayMetrics.heightPixels)
        }
    }

    private fun dispatchTap(xNorm: Double, yNorm: Double): Boolean {
        val svc = DromacAccessibilityService.instance ?: return false
        val (w, h) = realDisplaySize()
        return svc.tap((xNorm * w).toFloat(), (yNorm * h).toFloat())
    }

    private fun dispatchSwipe(x1: Double, y1: Double, x2: Double, y2: Double, durationMs: Long): Boolean {
        val svc = DromacAccessibilityService.instance ?: return false
        val (w, h) = realDisplaySize()
        return svc.swipe(
            (x1 * w).toFloat(), (y1 * h).toFloat(),
            (x2 * w).toFloat(), (y2 * h).toFloat(), durationMs
        )
    }

    private fun lockScreen(): Boolean {
        return try {
            val dpm = getSystemService(Context.DEVICE_POLICY_SERVICE) as DevicePolicyManager
            val adminComponent = ComponentName(this, DromacDeviceAdminReceiver::class.java)
            if (!dpm.isAdminActive(adminComponent)) return false
            dpm.lockNow()
            true
        } catch (_: Exception) {
            false
        }
    }

    // ---------- location ----------
    // On-demand only (a button press on the dashboard), never continuous tracking --
    // uses the plain LocationManager API (no Play Services dependency) and just
    // takes whatever fix is already cached, which is instant and battery-free.

    private fun fetchLocation(): JSONObject {
        val result = JSONObject()
        try {
            val lm = getSystemService(Context.LOCATION_SERVICE) as LocationManager
            val providers = lm.getProviders(true)
            var best: android.location.Location? = null
            for (p in providers) {
                val loc = try { lm.getLastKnownLocation(p) } catch (_: SecurityException) { null } ?: continue
                if (best == null || loc.time > best!!.time) best = loc
            }
            if (best != null) {
                result.put("lat", best.latitude)
                result.put("lng", best.longitude)
                result.put("ageMs", System.currentTimeMillis() - best.time)
            }
        } catch (_: Exception) {}
        return result
    }

    // ---------- calendar ----------

    private var cachedNextEvent: JSONObject? = null
    private var cachedNextEventAt: Long = 0

    private fun getNextCalendarEvent(): JSONObject? {
        if (System.currentTimeMillis() - cachedNextEventAt < 60_000) return cachedNextEvent
        cachedNextEventAt = System.currentTimeMillis()
        cachedNextEvent = try {
            val now = System.currentTimeMillis()
            val uri = CalendarContract.Instances.CONTENT_URI.buildUpon()
                .appendPath(now.toString())
                .appendPath((now + 24 * 60 * 60 * 1000).toString())
                .build()
            val projection = arrayOf(CalendarContract.Instances.TITLE, CalendarContract.Instances.BEGIN)
            var cursor: Cursor? = null
            try {
                cursor = contentResolver.query(uri, projection, null, null, "${CalendarContract.Instances.BEGIN} ASC")
                if (cursor != null && cursor.moveToFirst()) {
                    JSONObject().apply {
                        put("title", cursor.getString(0) ?: "")
                        put("startAt", cursor.getLong(1))
                    }
                } else null
            } finally {
                cursor?.close()
            }
        } catch (_: Exception) {
            null
        }
        return cachedNextEvent
    }

    // ---------- usage stats ----------

    private var cachedUsageMin = -1
    private var cachedUsageAt = 0L

    private fun getUsageTodayMinutes(): Int {
        if (System.currentTimeMillis() - cachedUsageAt < 30_000) return cachedUsageMin
        cachedUsageAt = System.currentTimeMillis()
        cachedUsageMin = try {
            val usm = getSystemService(Context.USAGE_STATS_SERVICE) as UsageStatsManager
            val cal = java.util.Calendar.getInstance()
            cal.set(java.util.Calendar.HOUR_OF_DAY, 0)
            cal.set(java.util.Calendar.MINUTE, 0)
            cal.set(java.util.Calendar.SECOND, 0)
            val start = cal.timeInMillis
            val stats = usm.queryUsageStats(UsageStatsManager.INTERVAL_DAILY, start, System.currentTimeMillis())
            ((stats?.sumOf { it.totalTimeInForeground } ?: 0L) / 60000L).toInt()
        } catch (_: Exception) {
            -1
        }
        return cachedUsageMin
    }

    // ---------- http helpers ----------

    private fun writeJson(out: OutputStream, status: Int, json: String) {
        val statusText = if (status == 200) "OK" else if (status == 404) "Not Found" else "Error"
        val bytes = json.toByteArray(Charsets.UTF_8)
        val header = "HTTP/1.1 $status $statusText\r\nContent-Type: application/json\r\nContent-Length: ${bytes.size}\r\nConnection: close\r\n\r\n"
        out.write(header.toByteArray(Charsets.UTF_8))
        out.write(bytes)
        out.flush()
    }

    private fun writeBinary(out: OutputStream, status: Int, contentType: String, bytes: ByteArray) {
        val header = "HTTP/1.1 $status OK\r\nContent-Type: $contentType\r\nContent-Length: ${bytes.size}\r\nConnection: close\r\n\r\n"
        out.write(header.toByteArray(Charsets.UTF_8))
        out.write(bytes)
        out.flush()
    }
}

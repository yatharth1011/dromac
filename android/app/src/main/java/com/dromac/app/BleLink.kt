package com.dromac.app

import android.annotation.SuppressLint
import android.bluetooth.BluetoothDevice
import android.bluetooth.BluetoothGatt
import android.bluetooth.BluetoothGattCharacteristic
import android.bluetooth.BluetoothGattDescriptor
import android.bluetooth.BluetoothGattServer
import android.bluetooth.BluetoothGattServerCallback
import android.bluetooth.BluetoothGattService
import android.bluetooth.BluetoothManager
import android.bluetooth.BluetoothProfile
import android.content.Context
import android.os.Build
import android.util.Base64
import android.util.Log
import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.net.HttpURLConnection
import java.net.URL
import java.security.MessageDigest
import java.security.SecureRandom
import java.util.UUID
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.Executors
import java.util.concurrent.LinkedBlockingQueue
import java.util.concurrent.TimeUnit

// Bluetooth data link: the fallback for when the Mac can reach this phone neither
// over the USB cable nor over Wi-Fi (campus networks that keep devices apart).
//
// A GATT service with two characteristics. The Mac writes a request into REQUEST
// and this replays it against our own HTTP server on 127.0.0.1, then streams the
// reply back as RESPONSE notifications -- so every endpoint the Mac uses over Wi-Fi
// works here unchanged, just slower (fine for state, controls and clipboard text).
//
// Framing: every write/notification starts with one byte, 1 = last frame of the
// message, 0 = more follow. Messages are JSON:
//   request {k: key, m: method, p: path, b: body text}
//   reply   {s: status, t: content type, b: body (base64)}
//
// Anyone within Bluetooth range could otherwise drive the phone, so every request
// must carry the pairing key, which the phone only hands out over the USB cable
// (GET /api/ble_key from loopback; see StationServerService.route).
@SuppressLint("MissingPermission") // only started once BLUETOOTH_CONNECT is granted
class BleLink(private val context: Context, private val httpPort: Int) {

    companion object {
        val SERVICE_UUID: UUID = UUID.fromString("d20a0001-7c2e-4b8a-9f3e-6a1d5c0b7e01")
        val REQUEST_UUID: UUID = UUID.fromString("d20a0002-7c2e-4b8a-9f3e-6a1d5c0b7e01")
        val RESPONSE_UUID: UUID = UUID.fromString("d20a0003-7c2e-4b8a-9f3e-6a1d5c0b7e01")
        private val CCCD_UUID: UUID = UUID.fromString("00002902-0000-1000-8000-00805f9b34fb")
        private const val MAX_REPLY_BYTES = 512 * 1024
        private const val NOTIFY_ACK_TIMEOUT_MS = 3000L
        private const val TAG = "DromacBleLink"

        fun key(context: Context): String {
            val prefs = context.getSharedPreferences("dromac_ble_link", Context.MODE_PRIVATE)
            prefs.getString("key", null)?.let { return it }
            val bytes = ByteArray(16).also { SecureRandom().nextBytes(it) }
            val fresh = bytes.joinToString("") { "%02x".format(it) }
            prefs.edit().putString("key", fresh).apply()
            return fresh
        }
    }

    private var server: BluetoothGattServer? = null
    private var responseChar: BluetoothGattCharacteristic? = null
    private val incoming = ConcurrentHashMap<String, ByteArrayOutputStream>()
    private val mtu = ConcurrentHashMap<String, Int>()
    // One request at a time: BLE is a single slow pipe, and notifications must be
    // sent strictly one after another (each waits for onNotificationSent).
    private val worker = Executors.newSingleThreadExecutor()
    private val notifyAcks = LinkedBlockingQueue<Int>()

    val isRunning: Boolean get() = server != null
    private val connected = ConcurrentHashMap.newKeySet<String>()
    @Volatile private var lastWriteAt = 0L
    // While the Mac is connected, the beacon must not be restarted: on recent
    // Android, stopping the advertising set a connection came in through also
    // detaches our GATT server from that connection, and the link drops.
    // Checked against what Android says is really connected (and a minute of
    // silence counts as gone): a remembered connection that had silently died
    // -- Bluetooth switched off under it -- used to stop the beacon for good.
    val hasClients: Boolean get() {
        if (connected.isEmpty()) return false
        val live = try {
            (context.getSystemService(Context.BLUETOOTH_SERVICE) as BluetoothManager)
                .getConnectedDevices(BluetoothProfile.GATT_SERVER).map { it.address }.toSet()
        } catch (_: Exception) { emptySet() }
        connected.retainAll(live)
        if (connected.isNotEmpty() && System.currentTimeMillis() - lastWriteAt > 60_000) connected.clear()
        return connected.isNotEmpty()
    }

    fun start(): Boolean {
        if (server != null) return true
        return try {
            val manager = context.getSystemService(Context.BLUETOOTH_SERVICE) as? BluetoothManager ?: return false
            if (manager.adapter?.isEnabled != true) return false
            val gatt = manager.openGattServer(context, callback) ?: return false
            val service = BluetoothGattService(SERVICE_UUID, BluetoothGattService.SERVICE_TYPE_PRIMARY)
            val request = BluetoothGattCharacteristic(
                REQUEST_UUID,
                BluetoothGattCharacteristic.PROPERTY_WRITE or BluetoothGattCharacteristic.PROPERTY_WRITE_NO_RESPONSE,
                BluetoothGattCharacteristic.PERMISSION_WRITE,
            )
            val response = BluetoothGattCharacteristic(
                RESPONSE_UUID,
                BluetoothGattCharacteristic.PROPERTY_NOTIFY,
                BluetoothGattCharacteristic.PERMISSION_READ,
            )
            response.addDescriptor(BluetoothGattDescriptor(
                CCCD_UUID, BluetoothGattDescriptor.PERMISSION_READ or BluetoothGattDescriptor.PERMISSION_WRITE,
            ))
            service.addCharacteristic(request)
            service.addCharacteristic(response)
            gatt.addService(service)
            server = gatt
            responseChar = response
            true
        } catch (_: SecurityException) {
            false
        } catch (_: Exception) {
            false
        }
    }

    // The adapter going off kills the server underneath us; drop it so the next
    // start() builds a fresh one.
    fun stop() {
        try { server?.close() } catch (_: Exception) {}
        server = null
        responseChar = null
        incoming.clear()
        mtu.clear()
        connected.clear()     // whatever was connected is gone with the server
    }

    private val callback = object : BluetoothGattServerCallback() {
        override fun onConnectionStateChange(device: BluetoothDevice, status: Int, newState: Int) {
            Log.i(TAG, "connection state $newState (status $status)")
            if (newState == BluetoothProfile.STATE_CONNECTED) {
                connected.add(device.address)
                lastWriteAt = System.currentTimeMillis()
            }
            if (newState == BluetoothProfile.STATE_DISCONNECTED) {
                connected.remove(device.address)
                incoming.remove(device.address)
                mtu.remove(device.address)
            }
        }

        override fun onMtuChanged(device: BluetoothDevice, newMtu: Int) {
            mtu[device.address] = newMtu
            Log.i(TAG, "mtu $newMtu")
        }

        override fun onCharacteristicWriteRequest(
            device: BluetoothDevice, requestId: Int, characteristic: BluetoothGattCharacteristic,
            preparedWrite: Boolean, responseNeeded: Boolean, offset: Int, value: ByteArray?,
        ) {
            val ok = characteristic.uuid == REQUEST_UUID && value != null && value.isNotEmpty()
            lastWriteAt = System.currentTimeMillis()
            Log.i(TAG, "write ${value?.size ?: 0} bytes ok=$ok last=${value?.firstOrNull()?.toInt() == 1} responseNeeded=$responseNeeded")
            if (responseNeeded) {
                server?.sendResponse(device, requestId, if (ok) BluetoothGatt.GATT_SUCCESS else BluetoothGatt.GATT_FAILURE, 0, null)
            }
            if (!ok) return
            val buf = incoming.getOrPut(device.address) { ByteArrayOutputStream() }
            buf.write(value!!, 1, value.size - 1)
            if (buf.size() > MAX_REPLY_BYTES) { incoming.remove(device.address); return }
            if (value[0].toInt() == 1) {
                incoming.remove(device.address)
                val message = buf.toByteArray()
                worker.execute { handle(device, message) }
            }
        }

        override fun onDescriptorWriteRequest(
            device: BluetoothDevice, requestId: Int, descriptor: BluetoothGattDescriptor,
            preparedWrite: Boolean, responseNeeded: Boolean, offset: Int, value: ByteArray?,
        ) {
            if (responseNeeded) server?.sendResponse(device, requestId, BluetoothGatt.GATT_SUCCESS, 0, null)
        }

        override fun onNotificationSent(device: BluetoothDevice, status: Int) {
            notifyAcks.offer(status)
        }
    }

    private fun handle(device: BluetoothDevice, message: ByteArray) {
        val reply = try {
            val req = JSONObject(String(message, Charsets.UTF_8))
            val expected = key(context).toByteArray()
            val given = req.optString("k", "").toByteArray()
            val path = req.optString("p", "/")
            when {
                !MessageDigest.isEqual(expected, given) -> reply(403, "application/json", "{\"error\":\"bad key\"}".toByteArray())
                // The key itself is only ever handed out over the cable.
                path.startsWith("/api/ble_key") || !path.startsWith("/") -> reply(403, "application/json", "{\"error\":\"forbidden\"}".toByteArray())
                else -> forward(req.optString("m", "GET"), path, if (req.has("b")) req.optString("b") else null)
            }
        } catch (_: Exception) {
            reply(400, "application/json", "{\"error\":\"bad request\"}".toByteArray())
        }
        Log.i(TAG, "replying ${reply.size} bytes")
        send(device, reply)
    }

    private fun forward(method: String, path: String, body: String?): ByteArray {
        val conn = URL("http://127.0.0.1:$httpPort$path").openConnection() as HttpURLConnection
        return try {
            conn.requestMethod = method
            conn.connectTimeout = 3000
            conn.readTimeout = 15000
            if (body != null && method != "GET") {
                conn.doOutput = true
                conn.setRequestProperty("Content-Type", "application/json")
                conn.outputStream.use { it.write(body.toByteArray(Charsets.UTF_8)) }
            }
            val status = conn.responseCode
            val stream = if (status >= 400) conn.errorStream else conn.inputStream
            val data = stream?.use { it.readBytes() } ?: ByteArray(0)
            if (data.size > MAX_REPLY_BYTES) reply(413, "application/json", "{\"error\":\"too large for bluetooth\"}".toByteArray())
            else reply(status, conn.contentType ?: "application/octet-stream", data)
        } catch (_: Exception) {
            reply(502, "application/json", "{\"error\":\"phone server unreachable\"}".toByteArray())
        } finally {
            conn.disconnect()
        }
    }

    private fun reply(status: Int, type: String, body: ByteArray): ByteArray =
        JSONObject()
            .put("s", status)
            .put("t", type)
            .put("b", Base64.encodeToString(body, Base64.NO_WRAP))
            .toString().toByteArray(Charsets.UTF_8)

    private fun send(device: BluetoothDevice, message: ByteArray) {
        val gatt = server ?: return
        val char = responseChar ?: return
        // ATT payload is MTU - 3, one byte of that goes to our frame header, and
        // no attribute value may exceed 512 bytes (the stack refuses the notify).
        val chunk = minOf(511, maxOf(19, (mtu[device.address] ?: 23) - 4))
        var offset = 0
        do {
            val end = minOf(message.size, offset + chunk)
            val frame = ByteArray(1 + end - offset)
            frame[0] = if (end == message.size) 1 else 0
            System.arraycopy(message, offset, frame, 1, end - offset)
            notifyAcks.clear()
            val queued = try {
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                    gatt.notifyCharacteristicChanged(device, char, false, frame) == BluetoothGatt.GATT_SUCCESS
                } else {
                    @Suppress("DEPRECATION")
                    char.value = frame
                    @Suppress("DEPRECATION")
                    gatt.notifyCharacteristicChanged(device, char, false)
                }
            } catch (_: Exception) { false }
            if (!queued) { Log.w(TAG, "notify not queued at $offset"); return }
            val ack = notifyAcks.poll(NOTIFY_ACK_TIMEOUT_MS, TimeUnit.MILLISECONDS)
            if (ack != BluetoothGatt.GATT_SUCCESS) { Log.w(TAG, "notify ack $ack at $offset"); return }
            offset = end
        } while (offset < message.size)
    }
}

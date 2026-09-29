package com.dromac.app

import android.content.pm.PackageManager
import android.graphics.Color
import android.graphics.drawable.GradientDrawable
import android.os.Bundle
import android.view.Gravity
import android.view.ViewGroup
import android.widget.FrameLayout
import android.widget.LinearLayout
import android.widget.TextView
import android.widget.Toast
import androidx.activity.ComponentActivity
import androidx.camera.core.CameraSelector
import androidx.camera.core.ImageCapture
import androidx.camera.core.ImageCaptureException
import androidx.camera.core.Preview
import androidx.camera.lifecycle.ProcessCameraProvider
import androidx.camera.view.PreviewView
import androidx.core.content.ContextCompat
import java.io.File

// A real, dedicated, instant-capture camera screen -- no confirm/retake step
// (that belongs to the system camera app's own UI when it hands a result
// back to a caller, and no Intent extra can suppress it; it's simply not
// under our control there). Built on CameraX instead of raw Camera2 this
// time: its PreviewView guarantees the live preview and the captured photo
// share the same framing, which is exactly the class of bug three straight
// hand-rolled Camera2/TextureView attempts got wrong without a device to
// verify on. Photos go straight into StationServerService's pending-capture
// mailbox, same destination as every other capture path in the app.
class CameraActivity : ComponentActivity() {

    private lateinit var previewView: PreviewView
    private lateinit var flashButton: TextView
    private var imageCapture: ImageCapture? = null
    private var cameraProvider: ProcessCameraProvider? = null
    private var lensFacing = CameraSelector.LENS_FACING_BACK
    private var flashOn = false

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        if (ContextCompat.checkSelfPermission(this, android.Manifest.permission.CAMERA) != PackageManager.PERMISSION_GRANTED) {
            Toast.makeText(this, "Grant camera access from the Dromac app first", Toast.LENGTH_LONG).show()
            finish()
            return
        }
        buildUi()
        startCamera()
    }

    private fun dp(v: Int) = (v * resources.displayMetrics.density).toInt()

    private fun circleChip(): TextView.() -> Unit = {
        textSize = 18f
        setTextColor(Color.WHITE)
        gravity = Gravity.CENTER
        background = GradientDrawable().apply {
            shape = GradientDrawable.OVAL
            setColor(Color.argb(110, 0, 0, 0))
        }
    }

    private fun buildUi() {
        val root = FrameLayout(this).apply { setBackgroundColor(Color.BLACK) }

        previewView = PreviewView(this).apply {
            // Shows the WHOLE frame, letterboxed if needed -- never crops, so
            // what's on screen and what gets captured are the same framing.
            scaleType = PreviewView.ScaleType.FIT_CENTER
        }
        root.addView(previewView, FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT))

        val scrim = android.view.View(this).apply {
            background = GradientDrawable(
                GradientDrawable.Orientation.BOTTOM_TOP,
                intArrayOf(Color.argb(160, 0, 0, 0), Color.TRANSPARENT)
            )
        }
        root.addView(scrim, FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, dp(160)).apply {
            gravity = Gravity.BOTTOM
        })

        val controls = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER
            setPadding(dp(24), dp(16), dp(24), dp(28))
        }
        root.addView(controls, FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT).apply {
            gravity = Gravity.BOTTOM
        })

        val switchButton = TextView(this).apply { text = "⟲"; apply(circleChip()) }
        val shutter = android.view.View(this).apply {
            background = GradientDrawable().apply {
                shape = GradientDrawable.OVAL
                setColor(Color.WHITE)
                setStroke(dp(3), Color.argb(150, 255, 255, 255))
            }
        }
        flashButton = TextView(this).apply { text = "⚡"; apply(circleChip()); alpha = 0.6f }

        val sideLp = LinearLayout.LayoutParams(dp(48), dp(48))
        val shutterLp = LinearLayout.LayoutParams(dp(72), dp(72)).apply { marginStart = dp(28); marginEnd = dp(28) }
        controls.addView(switchButton, sideLp)
        controls.addView(android.view.View(this), LinearLayout.LayoutParams(0, 0, 1f))
        controls.addView(shutter, shutterLp)
        controls.addView(android.view.View(this), LinearLayout.LayoutParams(0, 0, 1f))
        controls.addView(flashButton, sideLp)

        val closeButton = TextView(this).apply { text = "✕"; apply(circleChip()); textSize = 16f }
        root.addView(closeButton, FrameLayout.LayoutParams(dp(40), dp(40)).apply {
            gravity = Gravity.TOP or Gravity.START
            topMargin = dp(24); leftMargin = dp(16)
        })

        setContentView(root)

        closeButton.setOnClickListener { finish() }
        switchButton.setOnClickListener {
            lensFacing = if (lensFacing == CameraSelector.LENS_FACING_BACK) CameraSelector.LENS_FACING_FRONT else CameraSelector.LENS_FACING_BACK
            bindUseCases()
        }
        flashButton.setOnClickListener {
            flashOn = !flashOn
            flashButton.alpha = if (flashOn) 1f else 0.6f
            imageCapture?.flashMode = if (flashOn) ImageCapture.FLASH_MODE_ON else ImageCapture.FLASH_MODE_OFF
        }
        shutter.setOnClickListener { takePhoto() }
    }

    private fun startCamera() {
        val providerFuture = ProcessCameraProvider.getInstance(this)
        providerFuture.addListener({
            cameraProvider = providerFuture.get()
            bindUseCases()
        }, ContextCompat.getMainExecutor(this))
    }

    private fun bindUseCases() {
        val provider = cameraProvider ?: return
        val preview = Preview.Builder().build().also { it.setSurfaceProvider(previewView.surfaceProvider) }
        val capture = ImageCapture.Builder()
            .setCaptureMode(ImageCapture.CAPTURE_MODE_MINIMIZE_LATENCY)
            .setFlashMode(if (flashOn) ImageCapture.FLASH_MODE_ON else ImageCapture.FLASH_MODE_OFF)
            .build()
        imageCapture = capture
        val selector = CameraSelector.Builder().requireLensFacing(lensFacing).build()
        try {
            provider.unbindAll()
            provider.bindToLifecycle(this, selector, preview, capture)
        } catch (_: Exception) {
            Toast.makeText(this, "Couldn't start this camera", Toast.LENGTH_SHORT).show()
        }
    }

    private fun takePhoto() {
        val capture = imageCapture ?: return
        val file = File(cacheDir, "capture_${System.currentTimeMillis()}.jpg")
        val outputOptions = ImageCapture.OutputFileOptions.Builder(file).build()
        capture.takePicture(
            outputOptions, ContextCompat.getMainExecutor(this),
            object : ImageCapture.OnImageSavedCallback {
                override fun onImageSaved(output: ImageCapture.OutputFileResults) {
                    try {
                        StationServerService.instance?.enqueuePendingCapture(file.readBytes())
                        Toast.makeText(this@CameraActivity, "Saved -- syncing to Mac", Toast.LENGTH_SHORT).show()
                    } catch (_: Exception) {
                    } finally {
                        file.delete()
                    }
                }
                override fun onError(exc: ImageCaptureException) {
                    Toast.makeText(this@CameraActivity, "Capture failed", Toast.LENGTH_SHORT).show()
                }
            }
        )
    }

    override fun onDestroy() {
        try { cameraProvider?.unbindAll() } catch (_: Exception) {}
        super.onDestroy()
    }
}
